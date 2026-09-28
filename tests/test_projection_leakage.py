"""Oracle privacy: private conclusions must not leak through ANY Agent-visible interface.

This is the central negative-control suite. It scans every Agent-reachable
response body for private markers (ASUS identity, fixed viewer identity, the
private URI, Intent content, non-candidate UIDs, Oracle verdicts, repaired-state
results and raw logcat) and fails on the first occurrence.
"""

from __future__ import annotations

import json

from harness import Harness, make_source_files

# Markers that must never appear in any Agent-visible response.
PRIVATE_MARKERS = (
    "com.asus.aihome",
    "com.fixed.viewer",
    "asus",
    "viewer",
    "oracle-private.example",
    "secret-uri",
    "ACTION_VIEW",
    "action_view",
    "CommandService",
    "success",
    "not_evaluated",
    "inconclusive",
    "verdict",
    "repaired",
    "exported=false",
    "oracle_verdict",
    "viewer_received",
    "action_view_requested",
    "10094",
)

# Field names that must never appear in an Agent-visible payload.
FORBIDDEN_FIELDS = (
    "oracle_verdict",
    "oracle_evaluation",
    "evaluation_status",
    "observations",
    "verdict",
    "viewer_received",
    "action_view_requested",
    "raw_logcat",
    "private_reason",
)


def _walk(node, path="$"):
    """Yield (path, key_or_index, value) for every node in a JSON structure."""

    if isinstance(node, dict):
        for key, value in node.items():
            yield f"{path}.{key}", key, value
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield f"{path}[{index}]", index, value
            yield from _walk(value, f"{path}[{index}]")


def _assert_clean(label: str, payload) -> None:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).lower()
    for marker in PRIVATE_MARKERS:
        assert marker.lower() not in blob, f"{label}: private marker {marker!r} leaked: {blob[:600]}"
    for _, key, value in _walk(payload):
        if isinstance(key, str):
            assert key.lower() not in FORBIDDEN_FIELDS, f"{label}: forbidden field {key!r}"
        if isinstance(value, str):
            lowered = value.lower()
            for field in FORBIDDEN_FIELDS:
                assert field not in lowered, f"{label}: forbidden value {value!r}"


def _full_run(h: Harness, index: int = 1) -> tuple[str, str, str]:
    sub = h.submit(f"submit-key-{index:013d}", files=make_source_files(body=f"public final class L{index} {{}}"))
    build = h.build(f"build-key-{index:014d}", sub["candidate_id"])
    attempt_id = h.begin(f"attempt-key-{index:013d}", build["build_id"])["attempt_id"]
    h.launch(f"launch-key-{index:013d}", attempt_id)
    supervisor = h.make_supervisor()
    h.clock.advance(30)
    supervisor.sweep()
    return sub["candidate_id"], build["build_id"], attempt_id


def test_oracle_verdict_never_appears_in_any_agent_visible_response() -> None:
    h = Harness(oracle_scenario="present_present")
    try:
        candidate_id, build_id, attempt_id = _full_run(h)

        # The private evaluation exists and says success.
        private = h.service.read_private_oracle(attempt_id)
        assert private is not None and private["verdict"] == "success", private

        responses = {
            "session": h.get("/v1/session")[1],
            "capabilities": h.get("/v1/capabilities")[1],
            "attempt": h.get(f"/v1/attempts/{attempt_id}")[1],
            "attempts_list": h.get(f"/v1/sessions/{h.session_id}/attempts")[1],
            "tasks": h.get(f"/v1/attempts/{attempt_id}/tasks")[1],
            "feedback_runtime": h.get(
                "/v1/feedback",
                query={"candidate_id": candidate_id, "kind": "runtime", "attempt_id": attempt_id},
            )[1],
            "feedback_build": h.get(
                "/v1/feedback", query={"candidate_id": candidate_id, "kind": "build", "build_id": build_id}
            )[1],
            "build_replay": h.post(
                "/v1/builds", {"idempotency_key": "build-key-0000000141", "candidate_id": candidate_id}
            )[1],
        }
        tasks = responses["tasks"]["tasks"]
        for task in tasks:
            responses[f"task:{task['task_id']}"] = h.get(f"/v1/tasks/{task['task_id']}")[1]

        for label, payload in responses.items():
            _assert_clean(label, payload)
    finally:
        h.close()


