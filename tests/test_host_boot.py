"""Host boot identity and cross-process handoff (DESIGN.md §4.4).

Two invariants are covered here:

1. Processes in the SAME host boot must share one verifiable node-boot identity,
   because they exchange persisted monotonic deadlines.
2. After a HOST REBOOT the monotonic clock restarts, so a persisted monotonic
   deadline must never be compared. An observation interrupted that way must
   keep its evidence, be marked incomplete, and never be recorded as a normally
   completed 15 s window.
"""

from __future__ import annotations

import json
import os
import tempfile

from harness import Harness, mock_submission

from runner.adapters.build_backend import MockBuildBackend
from runner.adapters.collectors_mock import build_mock_collector_set
from runner.adapters.device_mock import MockDeviceAdapter
from runner.adapters.oracle import MockOracle
from runner.adapters.projection import DiagnosticOnlyProjector
from runner.config import mock_config
from runner.contracts.boot import (
    BOOT_ANCHOR_TOLERANCE_SECONDS,
    boot_id_for_anchor,
    classify_boot_anchor,
    host_boot_anchor,
)
from runner.contracts.clock import FixedClock, SystemClock
from runner.core.service import RunnerService
from runner.core.supervisor import Supervisor
from runner.store.artifacts import ArtifactStore
from runner.store.db import Store
from runner.store.events import EventLog


def _ready(h: Harness, index: int) -> tuple[str, str, dict]:
    sub = h.submit(f"boot-submit-{index:013d}", files=mock_submission())
    build = h.build(f"boot-build-{index:014d}", sub["candidate_id"])
    attempt_id = h.begin(f"boot-begin-{index:014d}", build["build_id"])["attempt_id"]
    return attempt_id, sub["candidate_id"], build


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------
def test_system_clock_identity_is_host_boot_scoped_not_process_random() -> None:
    """Two independent clock instances must agree: same boot, same identity."""

    first, second = SystemClock(), SystemClock()
    assert first.boot_id() == second.boot_id(), (first.boot_id(), second.boot_id())
    assert first.boot_id().startswith("boot-"), first.boot_id()
    # A per-process random value would be 32 hex chars; this must not be that.
    assert len(first.boot_id()) > 8 and not first.boot_id().startswith("boot-fixed")

    # The anchor is the host boot instant and is stable across processes.
    anchor_a, anchor_b = first.boot_anchor(), second.boot_anchor()
    assert anchor_a is not None and anchor_b is not None
    assert abs(float(anchor_a) - float(anchor_b)) < 1.0, (anchor_a, anchor_b)

    # It must also agree with a freshly derived anchor from the raw host clock.
    assert abs(float(anchor_a) - host_boot_anchor()) < 1.0


def test_boot_anchor_classification_detects_reboot_and_tolerates_slew() -> None:
    anchor = 1_700_000_000.0
    assert classify_boot_anchor(None, anchor) == "first_boot"
    # Small drift (process scheduling, NTP slew) stays the same boot.
    assert classify_boot_anchor(anchor, anchor + 30.0) == "same_boot"
    assert classify_boot_anchor(anchor, anchor - 30.0) == "same_boot"
    # A reboot moves the boot instant by at least the down time.
    assert classify_boot_anchor(anchor, anchor + 600.0) == "host_reboot"
    assert BOOT_ANCHOR_TOLERANCE_SECONDS < 600.0
    # The label is deterministic for one boot estimate.
    assert boot_id_for_anchor(anchor) == boot_id_for_anchor(anchor + 1.0)


def test_persisted_boot_identity_is_shared_and_reused_across_processes() -> None:
    """A second 'process' (fresh clock + store handle) adopts the first identity."""

    root = tempfile.mkdtemp(prefix="runner-boot-")
    try:
        clock_a = FixedClock(boot_id="boot-fixed-0001")
        store_a = Store(os.path.join(root, "control.db"), clock=clock_a)
        first = dict(store_a.host_boot)
        assert first["boot_id"] == "boot-fixed-0001"
        store_a.close()

        # A new process in the same boot: it publishes its own anchor estimate,
        # and the store keeps serving the identity that was already active.
        clock_b = FixedClock(boot_id="boot-fixed-0001")
        store_b = Store(os.path.join(root, "control.db"), clock=clock_b)
        second = dict(store_b.host_boot)
        assert second["boot_id"] == first["boot_id"] == "boot-fixed-0001"
        assert float(second["boot_anchor_epoch"]) == float(first["boot_anchor_epoch"])
        # Both processes therefore write events under one node-boot identity.
        assert clock_a.boot_id() == clock_b.boot_id()
        store_b.close()
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


