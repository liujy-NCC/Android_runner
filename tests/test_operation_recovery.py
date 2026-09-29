"""Operation recovery and budget settlement.

The load-bearing rules this module pins down, all from contract §5 and §7:

  * a reservation belongs to ONE operation; reclaiming a dead operation settles
    exactly its own reservations and never the whole session's;
  * liveness is decided by evidence (the operation's own deadline, or a host
    reboot that proves the owner is gone), never by elapsed time alone;
  * a reclaimed operation keeps its terminal state -- a late result from the old
    owner may not overwrite it or settle the budget a second time;
  * an accepted operation is *charged* even when its outcome is unknown;
  * repeated recovery settles exactly once and the counters reconcile with the
    append-only ledger.
"""

from __future__ import annotations

import hashlib

from harness import Harness
from runner.core.budget import C_ACTIONS, C_BUILDS, tree_builds


def _open_operation(h: Harness, *, deadline_utc: str | None, method: str = "build_candidate"):
    """Create a RUNNING operation plus a reservation bound to it."""

    with h.service.store.txn():
        operation_id = h.service._operation_create(
            h.session_id, method, state="RUNNING", deadline_utc=deadline_utc
        )
        reservation_ids = h.service._reserve(
            h.session_id,
            [(C_BUILDS, 1)],
            reason=method,
            ref_type="session",
            ref_id=None,
            operation_id=operation_id,
        )
    return operation_id, reservation_ids


# ---------------------------------------------------------------------------
# 1. Reclaiming an old operation must not disturb a new one
# ---------------------------------------------------------------------------
def test_reclaiming_an_old_operation_does_not_touch_a_new_one() -> None:
    """Only the dead operation's own reservations move; the live one is intact.

    The previous session-wide release refunded every reserved counter in the
    session, so recovering one dead request silently gave back the budget a
    *different, still-running* request was holding. Per-operation reservations
    make that impossible.
    """

    h = Harness()
    try:
        # Dead operation: its own deadline is in the past.
        dead_id, dead_reservations = _open_operation(h, deadline_utc="2000-01-01T00:00:00.000Z")

        # Live operation: no deadline yet, started now, owned by this boot.
        live_id, live_reservations = _open_operation(h, deadline_utc=None)

        before = h.service.store.counter_snapshot(h.session_id)
        assert before["session:builds"]["reserved"] == 2, before

        summary = h.make_supervisor().sweep()

        # Only the dead one is reclaimed.
        assert dead_id in summary["dangling_reservations"], summary
        assert live_id not in summary["dangling_reservations"], summary

        states = {
            row["operation_id"]: row["state"]
            for row in h.service.store.query_all("SELECT operation_id, state FROM operations")
        }
        assert states[dead_id] == "OUTCOME_UNKNOWN", states
        assert states[live_id] == "RUNNING", states

        # The live operation still holds its reservation: reserved goes 2 -> 1,
        # and only the dead operation's unit was charged.
        after = h.service.store.counter_snapshot(h.session_id)
        assert after["session:builds"] == {"limit": 3, "consumed": 1, "reserved": 1}, after

        # Each reservation is attributed to exactly its own operation.
        rows = {
            row["reservation_id"]: row
            for row in h.service.store.query_all(
                "SELECT reservation_id, operation_id, state FROM counter_reservations"
            )
        }
        assert rows[dead_reservations[0]]["operation_id"] == dead_id
        assert rows[dead_reservations[0]]["state"] == "SETTLED"
        assert rows[live_reservations[0]]["operation_id"] == live_id
        assert rows[live_reservations[0]]["state"] == "OPEN"
    finally:
        h.close()


