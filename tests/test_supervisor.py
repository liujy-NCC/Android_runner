"""Supervisor: deadline-driven finalization, crash recovery, quarantine."""

from __future__ import annotations

from harness import Harness, make_source_files
from runner.adapters.device_mock import MockDeviceAdapter


def _ready_attempt(h: Harness, index: int = 1, **kwargs) -> tuple[str, str, dict]:
    sub = h.submit(f"submit-key-{index:013d}", files=make_source_files(body=f"public final class S{index} {{}}"))
    build = h.build(f"build-key-{index:014d}", sub["candidate_id"])
    attempt_id = h.begin(f"attempt-key-{index:013d}", build["build_id"])["attempt_id"]
    return attempt_id, build["build_id"], build


def test_finalize_exactly_at_observation_deadline() -> None:
    h = Harness()
    try:
        attempt_id, _build_id, _build = _ready_attempt(h)
        h.launch("launch-key-0000000501", attempt_id)
        supervisor = h.make_supervisor()

        # Before the deadline nothing is finalized.
        h.clock.advance(5)
        summary = supervisor.sweep()
        assert summary["attempts_finalized"] == [], summary
        row = h.service.store.query_one("SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert row["state"] == "RUNNING"

        # At the deadline the supervisor acts without any client poll.
        h.clock.advance(11)
        summary = supervisor.sweep()
        assert summary["attempts_finalized"], summary
        row = h.service.store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert row["state"] == "CLOSED"
        assert row["observation_window_complete"] == 1
    finally:
        h.close()


def test_seal_happens_before_stop_candidate() -> None:
    """Execution evidence must be sealed before the candidate package stops."""

    order: list[str] = []

    class OrderingDevice(MockDeviceAdapter):
        def stop_package(self, resource_role: str, package_name: str) -> bool:
            order.append("stop_package")
            return super().stop_package(resource_role, package_name)

    device = OrderingDevice()
    h = Harness(device=device)
    try:
        attempt_id, _b, _build = _ready_attempt(h, 2)
        h.launch("launch-key-0000000502", attempt_id)
        supervisor = h.make_supervisor()

        # Observe the artifact rows as they are created.
        original_seal_group = h.service.artifacts.seal_group

        def tracing_seal_group(seal_id, **kwargs):
            row = h.service.store.query_one("SELECT kind FROM seal_groups WHERE seal_id=?", (seal_id,))
            order.append(f"seal:{row['kind']}")
            return original_seal_group(seal_id, **kwargs)

        h.service.artifacts.seal_group = tracing_seal_group  # type: ignore[assignment]
        h.clock.advance(30)
        supervisor.sweep()

        assert "seal:execution_evidence" in order, order
        assert "stop_package" in order, order
        assert order.index("seal:execution_evidence") < order.index("stop_package"), order
    finally:
        h.close()


def test_cleanup_evidence_is_a_separate_object_from_execution_evidence() -> None:
    h = Harness()
    try:
        attempt_id, _b, _build = _ready_attempt(h, 3)
        h.launch("launch-key-0000000503", attempt_id)
        supervisor = h.make_supervisor()
        h.clock.advance(30)
        supervisor.sweep()

        groups = h.service.store.query_all(
            "SELECT kind, state FROM seal_groups WHERE attempt_id=? ORDER BY created_utc", (attempt_id,)
        )
        kinds = [g["kind"] for g in groups]
        assert "execution_evidence" in kinds
        assert "cleanup_evidence" in kinds
        assert all(g["state"] == "sealed" for g in groups), groups

        exec_ids = {
            r["artifact_id"]
            for r in h.service.store.query_all(
                "SELECT artifact_id FROM artifacts WHERE attempt_id=? AND kind='execution_evidence'",
                (attempt_id,),
            )
        }
        cleanup_ids = {
            r["artifact_id"]
            for r in h.service.store.query_all(
                "SELECT artifact_id FROM artifacts WHERE attempt_id=? AND kind='cleanup_evidence'",
                (attempt_id,),
            )
        }
        assert exec_ids and cleanup_ids
        assert not (exec_ids & cleanup_ids), "cleanup must not extend sealed execution evidence"
    finally:
        h.close()


def test_no_launch_within_window_times_out_and_is_recorded() -> None:
    h = Harness()
    try:
        attempt_id, _b, _build = _ready_attempt(h, 4)
        supervisor = h.make_supervisor()
        # The framework's launch window is derived from the attempt deadline.
        h.service.store.execute(
            "UPDATE attempts SET launch_deadline_utc=? WHERE attempt_id=?",
            ("2000-01-01T00:00:00.000Z", attempt_id),
        )
        summary = supervisor.sweep()
        assert summary["attempts_finalized"], summary
        row = h.service.store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert row["state"] == "CLOSED"
        assert row["reason_code"] == "LAUNCH_TIMEOUT", dict(row)
        assert row["execution_status"] == "setup_failed"
    finally:
        h.close()


def test_launch_timeout_when_confirmation_never_arrives() -> None:
    device = MockDeviceAdapter(launch_never_confirms=True)
    h = Harness(device=device)
    try:
        attempt_id, _b, _build = _ready_attempt(h, 5)
        launch = h.launch("launch-key-0000000505", attempt_id)
        assert launch["state"] == "accepted", launch
        assert launch["reason_code"] == "LAUNCH_TIMEOUT"

        _s, page = h.get(f"/v1/attempts/{attempt_id}")
        assert page["stages"]["launch"] == "failed"
        assert page["stages"]["observe"] == "skipped"

        supervisor = h.make_supervisor()
        h.clock.advance(5)
        supervisor.sweep()
        row = h.service.store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert row["state"] == "CLOSED"
        assert row["reason_code"] == "LAUNCH_TIMEOUT"
        # No normal observation window was opened.
        assert row["observation_deadline_monotonic"] is None
    finally:
        h.close()


def test_second_launch_refused_after_unknown_launch_result() -> None:
    """An unknown launch result is never re-triggered in the same attempt."""

    device = MockDeviceAdapter(launch_never_confirms=True)
    h = Harness(device=device)
    try:
        attempt_id, _b, _build = _ready_attempt(h, 6)
        h.launch("launch-key-0000000506", attempt_id)
        second = h.launch("launch-key-0000000507", attempt_id)
        assert second["state"] == "rejected", second
        assert device.state.candidate_launch_count == 1
    finally:
        h.close()


def test_restore_failure_quarantines_device_and_marks_recovery_failed() -> None:
    device = MockDeviceAdapter(restore_fails=True)
    h = Harness(device=device)
    try:
        attempt_id, _b, _build = _ready_attempt(h, 7)
        h.launch("launch-key-0000000508", attempt_id)
        supervisor = h.make_supervisor()
        h.clock.advance(30)
        supervisor.sweep()

        row = h.service.store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert row["state"] == "CLOSED"
        assert row["recovery_status"] == "failed"
        assert row["reason_code"] == "CLEANUP_FAILED"
        assert row["device_health"] == "unavailable"
        assert device.quarantined is True

        # A quarantined device cannot accept a new attempt's preparation.
        sub = h.submit("submit-key-0000000509")
        build = h.build("build-key-00000000509", sub["candidate_id"])
        receipt = h.begin("attempt-key-0000000509", build["build_id"])
        assert receipt["status"] == "accepted", receipt
        row2 = h.service.store.query_one(
            "SELECT * FROM attempts WHERE attempt_id=?", (receipt["attempt_id"],)
        )
        assert row2["reason_code"] == "DEVICE_UNAVAILABLE", dict(row2)
        assert row2["execution_status"] == "setup_failed"
    finally:
        h.close()


def test_stop_failure_prevents_claiming_a_clean_close() -> None:
    order: list[str] = []

    class FailingStopDevice(MockDeviceAdapter):
        def stop_package(self, resource_role: str, package_name: str) -> bool:
            order.append("stop_failed")
            return False

    device = FailingStopDevice()
    h = Harness(device=device)
    try:
        attempt_id, _b, _build = _ready_attempt(h, 8)
        h.launch("launch-key-0000000509", attempt_id)
        supervisor = h.make_supervisor()
        h.clock.advance(30)
        supervisor.sweep()

        row = h.service.store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert row["state"] == "CLOSED"
        # Not verifying the stop means we must not report a healthy recovery.
        assert row["recovery_status"] == "failed"
        assert row["execution_status"] == "device_lost"
        assert device.quarantined is True
    finally:
        h.close()


def test_supervisor_recovers_staging_after_crash() -> None:
    """A staged-but-unpublished object is completed by the next sweep."""

    h = Harness()
    try:
        sub = h.submit("submit-key-0000000510")
        candidate_id = sub["candidate_id"]

        # Craft the crash state: a declared artifact whose staging file exists
        # but which was never published.
        import os

        artifact_id, staging_path = None, None
        with h.service.store.txn():
            artifact_id, staging_path = h.service.artifacts.declare(
                session_id=h.session_id,
                kind="execution_evidence",
                media_type="application/x-ndjson",
                source="collector",
                provenance="device_observed",
                candidate_id=candidate_id,
            )
        with open(staging_path, "wb") as handle:
            handle.write(b'{"line":"interrupted"}\n')

        supervisor = h.make_supervisor()
        summary = supervisor.sweep()
        assert summary["storage"]["staged_completed"] == 1, summary

        row = h.service.store.query_one("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
        assert row["state"] == "sealed", dict(row)
        assert row["content_sha256"]
        assert h.service.artifacts.read_object(artifact_id) == b'{"line":"interrupted"}\n'
    finally:
        h.close()


def test_supervisor_quarantines_orphan_staging_and_object_files() -> None:
    h = Harness()
    try:
        import os

        orphan = os.path.join(h.service.artifacts.staging_dir, "orphan.part")
        with open(orphan, "wb") as handle:
            handle.write(b"orphan")

        supervisor = h.make_supervisor()
        summary = supervisor.sweep()
        assert summary["storage"]["orphan_staging"] == 1, summary
        assert not os.path.exists(orphan)
        quarantined = os.listdir(h.service.artifacts.quarantine_dir)
        assert any(name.startswith("orphan.") for name in quarantined), quarantined
    finally:
        h.close()


def test_missing_object_marks_sealed_artifact_corrupt() -> None:
    h = Harness()
    try:
        import os

        artifact_id = h.service.artifacts.put(
            session_id=h.session_id,
            kind="metadata",
            media_type="text/plain",
            data=b"evidence",
            source="test",
        )
        row = h.service.store.query_one("SELECT content_sha256 FROM artifacts WHERE artifact_id=?", (artifact_id,))
        os.remove(h.service.artifacts.object_path(row["content_sha256"]))

        supervisor = h.make_supervisor()
        summary = supervisor.sweep()
        assert summary["storage"]["corrupt"] >= 1, summary
        row = h.service.store.query_one("SELECT state FROM artifacts WHERE artifact_id=?", (artifact_id,))
        assert row["state"] == "corrupt", dict(row)

        # A corrupt object can never be handed to the Oracle.
        from runner.errors import StorageError

        try:
            h.service.artifacts.read_object(artifact_id)
        except StorageError:
            pass
        else:  # pragma: no cover
            raise AssertionError("corrupt object was readable")
    finally:
        h.close()


def test_supervisor_evaluation_does_not_block_cleanup() -> None:
    """Oracle unavailability leaves recovery complete and records not_evaluated."""

    h = Harness(oracle_unavailable=True)
    try:
        attempt_id, _b, _build = _ready_attempt(h, 9)
        h.launch("launch-key-0000000510", attempt_id)
        supervisor = h.make_supervisor()
        h.clock.advance(30)
        summary = supervisor.sweep()

        assert summary["attempts_finalized"], summary
        row = h.service.store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert row["state"] == "CLOSED"
        assert row["recovery_status"] == "succeeded"
        assert row["oracle_verdict"] == "not_evaluated"
        assert row["evaluation_status"] == "error"
    finally:
        h.close()


def test_abort_marks_window_incomplete_and_still_finalizes() -> None:
    h = Harness()
    try:
        attempt_id, _b, _build = _ready_attempt(h, 10)
        h.launch("launch-key-0000000511", attempt_id)
        _s, body = h.post(
            f"/v1/attempts/{attempt_id}/abort",
            {"idempotency_key": "abort-key-0000000001"},
        )
        assert body.get("observation_window_complete") is False, body
        assert body.get("state") == "FINALIZING", body

        supervisor = h.make_supervisor()
        summary = supervisor.sweep()
        assert summary["attempts_finalized"], summary
        row = h.service.store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert row["state"] == "CLOSED"
        assert row["observation_window_complete"] == 0
    finally:
        h.close()


def test_finish_attempt_cannot_shorten_the_window() -> None:
    h = Harness()
    try:
        attempt_id, _b, _build = _ready_attempt(h, 11)
        h.launch("launch-key-0000000512", attempt_id)
        _s, body = h.post(
            f"/v1/attempts/{attempt_id}/finish", {"idempotency_key": "finish-key-000000001"}
        )
        assert body["accepted"] is False, body
        assert body["reason_code"] == "REQUEST_REJECTED"
        row = h.service.store.query_one("SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert row["state"] == "RUNNING"
    finally:
        h.close()


def test_dangling_operation_is_marked_outcome_unknown_not_replayed() -> None:
    h = Harness()
    try:
        sub = h.submit("submit-key-0000000520")
        # Simulate a crash after a side effect but before the outcome was stored.
        with h.service.store.txn():
            op_id = h.service._operation_create(
                h.session_id,
                "build_candidate",
                state="RUNNING",
                deadline_utc="2000-01-01T00:00:00.000Z",
            )
        supervisor = h.make_supervisor()
        summary = supervisor.sweep()
        assert op_id in summary["dangling_reservations"], summary
        row = h.service.store.query_one("SELECT state FROM operations WHERE operation_id=?", (op_id,))
        assert row["state"] == "OUTCOME_UNKNOWN", dict(row)
    finally:
        h.close()


def test_supervisor_heartbeat_is_recorded() -> None:
    h = Harness()
    try:
        supervisor = h.make_supervisor()
        supervisor.register()
        supervisor.sweep()
        row = h.service.store.query_one("SELECT * FROM supervisor_state")
        assert row is not None and row["heartbeat_utc"]
    finally:
        h.close()
