"""Permission boundary: no generic shell / ADB / filesystem / raw evidence surface."""

from __future__ import annotations

from harness import Harness

# Endpoint classes that must never exist.
FORBIDDEN_ENDPOINTS = (
    "/v1/exec",
    "/v1/shell",
    "/v1/powershell",
    "/v1/adb",
    "/v1/adb/shell",
    "/v1/emulator",
    "/v1/devices",
    "/v1/files",
    "/v1/files/read",
    "/v1/fs",
    "/v1/plugins",
    "/v1/config",
    "/v1/admin/session",
    "/v1/oracle",
    "/v1/oracle/verdict",
    "/v1/evidence",
    "/v1/artifacts",
    "/v1/artifacts/art_00000000000000000000000000",
    "/v1/logcat",
    "/v1/db",
    "/v1/sqlite",
    "/v1/install",
    "/v1/uninstall",
    "/v1/intent",
    "/v1/raw",
    "/exec",
    "/shell",
)


def test_forbidden_endpoints_are_absent() -> None:
    h = Harness()
    try:
        for path in FORBIDDEN_ENDPOINTS:
            for method in ("GET", "POST"):
                status, body = h.call(method, path, body={"idempotency_key": "forbidden-key-00000001"})
                assert status == 404, (method, path, status, body)
    finally:
        h.close()


def test_unauthenticated_requests_are_refused() -> None:
    h = Harness()
    try:
        status, body = h.get("/v1/session", token="")
        assert status == 401, body
        status, body = h.get("/v1/session", token="deadbeef" * 8)
        assert status == 401, body
        assert body["error"]["code"] == "unauthenticated"
    finally:
        h.close()


def test_session_id_must_match_credential() -> None:
    h = Harness()
    try:
        other_id, _other_token = h.service.create_session(h.config)
        status, body = h.call(
            "GET",
            "/v1/session",
            body={},
            query={"session_id": other_id},
        )
        assert status == 403, body
        assert body["error"]["code"] == "session_mismatch"
    finally:
        h.close()


def test_credential_scope_limits_methods() -> None:
    """A credential scoped to reads cannot perform mutations."""

    h = Harness()
    try:
        from runner.core.sessions import SessionService

        session_id, token = h.service.sessions.create(
            h.config, allowed_methods=("get_session", "get_capabilities")
        )
        h.service.sessions.activate(session_id, config=h.config)
        status, body = h.call("GET", "/v1/session", body={}, token=token, session_id=session_id)
        assert status == 200, body

        status, body = h.call(
            "POST",
            "/v1/candidates",
            body={"idempotency_key": "scoped-key-0000000001", "files": []},
            token=token,
            session_id=session_id,
        )
        assert status == 403, body
        assert body["error"]["code"] == "permission_denied"
    finally:
        h.close()


def test_cross_session_object_ids_are_refused() -> None:
    """Guessing an ID from another session grants nothing."""

    a = Harness()
    b = Harness()
    try:
        sub = a.submit("submit-key-0000000101")
        build = a.build("build-key-00000000101", sub["candidate_id"])
        attempt_id = a.begin("attempt-key-0000000101", build["build_id"])["attempt_id"]

        # B's credential cannot read A's attempt, build or candidate.
        status, body = b.get(f"/v1/attempts/{attempt_id}")
        assert status == 404, body
        status, body = b.get(f"/v1/operations/{'op_' + 'A' * 26}")
        assert status == 404, body
        status, body = b.post(
            "/v1/builds",
            {"idempotency_key": "cross-key-0000000001", "candidate_id": sub["candidate_id"]},
        )
        assert status == 404, body
        status, body = b.post(
            "/v1/finalize",
            {
                "idempotency_key": "cross-key-0000000002",
                "candidate_id": sub["candidate_id"],
                "build_id": build["build_id"],
            },
        )
        assert status == 400, body
    finally:
        a.close()
        b.close()