# ---------------------------------------------------------------------------
# 2. A live operation with no deadline must NOT be reclaimed
# ---------------------------------------------------------------------------
def test_live_operation_without_a_deadline_is_never_reclaimed_on_age() -> None:
    """Elapsed time alone is not evidence of death.

    ``deadline_utc IS NULL`` means the stage declared no limit (the generic
    framework allows unset timeouts). Such an operation must survive any amount
    of wall-clock time while its owner is alive in this host boot.
    """

    h = Harness()
    try:
        operation_id, reservations = _open_operation(h, deadline_utc=None)
        assert h.service.store.counter_snapshot(h.session_id)["session:builds"]["reserved"] == 1

        supervisor = h.make_supervisor()
        # Far beyond the old 300 s "must be dead" threshold.
        for seconds in (400, 4000, 40_000):
            h.clock.advance(seconds)
            summary = supervisor.sweep()
            assert operation_id not in summary["dangling_reservations"], (seconds, summary)

            row = h.service.store.query_one(
                "SELECT state FROM operations WHERE operation_id=?", (operation_id,)
            )
            assert row["state"] == "RUNNING", (seconds, dict(row))
            # Its reservation is untouched: not charged, not refunded.
            assert h.service.store.counter_snapshot(h.session_id)["session:builds"] == {
                "limit": 3,
                "consumed": 0,
                "reserved": 1,
            }, seconds
            reservation = h.service.store.reservation_load(reservations[0])
            assert reservation["state"] == "OPEN", dict(reservation)

        # A deadline-exceeded sibling IS reclaimed, proving the sweep is live and
        # that the distinction is the deadline, not inactivity.
        expired_id, _ = _open_operation(h, deadline_utc="2000-01-01T00:00:00.000Z")
        summary = supervisor.sweep()
        assert expired_id in summary["dangling_reservations"], summary
        assert operation_id not in summary["dangling_reservations"], summary
        # The survivor still holds its unit.
        assert h.service.store.counter_snapshot(h.session_id)["session:builds"]["reserved"] == 1
    finally:
        h.close()


# ---------------------------------------------------------------------------
# 3. A late result from a reclaimed owner must not damage the terminal state
# ---------------------------------------------------------------------------
def test_late_result_from_a_reclaimed_owner_cannot_overwrite_state_or_budget() -> None:
    """The first terminal write wins; the late writer must be a no-op.

    This is the two-sided race: the supervisor reclaims an operation because its
    deadline passed, and the (slow, still-alive) owner then tries to finish it
    normally. The reclaimed ``OUTCOME_UNKNOWN`` and its single charge must stand.
    """

    h = Harness()
    try:
        operation_id, reservations = _open_operation(h, deadline_utc="2000-01-01T00:00:00.000Z")

        summary = h.make_supervisor().sweep()
        assert operation_id in summary["dangling_reservations"], summary
        charged = h.service.store.counter_snapshot(h.session_id)
        assert charged["session:builds"] == {"limit": 3, "consumed": 1, "reserved": 0}, charged

        # The old owner finally reports success.
        with h.service.store.txn():
            applied = h.service._operation_finish(
                operation_id, state="SUCCEEDED", result={"build_id": "bld_LATE"}
            )
            # A guarded finisher reports that it did NOT win.
            assert applied is False, applied
            # And the guarded owner skips its settlement, so no second charge.
            h.service._settle(
                h.session_id,
                reserved=[(C_BUILDS, 1)],
                consumed=[(C_BUILDS, 1)],
                reason="build_candidate_succeeded",
                ref_type="build",
                ref_id="bld_LATE",
                reservation_ids=reservations,
            )

        row = h.service.store.query_one(
            "SELECT state, result_json FROM operations WHERE operation_id=?", (operation_id,)
        )
        assert row["state"] == "OUTCOME_UNKNOWN", dict(row)
        assert row["result_json"] is None, dict(row)

        # Budget unchanged: still exactly one charge, nothing refunded.
        after = h.service.store.counter_snapshot(h.session_id)
        assert after == charged, (charged, after)
        charges = h.service.store.query_all(
            "SELECT * FROM counter_ledger WHERE reason='outcome_unknown_charged'"
        )
        assert len(charges) == 1, [dict(r) for r in charges]
        assert h.service.store.query_one(
            "SELECT COUNT(*) AS n FROM counter_ledger WHERE reason='build_candidate_succeeded'"
        )["n"] == 0

        # Repeated recovery after the late result still changes nothing.
        for _ in range(3):
            again = h.make_supervisor().sweep()
            assert operation_id not in again["dangling_reservations"], again
        assert h.service.store.counter_snapshot(h.session_id) == charged
    finally:
        h.close()


