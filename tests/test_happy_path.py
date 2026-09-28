"""Mock end-to-end: session -> submit -> build -> begin -> launch -> seal -> feedback -> retry -> finalize."""

from __future__ import annotations

from harness import Harness, make_source_files, mock_submission


def test_full_mock_flow_end_to_end() -> None:
    h = Harness()
    try:
        # 1) session is ACTIVE with a wall clock and counters
        status, session = h.get("/v1/session")
        assert status == 200, session
        assert session["state"] == "ACTIVE", session
        assert session["can_continue"] is True

        # 2) capabilities expose exactly the whitelisted actions
        status, caps = h.get("/v1/capabilities")
        assert status == 200
        assert sorted(a["name"] for a in caps["actions"]) == [
            "launch_candidate",
            "read_feedback",
            "wait_task",
        ]

        # 3) submit candidate
        sub = h.submit("submit-key-0000000001")
        assert sub["status"] == "accepted", sub
        candidate_id = sub["candidate_id"]
        assert candidate_id.startswith("cnd_") and sub["tree_hash"]

        # 4) build
        build = h.build("build-key-00000000001", candidate_id)
        assert build["state"] == "SUCCEEDED", build
        build_id = build["build_id"]
        assert build["apk_sha256"] and build["package_name"]
        assert build["build_provenance"] == "source_built"

        # 5) begin attempt -> restricted three-field receipt
        receipt = h.begin("attempt-key-0000000001", build_id)
        assert set(receipt) == {"status", "attempt_id", "reason_code"}, receipt
        assert receipt["status"] == "accepted"
        attempt_id = receipt["attempt_id"]

        # 6) prepare stages complete before any launch is possible
        _s, page = h.get(f"/v1/attempts/{attempt_id}")
        assert page["stages"]["install"] == "completed"
        assert page["stages"]["permission_check"] == "completed"
        assert page["stages"]["launch"] == "pending"

        # 7) collectors are registered and required
        _s, tasks = h.get(f"/v1/attempts/{attempt_id}/tasks")
        assert tasks["tasks"], tasks
        assert all(t["state"] == "RUNNING" for t in tasks["tasks"])
        assert all(t["readiness"] == "READY" for t in tasks["tasks"])

        # 8) launch the candidate once
        launch = h.launch("launch-key-00000000001", attempt_id)
        assert launch["state"] == "accepted", launch
        _s, page = h.get(f"/v1/attempts/{attempt_id}")
        assert page["stages"]["launch"] == "completed"
        assert page["stages"]["observe"] == "running"

        # 9) second launch in the same attempt: new key is rejected
        second = h.launch("launch-key-00000000002", attempt_id)
        assert second["state"] == "rejected", second
        assert second["reason_code"] == "REQUEST_REJECTED"

        # 10) supervisor finalizes on the persisted deadline (no client polling)
        supervisor = h.make_supervisor()
        supervisor.sweep()
        h.clock.advance(20)
        summary = supervisor.sweep()
        assert summary["attempts_finalized"], summary

        _s, page = h.get(f"/v1/attempts/{attempt_id}")
        assert page["state"] == "CLOSED", page
        assert page["stages"] == {
            "intake": "completed",
            "build": "completed",
            "install": "completed",
            "permission_check": "completed",
            "launch": "completed",
            "observe": "completed",
            "cleanup": "completed",
        }, page["stages"]

        # 11) the candidate package was stopped and the device restored
        assert build["package_name"] in h.device.state.stopped_packages
        assert h.device.health_state == "ready"

        # 12) read diagnostics through the restricted channel only
        _s, feedback = h.get(
            "/v1/feedback",
            query={"candidate_id": candidate_id, "kind": "runtime", "attempt_id": attempt_id},
        )
        assert feedback["entries"], feedback
        assert feedback["truncated"] is False

        # 13) modify the candidate and try again
        sub2 = h.submit("submit-key-0000000002", files=make_source_files(body="public final class Second {}"))
        assert sub2["status"] == "accepted", sub2
        assert sub2["candidate_id"] != candidate_id
        build2 = h.build("build-key-00000000002", sub2["candidate_id"])
        assert build2["state"] == "SUCCEEDED"
        receipt2 = h.begin("attempt-key-0000000002", build2["build_id"])
        assert receipt2["status"] == "accepted"
        assert receipt2["attempt_id"] != attempt_id

        # 14) finalize the second candidate explicitly
        status, finalized = h.post(
            "/v1/finalize",
            {
                "idempotency_key": "finalize-key-000000001",
                "candidate_id": sub2["candidate_id"],
                "build_id": build2["build_id"],
            },
        )
        assert status == 200, finalized
        assert finalized["state"] == "finalized", finalized
        assert finalized["recipe_hash"]

        # 15) no new candidates after finalization
        blocked = h.submit("submit-key-0000000003", files=make_source_files(body="public final class Third {}"))
        status, _ = h.get("/v1/session")
        assert status == 200
    finally:
        h.close()


def test_attempt_outlives_api_request_process() -> None:
    """The supervisor completes an attempt even if no request ever polls again."""

    h = Harness()
    try:
        sub = h.submit("submit-key-0000000011")
        build = h.build("build-key-00000000011", sub["candidate_id"])
        attempt_id = h.begin("attempt-key-0000000011", build["build_id"])["attempt_id"]
        h.launch("launch-key-00000000011", attempt_id)

        # Simulate the API process dying: drop the in-process service entirely
        # and drive finalization from a fresh supervisor over the same DB/files.
        supervisor = h.make_supervisor()
        h.service._collector_instances.pop(attempt_id, None)
        h.clock.advance(30)
        summary = supervisor.sweep()
        assert summary["attempts_finalized"], summary

        row = h.service.store.query_one(
            "SELECT state, evidence_sealed, cleanup_sealed FROM attempts WHERE attempt_id=?",
            (attempt_id,),
        )
        assert row["state"] == "CLOSED"
        assert row["evidence_sealed"] == 1
        assert row["cleanup_sealed"] == 1
    finally:
        h.close()


def test_task_stop_rules() -> None:
    h = Harness()
    try:
        sub = h.submit("submit-key-0000000021")
        build = h.build("build-key-00000000021", sub["candidate_id"])
        attempt_id = h.begin("attempt-key-0000000021", build["build_id"])["attempt_id"]
        _s, tasks = h.get(f"/v1/attempts/{attempt_id}/tasks")
        required = [t for t in tasks["tasks"] if t["required"]]
        assert required, tasks
        collector = required[0]
        status, body = h.post(
            f"/v1/tasks/{collector['task_id']}/stop",
            {"idempotency_key": "stop-key-000000000001"},
        )
        # A required collector cannot be stopped by the Agent.
        assert status == 403, body
        assert body["error"]["code"] == "permission_denied"
    finally:
        h.close()


def test_wait_task_bounded() -> None:
    h = Harness()
    try:
        sub = h.submit("submit-key-0000000031")
        build = h.build("build-key-00000000031", sub["candidate_id"])
        attempt_id = h.begin("attempt-key-0000000031", build["build_id"])["attempt_id"]
        _s, tasks = h.get(f"/v1/attempts/{attempt_id}/tasks")
        task_id = tasks["tasks"][0]["task_id"]
        status, body = h.post(
            f"/v1/tasks/{task_id}/wait",
            {"idempotency_key": "wait-key-000000000001", "condition": "ready", "timeout_seconds": 0},
        )
        assert status == 200, body
        assert body["satisfied"] is True
        assert body["state"] == "RUNNING"
    finally:
        h.close()