def test_launch_candidate_rejects_intent_and_component_params() -> None:
    h = Harness()
    try:
        sub = h.submit("submit-key-0000000111")
        build = h.build("build-key-00000000111", sub["candidate_id"])
        attempt_id = h.begin("attempt-key-0000000111", build["build_id"])["attempt_id"]

        # The action must never degrade into a generic Intent / component launch.
        for params in (
            {"component": "com.asus.aihome/.CommandService"},
            {"intent": {"action": "android.intent.action.VIEW"}},
            {"extras": {"uri": "https://example.invalid"}},
            {"package": "com.asus.aihome"},
            {"serial": "emulator-5554"},
            {"command": "am start -n com.x/.Y"},
            {"action": "android.intent.action.VIEW"},
        ):
            status, body = h.post(
                f"/v1/attempts/{attempt_id}/actions",
                {
                    "idempotency_key": "launch-param-key-0000001",
                    "action_name": "launch_candidate",
                    "params": params,
                },
            )
            assert status == 400, (params, status, body)

        # Unknown action names are refused before any device contact.
        status, body = h.post(
            f"/v1/attempts/{attempt_id}/actions",
            {"idempotency_key": "launch-param-key-0000002", "action_name": "adb_shell", "params": {}},
        )
        assert status == 422, body
        assert body["error"]["code"] == "unsupported_capability"
    finally:
        h.close()


def test_http_methods_are_restricted() -> None:
    h = Harness()
    try:
        status, body = h.call("DELETE", "/v1/session", body={})
        assert status in (404, 405), body
        status, body = h.call("PUT", "/v1/candidates", body={})
        assert status == 405, body
        assert body["error"]["code"] == "method_not_allowed"
    finally:
        h.close()


def test_mutation_requires_idempotency_key() -> None:
    h = Harness()
    try:
        status, body = h.post("/v1/candidates", {"files": []})
        assert status == 400, body
        assert body["error"]["code"] == "malformed_request"

        status, body = h.post("/v1/candidates", {"idempotency_key": "short", "files": []})
        assert status == 400, body
    finally:
        h.close()


def test_oversized_request_body_is_refused() -> None:
    import base64

    h = Harness()
    try:
        blob = base64.b64encode(b"A" * 300_000).decode("ascii")
        status, body = h.post(
            "/v1/candidates",
            {
                "idempotency_key": "oversize-key-000000001",
                "files": [{"relative_path": "big.txt", "content_base64": blob}],
            },
        )
        assert status == 413, body
        assert body["error"]["code"] == "payload_too_large"
    finally:
        h.close()


def test_non_loopback_bind_is_refused() -> None:
    from runner.transports.server import make_server

    h = Harness()
    try:
        try:
            make_server(h.api, h.config, host="0.0.0.0", port=0)
        except ValueError as exc:
            assert "non-loopback" in str(exc), exc
        else:  # pragma: no cover
            raise AssertionError("non-loopback bind should have been refused")
    finally:
        h.close()


def test_rejections_are_not_charged_and_are_ledgered() -> None:
    h = Harness()
    try:
        before = h.service.store.counter_snapshot(h.session_id)
        _s, session_before = h.get("/v1/session")
        for _ in range(3):
            h.post("/v1/candidates", {"files": []})  # malformed: pre-acceptance reject
        _s, session_after = h.get("/v1/session")
        assert (
            session_after["budget_remaining"]["distinct_candidates_remaining"]
            == session_before["budget_remaining"]["distinct_candidates_remaining"]
        )
        after = h.service.store.counter_snapshot(h.session_id)
        assert after["session:distinct_candidates"]["consumed"] == before["session:distinct_candidates"]["consumed"]
        count = h.service.store.query_one(
            "SELECT COUNT(*) AS n FROM rejections WHERE session_id=?", (h.session_id,)
        )
        assert count["n"] >= 3
    finally:
        h.close()
