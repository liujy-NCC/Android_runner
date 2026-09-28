"""Real socket-level tests: auth headers, status codes, byte accounting."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from harness import Harness, make_source_files
from runner.transports.server import make_server


class LiveServer:
    """A real HTTP server on an ephemeral loopback port."""

    def __init__(self, harness: Harness) -> None:
        self.harness = harness
        self.server = make_server(
            harness.api,
            harness.config,
            host="127.0.0.1",
            port=0,
            session_id=harness.session_id,
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05})
        self.thread.daemon = True
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def request(self, method: str, path: str, body=None, token: str | None = ..., **kwargs):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        token_value = self.harness.token if token is ... else token
        if token_value:
            request.add_header("Authorization", f"Bearer {token_value}")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return (
                    response.status,
                    json.loads(response.read().decode("utf-8")),
                    dict(response.headers),
                )
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8")), dict(exc.headers)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def test_real_http_round_trip_and_headers() -> None:
    h = Harness()
    live = LiveServer(h)
    try:
        status, body, headers = live.request("GET", "/v1/health", token=None)
        assert status == 200 and body["status"] == "ok", (status, body)
        assert headers["X-Runner-Schema-Version"] == "1.0"
        assert headers["X-Runner-Response-Bytes"] == str(len(json.dumps(body, separators=(",", ":"), sort_keys=True).encode()))

        status, body, _ = live.request("GET", "/v1/session")
        assert status == 200 and body["state"] == "ACTIVE", body

        status, body, headers = live.request(
            "POST",
            "/v1/candidates",
            {"idempotency_key": "http-key-000000000001", "files": make_source_files()},
        )
        assert status == 200 and body["status"] == "accepted", body
        candidate_id = body["candidate_id"]
        assert int(headers["X-Runner-Response-Bytes"]) > 0
    finally:
        live.close()
        h.close()


def test_real_http_rejects_missing_and_bad_credentials() -> None:
    h = Harness()
    live = LiveServer(h)
    try:
        status, body, _ = live.request("GET", "/v1/session", token=None)
        assert status == 401 and body["error"]["code"] == "unauthenticated", (status, body)
        status, body, _ = live.request("GET", "/v1/session", token="nope")
        assert status == 401, (status, body)
    finally:
        live.close()
        h.close()


def test_real_http_forbidden_paths_are_404() -> None:
    h = Harness()
    live = LiveServer(h)
    try:
        for path in ("/v1/adb", "/v1/exec", "/v1/shell", "/v1/devices", "/v1/files", "/v1/oracle"):
            status, body, _ = live.request("GET", path)
            assert status == 404, (path, status, body)
            status, body, _ = live.request("POST", path, {"idempotency_key": "http-key-000000000002"})
            assert status == 404, (path, status, body)
    finally:
        live.close()
        h.close()


def test_real_http_rejects_oversized_body_at_the_socket_layer() -> None:
    h = Harness()
    live = LiveServer(h)
    try:
        import base64

        big = base64.b64encode(b"A" * 400_000).decode("ascii")
        status, body, _ = live.request(
            "POST",
            "/v1/candidates",
            {
                "idempotency_key": "http-key-000000000003",
                "files": [{"relative_path": "big.txt", "content_base64": big}],
            },
        )
        assert status == 413, (status, body)
    finally:
        live.close()
        h.close()


def test_status_page_stays_under_eight_kib_and_is_billed() -> None:
    h = Harness()
    live = LiveServer(h)
    try:
        status, body, headers = live.request(
            "POST", "/v1/candidates", {"idempotency_key": "http-key-000000000004", "files": make_source_files()}
        )
        candidate_id = body["candidate_id"]
        status, build, _ = live.request(
            "POST", "/v1/builds", {"idempotency_key": "http-key-000000000005", "candidate_id": candidate_id}
        )
        status, receipt, _ = live.request(
            "POST", "/v1/attempts", {"idempotency_key": "http-key-000000000006", "build_id": build["build_id"]}
        )
        attempt_id = receipt["attempt_id"]

        for _ in range(5):
            status, page, headers = live.request("GET", f"/v1/attempts/{attempt_id}")
            assert status == 200
            delivered = int(headers["X-Runner-Response-Bytes"])
            assert delivered <= 8192, (delivered, page)
            assert set(page["stages"]) == {
                "intake",
                "build",
                "install",
                "permission_check",
                "launch",
                "observe",
                "cleanup",
            }

        ledger = h.service.store.read_ledger_row(h.session_id, "session:get_attempt")
        assert ledger["count_used"] == 5, ledger
        assert ledger["bytes_used"] > 0
    finally:
        live.close()
        h.close()


def test_get_attempt_call_limit_returns_truncated_quota_error() -> None:
    from runner.config import mock_config

    limits = dict(mock_config().feedback_limits)
    limits["status_call_limit"] = 3
    h = Harness(config=mock_config(feedback_limits=limits))
    try:
        sub = h.submit("submit-key-0000001001")
        build = h.build("build-key-00000001001", sub["candidate_id"])
        attempt_id = h.begin("attempt-key-0000001001", build["build_id"])["attempt_id"]

        for _ in range(3):
            status, page = h.get(f"/v1/attempts/{attempt_id}")
            assert status == 200 and "stages" in page, page

        status, page = h.get(f"/v1/attempts/{attempt_id}")
        assert status == 200
        assert page["truncated"] is True, page
        assert page["truncation_reason"] == "STATUS_CALL_LIMIT", page
        assert "stages" not in page, page
        assert set(page) == {"truncated", "truncation_reason", "error"}, page
    finally:
        h.close()


def test_status_wait_does_not_bypass_the_call_limit() -> None:
    from runner.config import mock_config

    limits = dict(mock_config().feedback_limits)
    limits["status_call_limit"] = 2
    h = Harness(config=mock_config(feedback_limits=limits))
    try:
        sub = h.submit("submit-key-0000001002")
        build = h.build("build-key-00000001002", sub["candidate_id"])
        attempt_id = h.begin("attempt-key-0000001002", build["build_id"])["attempt_id"]

        h.get(f"/v1/attempts/{attempt_id}")
        _s, waited = h.get(f"/v1/attempts/{attempt_id}", query={"wait_for_status_version": "0"})
        assert "stages" in waited, waited
        _s, third = h.get(f"/v1/attempts/{attempt_id}")
        assert third["truncation_reason"] == "STATUS_CALL_LIMIT", third
    finally:
        h.close()


def test_malformed_json_is_rejected_without_a_stack_trace() -> None:
    h = Harness()
    live = LiveServer(h)
    try:
        request = urllib.request.Request(live.base + "/v1/candidates", data=b"{not json", method="POST")
        request.add_header("Authorization", f"Bearer {h.token}")
        request.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400, exc.code
            body = json.loads(exc.read().decode("utf-8"))
            assert body["error"]["code"] == "malformed_request", body
        else:  # pragma: no cover
            raise AssertionError("malformed JSON was accepted")
    finally:
        live.close()
        h.close()
