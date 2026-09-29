"""Real socket-level tests: auth headers, status codes, byte accounting."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

from harness import Harness, make_source_files
from runner.config import mock_config
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
        # Repeat: the 413 must be *delivered* every time. Closing the socket
        # without draining the announced body made the peer see a connection
        # reset (WinError 10053) instead of this response, which is what made
        # the full suite intermittently abort here.
        for attempt in range(5):
            status, body, _ = live.request(
                "POST",
                "/v1/candidates",
                {
                    "idempotency_key": f"http-key-oversized-{attempt:012d}",
                    "files": [{"relative_path": "big.txt", "content_base64": big}],
                },
            )
            assert status == 413, (attempt, status, body)
            assert body["error"]["code"] == "payload_too_large", (attempt, body)
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


# ---------------------------------------------------------------------------
# Draining a rejected body must be bounded in TIME, not only in bytes.
# A byte cap does not stop a peer that announces a large body and then sends
# nothing (or drips), which would otherwise hold the worker thread forever.
# ---------------------------------------------------------------------------
def _open_rejected_request(live: "LiveServer", token: str, *, length: int) -> "socket.socket":
    """Send ONLY the request head with an oversized Content-Length, no body."""

    sock = socket.create_connection(("127.0.0.1", live.port), timeout=20)
    head = (
        f"POST /v1/candidates HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{live.port}\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {length}\r\n"
        f"\r\n"
    ).encode("ascii")
    sock.sendall(head)
    return sock


def _read_response_head(sock: "socket.socket", *, timeout: float) -> tuple[int, str]:
    sock.settimeout(timeout)
    data = b""
    deadline = time.monotonic() + timeout
    while b"\r\n\r\n" not in data:
        if time.monotonic() > deadline:
            raise AssertionError(f"no response head within {timeout}s: {data!r}")
        try:
            chunk = sock.recv(4096)
        except (TimeoutError, socket.timeout):
            raise AssertionError(f"no response head within {timeout}s: {data!r}") from None
        if not chunk:
            break
        data += chunk
    head = data.split(b"\r\n", 1)[0].decode("latin-1")
    parts = head.split(" ")
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return status, head


def test_headers_only_oversized_request_still_gets_413_within_the_drain_budget() -> None:
    """The peer announces a huge body and then sends NOTHING.

    The byte cap cannot help here: nothing is ever sent, so a drain that only
    bounds bytes blocks forever and the 413 never arrives. The total drain
    deadline must expire and the 413 must still be delivered.
    """

    from runner.transports.server import MAX_DRAIN_SECONDS

    h = Harness()
    live = LiveServer(h)
    sock = None
    try:
        started = time.monotonic()
        sock = _open_rejected_request(live, h.token, length=1_000_000)
        status, head = _read_response_head(sock, timeout=MAX_DRAIN_SECONDS + 15.0)
        elapsed = time.monotonic() - started

        assert status == 413, (status, head)
        # Bounded by the drain budget, not by the peer giving up.
        assert elapsed < MAX_DRAIN_SECONDS + 10.0, elapsed
    finally:
        if sock is not None:
            sock.close()
        live.close()
        h.close()


def test_request_stalled_mid_body_still_gets_413_within_the_drain_budget() -> None:
    """The peer sends part of the body and then stops: still bounded in time."""

    from runner.transports.server import MAX_DRAIN_SECONDS

    h = Harness()
    live = LiveServer(h)
    sock = None
    try:
        started = time.monotonic()
        sock = _open_rejected_request(live, h.token, length=1_000_000)
        sock.sendall(b"A" * 1024)  # partial body, then silence
        status, head = _read_response_head(sock, timeout=MAX_DRAIN_SECONDS + 15.0)
        elapsed = time.monotonic() - started

        assert status == 413, (status, head)
        assert elapsed < MAX_DRAIN_SECONDS + 10.0, elapsed
    finally:
        if sock is not None:
            sock.close()
        live.close()
        h.close()


def test_drain_deadline_is_total_not_per_read() -> None:
    """A peer that keeps dribbling must not extend the drain past the budget.

    This is the case that defeats a byte-only bound AND a naive deadline: the
    bytes keep arriving, so every low-level socket read succeeds and no socket
    timeout ever fires. If the drain uses a buffered ``read(n)`` it loops inside
    the buffer until ``n`` bytes arrive, so the outer deadline is only re-checked
    once per buffer fill -- one byte every 0.1s would take hours.

    The drip therefore runs continuously while the response is READ BACK, and the
    assertion is a tight bound: the 413 must arrive within the total drain budget
    plus a small scheduling tolerance. A large tolerance would let the buggy
    implementation (which finishes only when the buffer happens to fill) pass.
    """

    from runner.transports.server import MAX_DRAIN_SECONDS

    h = Harness()
    live = LiveServer(h)
    sock = None
    stop = threading.Event()
    try:
        sock = _open_rejected_request(live, h.token, length=8_000_000)

        def drip() -> None:
            while not stop.is_set():
                try:
                    sock.sendall(b"B" * 8)
                except OSError:
                    return  # server closed on us: the bounded end
                if stop.wait(0.1):
                    return

        started = time.monotonic()
        thread = threading.Thread(target=drip, daemon=True)
        thread.start()

        # Read the response back while the drip is still running.
        status, head = _read_response_head(sock, timeout=MAX_DRAIN_SECONDS + 20.0)
        elapsed = time.monotonic() - started
        stop.set()

        assert status == 413, (status, head)
        # Tight: the budget plus scheduling slack, NOT "whenever a buffer fills".
        assert elapsed < MAX_DRAIN_SECONDS + 3.0, (
            f"drain took {elapsed:.2f}s; the total budget was bypassed by the drip"
        )
    finally:
        stop.set()
        if sock is not None:
            sock.close()
        live.close()
        h.close()

def test_headers_only_under_cap_request_is_dropped_within_the_read_timeout() -> None:
    """The SAME unbounded-wait defect exists on the ordinary read path.

    Here Content-Length is UNDER the cap, so the 413 drain never runs: the
    handler blocks in the normal ``rfile.read(length)``. Without a connection
    timeout that wait is unbounded, so the peer can pin a worker thread by
    announcing a body and never sending it. The read timeout must drop it.
    """

    limits = dict(mock_config().limits)
    limits["request_read_timeout_seconds"] = 2
    h = Harness(config=mock_config(limits=limits))
    live = LiveServer(h)
    sock = None
    try:
        sock = socket.create_connection(("127.0.0.1", live.port), timeout=20)
        head = (
            f"POST /v1/candidates HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{live.port}\r\n"
            f"Authorization: Bearer {h.token}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: 100000\r\n"  # under max_request_body_bytes
            f"\r\n"
        ).encode("ascii")
        sock.sendall(head)  # headers only, body never sent

        sock.settimeout(15.0)
        started = time.monotonic()
        try:
            data = sock.recv(4096)
        except (TimeoutError, socket.timeout):
            raise AssertionError(
                "the connection was never dropped: a peer can pin a worker thread"
            ) from None
        elapsed = time.monotonic() - started
        # Dropped (empty read = closed) rather than served; bounded by the timeout.
        assert data == b"", data[:60]
        assert elapsed < 12.0, elapsed
    finally:
        if sock is not None:
            sock.close()
        live.close()
        h.close()

def test_handler_default_read_timeout_is_finite() -> None:
    """The DEFAULT connection timeout must be a real bound, not ``None``.

    The under-cap stall test below uses a small config override so it does not
    wait 30 seconds; that override would also mask the default being removed.
    This asserts the default itself, so ``timeout = None`` cannot slip through.
    """

    from runner.transports.server import REQUEST_READ_TIMEOUT_SECONDS, _Handler

    assert _Handler.timeout == REQUEST_READ_TIMEOUT_SECONDS, _Handler.timeout
    assert _Handler.timeout is not None and _Handler.timeout > 0, _Handler.timeout

def test_one_byte_per_tenth_second_drip_cannot_bypass_the_drain_budget() -> None:
    """The reviewer's exact repro: 1 byte every 0.1s against a 1 MB declaration.

    With a buffered ``read(65536)`` this never re-checks the deadline: a single
    call needs 65536 drips (~1.8 hours), so the worker is pinned far past the
    2s budget and the 413 only arrives once the peer stops. Here the response
    must come back within the budget while the drip is still running.
    """

    from runner.transports.server import MAX_DRAIN_SECONDS

    h = Harness()
    live = LiveServer(h)
    sock = None
    stop = threading.Event()
    try:
        sock = _open_rejected_request(live, h.token, length=1_000_000)

        def drip_one_byte() -> None:
            while not stop.is_set():
                try:
                    sock.sendall(b"Z")
                except OSError:
                    return
                if stop.wait(0.1):
                    return

        started = time.monotonic()
        thread = threading.Thread(target=drip_one_byte, daemon=True)
        thread.start()

        status, head = _read_response_head(sock, timeout=MAX_DRAIN_SECONDS + 20.0)
        elapsed = time.monotonic() - started
        stop.set()

        assert status == 413, (status, head)
        assert elapsed < MAX_DRAIN_SECONDS + 3.0, (
            f"1-byte/0.1s drip bypassed the total budget: {elapsed:.2f}s"
        )
    finally:
        stop.set()
        if sock is not None:
            sock.close()
        live.close()
        h.close()