# ---------------------------------------------------------------------------
# 4. Repeated recovery settles once; counters reconcile with the ledger
# ---------------------------------------------------------------------------
def test_repeated_recovery_is_settled_once_and_reconciles_with_the_ledger() -> None:
    """N sweeps over the same dead operations produce exactly one settlement."""

    h = Harness()
    try:
        first, _ = _open_operation(h, deadline_utc="2000-01-01T00:00:00.000Z")
        second, _ = _open_operation(h, deadline_utc="2000-01-01T00:00:00.000Z", method="perform_action")

        supervisor = h.make_supervisor()
        summary = supervisor.sweep()
        assert set(summary["dangling_reservations"]) == {first, second}, summary

        snapshot = h.service.store.counter_snapshot(h.session_id)
        ledger_total = h.service.store.query_one("SELECT COUNT(*) AS n FROM counter_ledger")["n"]

        for _ in range(4):
            later = supervisor.sweep()
            assert later["dangling_reservations"] == [], later
        assert h.service.store.counter_snapshot(h.session_id) == snapshot
        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM counter_ledger")["n"] == ledger_total

        # Exactly one charge per operation, and the counters equal the ledger.
        assert h.service.store.query_one(
            "SELECT COUNT(*) AS n FROM counter_reservations WHERE state='SETTLED'"
        )["n"] == 2
        assert h.service.store.query_one(
            "SELECT COUNT(*) AS n FROM counter_reservations WHERE state='OPEN'"
        )["n"] == 0
        for counter_key in snapshot:
            row = h.service.store.query_one(
                "SELECT COALESCE(SUM(delta_reserved),0) AS r, COALESCE(SUM(delta_consumed),0) AS c "
                "FROM counter_ledger WHERE session_id=? AND counter_key=?",
                (h.session_id, counter_key),
            )
            # The ledger's net reserved delta must equal the live reserved value,
            # and the consumed delta must equal the counter's consumed value.
            assert int(row["r"]) == snapshot[counter_key]["reserved"], (counter_key, dict(row))
            assert int(row["c"]) == snapshot[counter_key]["consumed"], (counter_key, dict(row))
    finally:
        h.close()


def test_every_reservation_is_closed_and_reconciles_with_the_ledger() -> None:
    """A normal flow leaves no reservation open, and the ledger reconciles.

    Every reserve must be closed by exactly one settle (charge or refund), so the
    ``OPEN`` set is empty after a completed build+attempt, and each counter's
    counter row equals the sum of its append-only ledger deltas.
    """

    h = Harness()
    try:
        sub = h.submit("recon-submit-00000001")
        built = h.build("recon-build-000000001", sub["candidate_id"])
        assert built["state"] == "SUCCEEDED", built
        accepted = h.begin("recon-begin-000000001", built["build_id"])
        assert accepted["status"] == "accepted", accepted

        open_rows = h.service.store.query_all(
            "SELECT * FROM counter_reservations WHERE state='OPEN'"
        )
        assert open_rows == [], [dict(r) for r in open_rows]
        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM counter_reservations")["n"] > 0

        snapshot = h.service.store.counter_snapshot(h.session_id)
        for counter_key, values in snapshot.items():
            row = h.service.store.query_one(
                "SELECT COALESCE(SUM(delta_reserved),0) AS r, COALESCE(SUM(delta_consumed),0) AS c "
                "FROM counter_ledger WHERE session_id=? AND counter_key=?",
                (h.session_id, counter_key),
            )
            assert int(row["r"]) == values["reserved"], (counter_key, dict(row), values)
            assert int(row["c"]) == values["consumed"], (counter_key, dict(row), values)
    finally:
        h.close()


def test_previous_boot_operation_is_reclaimable_while_same_boot_one_is_not() -> None:
    """A host reboot proves the owner is gone; same-boot liveness does not."""

    h = Harness()
    try:
        same_boot, _ = _open_operation(h, deadline_utc=None)

        # Simulate a genuine host reboot: the monotonic clock restarts and the
        # store rebinds to a new boot anchor.
        h.clock.simulate_host_reboot()
        h.service.store.refresh_host_boot()

        old_boot_id = h.service.store.query_one(
            "SELECT owner_boot_id FROM operations WHERE operation_id=?", (same_boot,)
        )["owner_boot_id"]
        current = str(h.service.store.host_boot.get("boot_id"))
        assert old_boot_id != current, (old_boot_id, current)

        # An operation from the previous boot is provably orphaned.
        summary = h.make_supervisor().sweep()
        assert same_boot in summary["dangling_reservations"], summary
        assert h.service.store.query_one(
            "SELECT state FROM operations WHERE operation_id=?", (same_boot,)
        )["state"] == "OUTCOME_UNKNOWN"
    finally:
        h.close()


# ---------------------------------------------------------------------------
# 5. A configured stage timeout must reach the operation record
# ---------------------------------------------------------------------------
def test_configured_build_timeout_is_recorded_on_the_operation() -> None:
    """A configured ``build`` timeout must bound the operation, not just the build.

    The supervisor reclaims on evidence, and the operation's own ``deadline_utc``
    is that evidence. If the configured stage timeout is written to ``builds`` but
    not to ``operations``, a crashed build is unreclaimable even though the
    operator explicitly configured a limit.
    """

    from runner.config import mock_config

    config = mock_config()
    config.timeouts = {**config.timeouts, "build": 1.0}
    h = Harness(config=config)
    try:
        sub = h.submit("deadline-submit-000001")
        built = h.build("deadline-build-000001", sub["candidate_id"])
        assert built["state"] == "SUCCEEDED", built

        row = h.service.store.query_one(
            "SELECT * FROM operations WHERE method='build_candidate'"
        )
        assert row["deadline_utc"] is not None, dict(row)
        build_row = h.service.store.query_one(
            "SELECT attempt_deadline_utc FROM builds WHERE build_id=?", (built["build_id"],)
        )
        # Same instant as the build row's own deadline: one configured timeout.
        assert row["deadline_utc"] == build_row["attempt_deadline_utc"], (
            row["deadline_utc"],
            build_row["attempt_deadline_utc"],
        )
    finally:
        h.close()