def test_verdict_does_not_change_any_agent_visible_byte() -> None:
    """The Agent-visible projection must not branch on the private verdict."""

    def run(scenario: str) -> dict:
        h = Harness(oracle_scenario=scenario)
        try:
            sub = h.submit("submit-key-0000000701")
            build = h.build("build-key-00000000701", sub["candidate_id"])
            attempt_id = h.begin("attempt-key-0000000701", build["build_id"])["attempt_id"]
            h.launch("launch-key-0000000701", attempt_id)
            supervisor = h.make_supervisor()
            h.clock.advance(30)
            supervisor.sweep()

            private = h.service.read_private_oracle(attempt_id)
            return {
                "private_verdict": private["verdict"],
                "attempt": h.get(f"/v1/attempts/{attempt_id}")[1],
                "feedback": h.get(
                    "/v1/feedback",
                    query={"candidate_id": sub["candidate_id"], "kind": "runtime", "attempt_id": attempt_id},
                )[1],
            }
        finally:
            h.close()

    success = run("present_present")
    failure = run("absent_absent")
    inconclusive = run("unknown_viewer")

    assert success["private_verdict"] == "success"
    assert failure["private_verdict"] == "failure"
    assert inconclusive["private_verdict"] == "inconclusive"

    # Normalize only inherently per-run volatile values (fresh ids, derived
    # package/uid, stream ordinals). Everything the verdict could possibly leak
    # through -- messages, levels, event types, counts, flags -- must match.
    _VOLATILE = ("ordinal", "candidate_package", "candidate_uid", "diagnostic_id")

    def strip(payload: dict) -> dict:
        clean = json.loads(json.dumps(payload))
        clean.pop("attempt_id", None)
        clean.pop("candidate_id", None)
        clean.pop("next_cursor", None)
        for entry in clean.get("entries", []):
            for key in _VOLATILE:
                entry.pop(key, None)
        if "entries" in clean:
            # Ordinals are positions in the raw private stream, so they shift
            # with per-run evidence sizes. Compare the delivered *set* of
            # diagnostics: same count, same content, no verdict-dependent extra.
            clean["entries"] = sorted(
                clean["entries"], key=lambda e: json.dumps(e, sort_keys=True)
            )
        return clean

    assert strip(success["attempt"]) == strip(failure["attempt"])
    assert strip(success["attempt"]) == strip(inconclusive["attempt"])
    assert strip(success["feedback"]) == strip(failure["feedback"])
    assert strip(success["feedback"]) == strip(inconclusive["feedback"])
    for payload in (success["attempt"], failure["attempt"], inconclusive["attempt"]):
        _assert_clean("verdict-invariance", payload)


def test_begin_attempt_receipt_has_exactly_three_business_fields() -> None:
    h = Harness()
    try:
        sub = h.submit("submit-key-0000000702")
        build = h.build("build-key-00000000702", sub["candidate_id"])
        receipt = h.begin("attempt-key-0000000702", build["build_id"])
        assert set(receipt) == {"status", "attempt_id", "reason_code"}, receipt

        # Rejected receipts are equally restricted.
        rejected = h.begin("attempt-key-0000000703", "bld_" + "A" * 26)
        assert set(rejected) == {"status", "attempt_id", "reason_code"}, rejected
        assert rejected["attempt_id"] is None
    finally:
        h.close()


def test_raw_logcat_and_private_system_events_are_absent_from_feedback() -> None:
    h = Harness()
    try:
        candidate_id, _b, attempt_id = _full_run(h, 2)
        _s, feedback = h.get(
            "/v1/feedback",
            query={"candidate_id": candidate_id, "kind": "runtime", "attempt_id": attempt_id},
        )
        assert feedback["entries"], feedback

        blob = json.dumps(feedback, ensure_ascii=False).lower()
        # The private system events exist in the evidence but never in feedback.
        assert "ActivityTaskManager".lower() not in blob
        assert "Permission Denial".lower() not in blob
        assert "com.asus" not in blob
        assert "com.fixed.viewer" not in blob
        assert "oracle-private.example" not in blob
        assert "uid=10094" not in blob

        # The safe structured event did come through.
        kinds = {entry["kind"] for entry in feedback["entries"]}
        assert kinds, kinds
        assert "candidate_anr" in blob or "candidate_process_error" in blob
    finally:
        h.close()