def test_api_and_supervisor_processes_share_one_boot_identity() -> None:
    """The API service and a supervisor built separately must agree."""

    h = Harness()
    try:
        attempt_id, _cand, _build = _ready(h, 1)
        supervisor = h.make_supervisor()
        assert supervisor.clock.boot_id() == h.service.clock.boot_id()
        # Events written by both sides carry the same node_boot_id.
        h.clock.advance(1)
        supervisor.sweep()
        rows = h.service.store.query_all("SELECT DISTINCT node_boot_id FROM events")
        assert len(rows) == 1, [dict(r) for r in rows]
        assert rows[0]["node_boot_id"] == h.service.clock.boot_id()
    finally:
        h.close()


def test_a_new_host_boot_supersedes_the_previous_anchor() -> None:
    root = tempfile.mkdtemp(prefix="runner-boot-")
    try:
        clock = FixedClock(boot_id="boot-fixed-0001")
        store = Store(os.path.join(root, "control.db"), clock=clock)
        assert store.host_boot["boot_id"] == "boot-fixed-0001"

        clock.simulate_host_reboot(epoch=clock.epoch() + 3600.0, monotonic_start=4.0)
        store.refresh_host_boot()

        active = store.active_host_boot()
        assert active is not None
        assert active["boot_id"] != "boot-fixed-0001", active
        assert float(active["boot_anchor_epoch"]) > 1_700_000_000.0
        # Exactly one boot is active; the old one is explicitly superseded.
        superseded = store.query_all("SELECT * FROM host_boot WHERE superseded_utc IS NOT NULL")
        assert len(superseded) == 1 and superseded[0]["boot_id"] == "boot-fixed-0001"
        store.close()
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# interrupted observation
# ---------------------------------------------------------------------------
def test_host_reboot_does_not_record_a_completed_observation_window() -> None:
    """The core requirement: never claim a normal window after a reboot."""

    h = Harness()
    try:
        attempt_id, candidate_id, _build = _ready(h, 2)
        h.launch("boot-launch-00000000001", attempt_id)
        supervisor = h.make_supervisor()

        before = h.service.store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert before["observation_deadline_monotonic"] is not None
        assert before["monotonic_boot_anchor"] is not None
        assert supervisor._monotonic_deadline_is_usable(before) is True

        # The host reboots mid-window: monotonic restarts, wall clock jumps.
        h.clock.simulate_host_reboot(epoch=h.clock.epoch() + 3600.0, monotonic_start=3.0)

        assert supervisor._monotonic_deadline_is_usable(
            h.service.store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        ) is False

        summary = supervisor.sweep()
        assert summary["attempts_interrupted"], summary
        assert summary["attempts_finalized"], summary

        row = h.service.store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert row["state"] == "CLOSED"
        # The window is explicitly NOT complete.
        assert row["observation_window_complete"] == 0, dict(row)
        assert row["execution_status"] == "outcome_unknown", dict(row)
        assert row["reason_code"] == "DEVICE_UNAVAILABLE", dict(row)
        stages = h.service.store.loads(row["stages_json"])
        assert stages["observe"] == "unknown", stages
        # Cleanup cannot be claimed as clean: the device was quarantined across
        # the reboot, so the restore genuinely fails and recovery is not a
        # success. Claiming "completed" here would hide an unverified device.
        assert stages["cleanup"] == "failed", stages
        assert row["recovery_status"] == "failed", dict(row)
        assert row["device_health"] == "unavailable", dict(row)

        # Evidence collected before the reboot is preserved, not discarded.
        assert row["evidence_sealed"] == 1
        assert row["cleanup_sealed"] == 1
        artifacts = h.service.store.query_all(
            "SELECT * FROM artifacts WHERE attempt_id=? AND state='sealed'", (attempt_id,)
        )
        assert artifacts, "sealed evidence must survive the interruption"

        # The device state cannot be trusted across a reboot.
        assert h.device.quarantined is True

        # The interrupted attempt must never be reported as a completed window
        # through any Agent-visible surface.
        status, page = h.get(f"/v1/attempts/{attempt_id}")
        assert status == 200
        assert page["stages"]["observe"] == "unknown", page
        blob = json.dumps(page).lower()
        assert "success" not in blob and "verdict" not in blob

        # The private Oracle still sees the preserved evidence, but the
        # environment is flagged invalid, so it can never return `success` for
        # an observation that a host reboot cut short.
        evaluation = h.service.read_private_oracle(attempt_id)
        assert evaluation is not None
        assert evaluation["verdict"] != "success", dict(evaluation)
        assert evaluation["verdict"] == "inconclusive", dict(evaluation)
        assert "invalid" in (evaluation["reason_private"] or "").lower(), dict(evaluation)
        del candidate_id
    finally:
        h.close()


