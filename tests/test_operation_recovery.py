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