def test_expired_configured_build_timeout_lets_the_supervisor_reclaim() -> None:
    """With the deadline propagated, an abandoned build IS reclaimed.

    End-to-end version of the review finding: configure a 1 s build timeout,
    advance past it, and the supervisor must reclaim the still-open build
    operation (previously it never would, because ``deadline_utc`` was NULL).
    """

    import json as _json

    from runner.config import mock_config

    config = mock_config()
    config.timeouts = {**config.timeouts, "build": 1.0}
    h = Harness(config=config)
    try:
        sub = h.submit("expire-submit-0000001")
        # Freeze the build mid-flight: the backend never returns, so the
        # operation stays RUNNING exactly as a crashed build would.
        marker: dict[str, str] = {}

        def _capture(request, *, limits):
            row = h.service.store.query_one(
                "SELECT operation_id FROM operations WHERE method='build_candidate'"
            )
            marker["operation_id"] = row["operation_id"]
            raise RuntimeError("simulated mid-build process death")

        h.build_backend.build = _capture  # type: ignore[method-assign]
        try:
            h.build("expire-build-00000001", sub["candidate_id"])
        except Exception:
            pass  # the injected fault is expected

        operation_id = marker["operation_id"]
        assert (
            h.service.store.query_one(
                "SELECT deadline_utc FROM operations WHERE operation_id=?", (operation_id,)
            )["deadline_utc"]
            is not None
        )

        # Re-open the row the fault left behind, then advance past the deadline.
        with h.service.store.txn():
            h.service.store.execute(
                "UPDATE operations SET state='RUNNING' WHERE operation_id=?", (operation_id,)
            )
        h.clock.advance(2.0)
        summary = h.make_supervisor().sweep()
        assert operation_id in summary["dangling_reservations"], summary
        assert h.service.store.query_one(
            "SELECT state FROM operations WHERE operation_id=?", (operation_id,)
        )["state"] == "OUTCOME_UNKNOWN"
        # Charged, not refunded (accepted => billed).
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:builds"] == {"limit": 3, "consumed": 1, "reserved": 0}, snapshot
    finally:
        h.close()


# ---------------------------------------------------------------------------
# 6. The FULL build path must not publish a result after being reclaimed
# ---------------------------------------------------------------------------
class _ReclaimingBuildBackend:
    """Reclaims its own operation mid-build, then returns a normal outcome.

    This reproduces the real interleaving deterministically: the supervisor
    reclaims the operation while the build is still running, and only then does
    the (slow but alive) owner come back with a result. Driving the public
    ``build_candidate`` path is the point -- calling the internal finalizers
    directly is what let the original defect hide.
    """

    name = "reclaiming"
    version = "1"

    def __init__(self, service, clock, *, ok: bool | None) -> None:
        self.service = service
        self.clock = clock
        self.ok = ok
        self.calls = 0

    def build(self, request, *, limits):
        from runner.adapters.base import BuildError, BuildOutcome
        from runner.core.supervisor import Supervisor

        self.calls += 1
        # The configured build timeout is now on the operation record, so
        # advancing just past it is enough evidence for the supervisor to
        # reclaim. Kept well under the session wall clock on purpose: this test
        # is about operation reclaim, not about session expiry.
        self.clock.advance(2.0)
        Supervisor(
            store=self.service.store,
            artifacts=self.service.artifacts,
            events=self.service.events,
            clock=self.clock,
            config=self.service.config,
            device=self.service.device,
            oracle=self.service.oracle,
        ).sweep()

        if self.ok is None:
            # Third entry point: the backend raises after the operation was
            # already reclaimed, exercising the unexpected-adapter-failure path
            # rather than a normal success/failure outcome.
            raise RuntimeError("backend failed after the operation was reclaimed")

        if self.ok:
            apk = b"late apk bytes"
            return BuildOutcome(
                ok=True,
                exit_code=0,
                log_text="late success\n",
                apk_bytes=apk,
                apk_sha256=hashlib.sha256(apk).hexdigest(),
                package_name="org.androidpoc.candidate.late",
                version_code=1,
                template_id=request.build_profile,
                toolchain_id="late-toolchain",
                manifest_sha256="0" * 64,
                permissions=[],
                components=[],
                queries=[],
                entry_component=".LateActivity",
                application_id=request.application_id,
                signer_fingerprint="late-signer",
            )
        return BuildOutcome(
            ok=False,
            exit_code=1,
            log_text="late failure\n",
            errors=[BuildError("src/A.java", 1, 1, "late failure", "compile_error")],
            failure_reason_code="BUILD_FAILED",
        )

    def cancel(self, request):  # pragma: no cover - no async work
        return None