def test_reboot_interruption_is_recorded_as_an_event_with_both_anchors() -> None:
    h = Harness()
    try:
        attempt_id, _cand, _build = _ready(h, 3)
        h.launch("boot-launch-00000000002", attempt_id)
        supervisor = h.make_supervisor()
        stale_anchor = h.service.store.query_one(
            "SELECT monotonic_boot_anchor FROM attempts WHERE attempt_id=?", (attempt_id,)
        )["monotonic_boot_anchor"]

        h.clock.simulate_host_reboot(epoch=h.clock.epoch() + 7200.0, monotonic_start=2.0)
        supervisor.sweep()

        row = h.service.store.query_one(
            "SELECT * FROM events WHERE attempt_id=? AND event_type='attempt_interrupted_by_host_reboot'",
            (attempt_id,),
        )
        assert row is not None, "interruption must leave an audit event"
        payload = h.service.store.loads(row["payload_json"], {})
        assert payload["observation_window_complete"] is False
        assert float(payload["persisted_boot_anchor"]) == float(stale_anchor)
        assert float(payload["current_boot_anchor"]) != float(stale_anchor)
        # The event itself records which boot emitted it.
        assert row["node_boot_id"] == h.service.clock.boot_id()
    finally:
        h.close()


def test_reboot_during_launch_confirmation_is_not_a_clean_launch_timeout() -> None:
    """An unconfirmed launch interrupted by a reboot is not a normal outcome."""

    h = Harness()
    try:
        attempt_id, _cand, _build = _ready(h, 4)
        # Mark the launch window open without confirming, then reboot.
        h.service.runtime.set_stage(attempt_id, "launch", "running")
        h.service.runtime.update(
            attempt_id,
            launch_deadline_utc=h.service.clock.utc(),
            launch_dispatch_monotonic=h.service.clock.monotonic(),
        )
        supervisor = h.make_supervisor()
        h.clock.simulate_host_reboot(epoch=h.clock.epoch() + 1800.0, monotonic_start=1.0)
        supervisor.sweep()

        row = h.service.store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert row["state"] == "CLOSED"
        # A reboot during confirmation is a device/environment fault, and it must
        # not be laundered into a completed window.
        assert row["observation_window_complete"] == 0
        assert row["reason_code"] in ("DEVICE_UNAVAILABLE", "LAUNCH_TIMEOUT"), dict(row)
    finally:
        h.close()


def test_same_boot_handoff_still_finalizes_normally() -> None:
    """A cross-process takeover in the SAME boot keeps the normal deadline path.

    This is the control for the reboot test: the guard must not turn every
    cross-process handoff into an incomplete observation.
    """

    root = tempfile.mkdtemp(prefix="runner-handoff-")
    try:
        clock = FixedClock()
        config = mock_config()
        device = MockDeviceAdapter()
        store = Store(os.path.join(root, "control.db"), clock=clock)
        artifacts = ArtifactStore(os.path.join(root, "artifacts"), store, EventLog(store, clock=clock), clock=clock)
        events = EventLog(store, clock=clock)
        service = RunnerService(
            root=root,
            config=config,
            store=store,
            clock=clock,
            device=device,
            build_backend=MockBuildBackend(),
            oracle=MockOracle(),
            projector=DiagnosticOnlyProjector(),
            collector_factory=lambda **kw: build_mock_collector_set(**kw),
        )
        session_id, _token = service.create_session(config)
        service.activate_session(session_id, config)

        sub = service.submit_candidate(
            session_id, idempotency_key="handoff-submit-00000001", files_payload=mock_submission()
        )
        build = service.build_candidate(
            session_id, idempotency_key="handoff-build-000000001", candidate_id=sub["candidate_id"]
        )
        receipt = service.begin_attempt(
            session_id, idempotency_key="handoff-begin-000000001", build_id=build["build_id"]
        )
        attempt_id = receipt["attempt_id"]
        service.perform_action(
            session_id, attempt_id=attempt_id, idempotency_key="handoff-launch-00000001",
            action_name="launch_candidate", params={},
        )
        store.close()

        # --- a genuinely separate process takes over in the same host boot ---
        clock2 = FixedClock()  # same boot, fresh process
        store2 = Store(os.path.join(root, "control.db"), clock=clock2)
        artifacts2 = ArtifactStore(
            os.path.join(root, "artifacts"), store2, EventLog(store2, clock=clock2), clock=clock2
        )
        supervisor = Supervisor(
            store=store2,
            artifacts=artifacts2,
            events=EventLog(store2, clock=clock2),
            clock=clock2,
            config=config,
            device=device,
            oracle=MockOracle(),
        )
        # Same boot: the persisted deadline stays comparable.
        row = store2.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert supervisor._monotonic_deadline_is_usable(row) is True

        clock2.advance(30)
        summary = supervisor.sweep()
        assert not summary["attempts_interrupted"], summary
        assert summary["attempts_finalized"], summary

        row2 = store2.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert row2["state"] == "CLOSED"
        # The normal path DOES record a complete observation window.
        assert row2["observation_window_complete"] == 1, dict(row2)
        assert row2["execution_status"] == "completed", dict(row2)
        stages = store2.loads(row2["stages_json"])
        assert stages["observe"] == "completed", stages
        store2.close()
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