def test_unattributable_and_unsanitizable_events_are_dropped_with_incomplete_flag() -> None:
    h = Harness()
    try:
        candidate_id, _b, attempt_id = _full_run(h, 3)
        _s, feedback = h.get(
            "/v1/feedback",
            query={"candidate_id": candidate_id, "kind": "runtime", "attempt_id": attempt_id},
        )
        # The mock evidence contains an unattributable system event, an
        # out-of-whitelist event type and a private-marker event, so the flag
        # must be set and no reason for the omission may be exposed.
        assert feedback["diagnostics_incomplete"] is True, feedback
        assert "reason" not in json.dumps(feedback).lower() or "truncation_reason" in feedback
        for entry in feedback["entries"]:
            assert "oracle_private_observation" not in json.dumps(entry)
    finally:
        h.close()


def test_private_verdict_is_not_reachable_over_http() -> None:
    h = Harness()
    try:
        _c, _b, attempt_id = _full_run(h, 4)
        for path in (
            f"/v1/attempts/{attempt_id}/oracle",
            f"/v1/oracle/{attempt_id}",
            f"/v1/attempts/{attempt_id}/verdict",
            f"/v1/attempts/{attempt_id}/observations",
            f"/v1/attempts/{attempt_id}/evidence",
        ):
            status, body = h.get(path)
            assert status == 404, (path, status, body)
    finally:
        h.close()


def test_operation_and_task_views_carry_no_diagnostics_body() -> None:
    h = Harness(build_fails=True)
    try:
        sub = h.submit("submit-key-0000000704")
        build = h.build("build-key-00000000704", sub["candidate_id"])
        assert build["state"] == "FAILED"

        # The failed build's diagnostics are reachable only via read_feedback;
        # the operation view must not include the error text.
        _s, operation = h.get(f"/v1/operations/{build.get('operation_id') or 'op_' + 'A' * 26}")
        # operation_id is not in the restricted build response by design, so the
        # lookup above may 404; assert either way that no diagnostics body leaks.
        _assert_clean("operation", operation)

        _s, build_replay = h.post(
            "/v1/builds", {"idempotency_key": "build-key-0000000704", "candidate_id": sub["candidate_id"]}
        )
        _assert_clean("build_replay", build_replay)
        blob = json.dumps(build_replay).lower()
        assert "cannot find symbol" not in blob
        assert "compile" not in blob
    finally:
        h.close()


def test_rejection_ledger_does_not_leak_into_responses() -> None:
    h = Harness()
    try:
        for _ in range(5):
            status, body = h.post("/v1/candidates", {"files": []})
            assert status == 400
        # The rejection detail stored privately is never echoed.
        blob = json.dumps(body).lower()
        assert "traceback" not in blob
        assert "sqlite" not in blob
        assert ".py" not in blob
        assert "detail" not in blob
    finally:
        h.close()


def test_errors_never_include_internal_paths_or_stack_traces() -> None:
    h = Harness()
    try:
        probes = [
            ("POST", "/v1/candidates", {"idempotency_key": "probe-key-" + "0" * 12, "files": "not-a-list"}),
            ("POST", "/v1/builds", {"idempotency_key": "probe-key-" + "1" * 12, "candidate_id": 12345}),
            ("POST", "/v1/attempts", {"idempotency_key": "probe-key-" + "2" * 12, "build_id": ["x"]}),
            ("GET", "/v1/feedback", {}),
        ]
        for method, path, body in probes:
            status, response = h.call(method, path, body=body)
            assert status >= 400, (path, status, response)
            blob = json.dumps(response, ensure_ascii=False)
            for token in ("Traceback", "sqlite3", "\\\\", "D:\\\\", "/home/", "runner/", ".py", "SELECT "):
                assert token not in blob, (path, token, blob)
            assert set(response) == {"error"}, response
            assert set(response["error"]) == {"code", "message"}, response
    finally:
        h.close()