def _run_reclaimed_full_build(*, ok: bool | None) -> None:
    """Drive the public build path with the owner reclaimed mid-build."""

    from runner.config import mock_config

    config = mock_config()
    # A configured stage timeout is what gives recovery the evidence it needs;
    # this also exercises the deadline propagation fix.
    config.timeouts = {**config.timeouts, "build": 1.0}
    h = Harness(config=config)
    try:
        sub = h.submit("late-submit-000000001")
        h.service.build_backend = _ReclaimingBuildBackend(h.service, h.clock, ok=ok)

        response = h.build("late-build-000000001", sub["candidate_id"])
        build_id = response["build_id"]

        # The operation was reclaimed, and it stays reclaimed.
        operation = h.service.store.query_one(
            "SELECT * FROM operations WHERE method='build_candidate'"
        )
        assert operation["state"] == "OUTCOME_UNKNOWN", dict(operation)

        # The build must NOT be published as a success, and no APK may exist.
        build = h.service.store.query_one("SELECT * FROM builds WHERE build_id=?", (build_id,))
        assert build["state"] == "CANCELLED", dict(build)
        assert build["apk_artifact_id"] is None, dict(build)
        assert build["apk_sha256"] is None, dict(build)
        assert response["state"] == "CANCELLED", response
        assert response["apk_artifact_id"] is None, response
        assert response["apk_sha256"] is None, response

        apks = h.service.store.query_all(
            "SELECT * FROM artifacts WHERE kind='candidate_apk' AND build_id=?", (build_id,)
        )
        assert apks == [], [dict(a) for a in apks]

        # No success/failure event was published for the reclaimed build.
        for event_type in ("build_succeeded", "build_failed"):
            n = h.service.store.query_one(
                "SELECT COUNT(*) AS n FROM events WHERE event_type=? AND build_id=?",
                (event_type, build_id),
            )["n"]
            assert n == 0, (event_type, n)
        assert h.service.store.query_one(
            "SELECT COUNT(*) AS n FROM events WHERE event_type='build_reclaimed' AND build_id=?",
            (build_id,),
        )["n"] == 1

        # The idempotency receipt replays the same honest state, not a success.
        replay = h.build("late-build-000000001", sub["candidate_id"])
        assert replay["state"] == "CANCELLED", replay
        assert replay["apk_artifact_id"] is None, replay

        # Exactly one charge per counter (from recovery), never a second from the
        # late owner re-settling the same reservations.
        charges = h.service.store.query_all(
            "SELECT * FROM counter_ledger WHERE reason='outcome_unknown_charged'"
        )
        assert len(charges) == 2, [dict(r) for r in charges]  # session:builds + tree:builds
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:builds"] == {"limit": 3, "consumed": 1, "reserved": 0}, snapshot
        assert h.service.store.query_one(
            "SELECT COUNT(*) AS n FROM counter_reservations WHERE state='OPEN'"
        )["n"] == 0

        # A CANCELLED build can never start an attempt.
        begun = h.begin("late-begin-0000000001", build_id)
        assert begun["status"] == "rejected", begun
        assert begun["attempt_id"] is None, begun
    finally:
        h.close()


def test_late_success_cannot_publish_a_build_after_reclaim() -> None:
    """A SUCCEEDED outcome arriving after reclaim must not ship an APK."""

    _run_reclaimed_full_build(ok=True)


def test_late_failure_cannot_publish_diagnostics_after_reclaim() -> None:
    """Same rule for the failure path: no FAILED build, no diagnostics."""

    _run_reclaimed_full_build(ok=False)


def test_backend_error_after_reclaim_still_converges_to_cancelled() -> None:
    """A raising backend must not bypass the reclaimed-operation guard.

    Third entry point into the same decision: the unexpected-adapter-failure
    path (previously a bare HTTP 500) also has to respect a reclaimed operation
    instead of writing a FAILED build and its diagnostics.
    """

    _run_reclaimed_full_build(ok=None)
