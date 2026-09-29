"""Scenario A: concurrent contention for the LAST remaining budget slot.

Complements ``test_budget`` / ``test_budget_rejection`` / ``test_idempotency``
by pinning the *boundary* case: exactly one slot left, several callers arriving
together. The existing suites cover an already-exhausted budget; this module
covers the race that decides who gets the last unit.

Every test asserts the four observable surfaces the contract makes load-bearing:

  * SQLite rows (candidates / builds / attempts / counters / idempotency);
  * the budget counters (``consumed`` / ``reserved``);
  * real adapter side-effect counts (build backend, device launch);
  * the HTTP/API-level response body and shape.

Concurrency is driven by an explicit ``threading.Barrier`` plus a store-level
gate where a specific interleaving must be forced; no test relies on sleeping
until a race "probably" happens.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

from harness import Harness
from runner.config import mock_config


def distinct_submission(index: int) -> list[dict[str, str]]:
    """A submission whose frozen tree hash is unique per ``index``."""

    import base64

    return [
        {
            "relative_path": f"candidate/behaviour-{index}.txt",
            "content_base64": base64.b64encode(f"candidate source {index}\n".encode()).decode("ascii"),
        }
    ]


def _race(workers: int, call: Callable[[int], Any], *, timeout: float = 60.0) -> tuple[list[Any], list[str]]:
    """Run ``call(i)`` in ``workers`` threads released by one barrier."""

    barrier = threading.Barrier(workers)
    results: list[Any] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=20)
            value = call(index)
        except Exception as exc:  # a 500 or a raised error surfaces here
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")
        else:
            with lock:
                results.append(value)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    return results, errors


# ---------------------------------------------------------------------------
# A.1 distinct keys, last distinct-candidate slot
# ---------------------------------------------------------------------------
def test_distinct_keys_race_for_the_last_candidate_slot() -> None:
    config = mock_config(budget={**mock_config().budget, "max_distinct_candidates": 1})
    h = Harness(config=config)
    try:
        workers = 6
        results, errors = _race(
            workers, lambda i: h.submit(f"last-cand-submit-{i:08d}", files=distinct_submission(i))
        )
        assert not errors, errors
        assert len(results) == workers, len(results)

        accepted = [r for r in results if r.get("status") == "accepted"]
        assert len(accepted) == 1, results
        for rejected in (r for r in results if r.get("status") != "accepted"):
            assert rejected["status"] == "rejected", rejected
            assert rejected["reason_code"] == "BUDGET_EXHAUSTED", rejected
            # The rejection must use this method's own response shape: a
            # candidate receipt, never a begin_attempt-shaped body.
            assert set(rejected) == {
                "operation_id",
                "candidate_id",
                "tree_hash",
                "file_count",
                "source_bytes",
                "status",
                "reason_code",
                "diagnostics_available",
            }, rejected
            assert rejected["candidate_id"] is None, rejected

        # Exactly one candidate row, one quota unit consumed, no leaked reserve.
        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM candidates")["n"] == 1
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:distinct_candidates"] == {"limit": 1, "consumed": 1, "reserved": 0}

        # Each loser left a rejection, and none of them charged a candidate slot.
        rejections = h.service.store.query_all(
            "SELECT reason_code FROM rejections WHERE scope='submit_candidate'"
        )
        assert len(rejections) >= 1, [dict(r) for r in rejections]
        assert all(r["reason_code"] == "BUDGET_EXHAUSTED" for r in rejections), [dict(r) for r in rejections]
    finally:
        h.close()


def test_last_candidate_slot_rejection_is_atomic_and_replayable() -> None:
    """A rejected submission keeps one ledger row and one replayable receipt."""

    config = mock_config(budget={**mock_config().budget, "max_distinct_candidates": 1})
    h = Harness(config=config)
    try:
        assert h.submit("slot-first-0000000001", files=distinct_submission(1))["status"] == "accepted"
        key = "slot-second-000000001"
        first = h.submit(key, files=distinct_submission(2))
        assert first["status"] == "rejected" and first["reason_code"] == "BUDGET_EXHAUSTED", first

        stored = h.service.store.query_one(
            "SELECT state, response_json FROM idempotency WHERE scope='submit_candidate' AND idempotency_key=?",
            (key,),
        )
        assert stored is not None and stored["state"] == "completed", dict(stored) if stored else None
        # The stored receipt is byte-identical to what the caller received.
        assert json.loads(stored["response_json"]) == first

        replay = h.submit(key, files=distinct_submission(2))
        assert replay == first, (replay, first)

        # Replay must not add a second rejection row or consume a slot.
        rejections = h.service.store.query_all(
            "SELECT * FROM rejections WHERE scope='submit_candidate' AND detail_sanitized LIKE '%exhausted%'"
        )
        assert len(rejections) == 1, [dict(r) for r in rejections]
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:distinct_candidates"] == {"limit": 1, "consumed": 1, "reserved": 0}
        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM candidates")["n"] == 1
    finally:
        h.close()


# ---------------------------------------------------------------------------
# A.2 distinct keys, last build slot
# ---------------------------------------------------------------------------
def test_distinct_keys_race_for_the_last_build_slot() -> None:
    h = Harness()
    try:
        # Spend two of the three session build slots.
        for index in range(2):
            sub = h.submit(f"pre-submit-{index:013d}", files=distinct_submission(500 + index))
            built = h.build(f"pre-build-{index:014d}", sub["candidate_id"])
            assert built["state"] == "SUCCEEDED", built
        target = h.submit("race-submit-000000001", files=distinct_submission(999))
        calls_before = len(h.build_backend.calls)

        workers = 6
        results, errors = _race(workers, lambda i: h.build(f"race-build-{i:014d}", target["candidate_id"]))
        assert not errors, errors
        assert len(results) == workers, len(results)

        succeeded = [r for r in results if r.get("state") == "SUCCEEDED"]
        assert len(succeeded) == 1, results
        for rejected in (r for r in results if r.get("state") != "SUCCEEDED"):
            assert rejected["state"] == "REJECTED", rejected
            assert rejected["reason_code"] == "BUDGET_EXHAUSTED", rejected
            assert rejected["build_id"] is None, rejected

        # Exactly one execution side effect for the contended candidate.
        assert len(h.build_backend.calls) - calls_before == 1, h.build_backend.calls

        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM builds")["n"] == 3
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:builds"] == {"limit": 3, "consumed": 3, "reserved": 0}
        assert snapshot[f"tree:{target['tree_hash']}:builds"] == {"limit": 1, "consumed": 1, "reserved": 0}
    finally:
        h.close()


def test_distinct_keys_race_for_the_last_effective_attempt_slot() -> None:
    """Exactly one attempt is created for the last slot; the rest are rejected.

    A loser can be refused either because the quota is gone or because the
    session already has an open attempt (contract §9.13 forbids queueing). Both
    are rejections, and neither may create a second attempt or charge a slot.
    """

    config = mock_config(budget={**mock_config().budget, "max_effective_agent_attempts": 1})
    h = Harness(config=config)
    try:
        sub = h.submit("eff-submit-0000000001", files=distinct_submission(1))
        built = h.build("eff-build-00000000001", sub["candidate_id"])

        workers = 6
        results, errors = _race(
            workers, lambda i: h.begin(f"eff-begin-{i:014d}", built["build_id"])
        )
        assert not errors, errors
        assert len(results) == workers, len(results)

        accepted = [r for r in results if r.get("status") == "accepted"]
        assert len(accepted) == 1, results
        attempt_ids = {r["attempt_id"] for r in accepted}
        assert None not in attempt_ids and len(attempt_ids) == 1, attempt_ids
        for rejected in (r for r in results if r.get("status") != "accepted"):
            assert rejected["status"] == "rejected", rejected
            assert rejected["reason_code"] in ("BUDGET_EXHAUSTED", "REQUEST_REJECTED"), rejected
            assert rejected["attempt_id"] is None, rejected

        # No second attempt was queued or created, and one slot was charged.
        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM attempts")["n"] == 1
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:effective_attempts"] == {"limit": 1, "consumed": 1, "reserved": 0}
        session = h.service.store.query_one(
            "SELECT active_attempt_id FROM sessions WHERE session_id=?", (h.session_id,)
        )
        assert session["active_attempt_id"] == next(iter(attempt_ids)), dict(session)
    finally:
        h.close()


# ---------------------------------------------------------------------------
# A.3 one idempotency key, last slot
# ---------------------------------------------------------------------------
def test_same_key_race_for_the_last_build_slot_is_one_side_effect() -> None:
    h = Harness()
    try:
        for index in range(2):
            sub = h.submit(f"sk-pre-submit-{index:09d}", files=distinct_submission(700 + index))
            assert h.build(f"sk-pre-build-{index:010d}", sub["candidate_id"])["state"] == "SUCCEEDED"
        target = h.submit("sk-submit-0000000001", files=distinct_submission(777))
        key = "sk-same-key-0000000001"
        calls_before = len(h.build_backend.calls)

        workers = 8
        results, errors = _race(workers, lambda _i: h.build(key, target["candidate_id"]))
        assert not errors, errors
        assert len(results) == workers, len(results)

        # One budget reservation and one execution: every caller replays the
        # winner's single stored receipt verbatim.
        assert len({json.dumps(r, sort_keys=True) for r in results}) == 1, results
        only = results[0]
        assert only["state"] == "SUCCEEDED", only
        assert len(h.build_backend.calls) - calls_before == 1, h.build_backend.calls
        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM builds")["n"] == 3
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:builds"] == {"limit": 3, "consumed": 3, "reserved": 0}
        receipts = h.service.store.query_all(
            "SELECT * FROM idempotency WHERE scope='build_candidate' AND idempotency_key=?", (key,)
        )
        assert len(receipts) == 1 and receipts[0]["state"] == "completed", [dict(r) for r in receipts]
    finally:
        h.close()


def test_same_key_race_for_the_last_attempt_slot_never_clobbers_the_receipt() -> None:
    """The winner's receipt must survive a losing same-key caller.

    A losing caller whose reservation fails takes a rejection branch. It must not
    overwrite the receipt the winner just stored, or a replay would report a
    rejection for an attempt that genuinely exists.
    """

    config = mock_config(budget={**mock_config().budget, "max_effective_agent_attempts": 1})
    h = Harness(config=config)
    try:
        sub = h.submit("clob-submit-000000001", files=distinct_submission(1))
        built = h.build("clob-build-0000000001", sub["candidate_id"])
        build_id = built["build_id"]
        key = "clob-same-key-00000001"

        # Force the exact interleaving: the loser performs its opening lookup
        # before the winner commits, then arrives at the rejection branch only
        # after the winner has stored its "accepted" receipt.
        original_lookup = h.service.idempotency.lookup
        loser_past_lookup = threading.Event()
        winner_committed = threading.Event()

        def gated_lookup(session_id: str, scope: str, lookup_key: str, rhash: str):
            result = original_lookup(session_id, scope, lookup_key, rhash)
            if scope == "begin_attempt" and threading.current_thread().name == "loser":
                loser_past_lookup.set()
                winner_committed.wait(timeout=20)
            return result

        h.service.idempotency.lookup = gated_lookup  # type: ignore[assignment]

        results: dict[str, Any] = {}

        def winner() -> None:
            results["winner"] = h.begin(key, build_id)
            winner_committed.set()

        def loser() -> None:
            try:
                results["loser"] = h.begin(key, build_id)
            except Exception as exc:  # pragma: no cover - failure path
                results["loser_error"] = f"{type(exc).__name__}: {exc}"

        winner_thread = threading.Thread(target=winner, name="winner")
        loser_thread = threading.Thread(target=loser, name="loser")
        loser_thread.start()
        assert loser_past_lookup.wait(timeout=20), "loser never reached its lookup"
        winner_thread.start()
        winner_thread.join(timeout=60)
        loser_thread.join(timeout=60)

        assert "loser_error" not in results, results
        assert results["winner"]["status"] == "accepted", results
        # A replay after the race still returns the winning acceptance.
        assert h.begin(key, build_id) == results["winner"], results

        row = h.service.store.query_one(
            "SELECT state, response_json FROM idempotency WHERE scope='begin_attempt' AND idempotency_key=?",
            (key,),
        )
        assert row is not None and row["state"] == "completed", dict(row) if row else None
        stored = json.loads(row["response_json"])
        assert stored["status"] == "accepted", stored
        assert stored["attempt_id"] == results["winner"]["attempt_id"], stored

        # Only one attempt exists and only one slot was charged.
        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM attempts")["n"] == 1
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:effective_attempts"] == {"limit": 1, "consumed": 1, "reserved": 0}
    finally:
        h.close()


# ---------------------------------------------------------------------------
# A.4 failures and rejections leave no residue
# ---------------------------------------------------------------------------
def test_budget_rejection_under_concurrency_leaves_no_residue() -> None:
    """Concurrent rejects: no extra reservation, no negative budget, no 500."""

    config = mock_config(budget={**mock_config().budget, "max_distinct_candidates": 1})
    h = Harness(config=config)
    try:
        assert h.submit("res-first-0000000001", files=distinct_submission(1))["status"] == "accepted"
        workers = 8
        results, errors = _race(
            workers, lambda i: h.submit(f"res-loser-{i:014d}", files=distinct_submission(50 + i))
        )
        assert not errors, errors
        assert len(results) == workers, len(results)
        assert all(r["status"] == "rejected" for r in results), results

        snapshot = h.service.store.counter_snapshot(h.session_id)
        for key, entry in snapshot.items():
            assert entry["reserved"] == 0, (key, entry)
            assert entry["reserved"] >= 0 and entry["consumed"] >= 0, (key, entry)
            if entry["limit"] is not None:
                assert entry["consumed"] <= entry["limit"], (key, entry)

        # The Agent-visible budget never goes negative either.
        remaining = h.service.get_session(h.session_id)["budget_remaining"]
        for name, value in remaining.items():
            if value is not None:
                assert value >= 0, (name, value)
        assert remaining["distinct_candidates_remaining"] == 0, remaining
    finally:
        h.close()


def test_build_backend_fault_is_recorded_not_leaked_as_a_500() -> None:
    """An unexpected backend fault must not leak budget or escape as HTTP 500."""

    from runner.adapters.build_backend import MockBuildBackend

    class ExplodingBackend(MockBuildBackend):
        def build(self, request, *, limits):  # noqa: ANN001
            raise RuntimeError("backend exploded")

    h = Harness()
    h.service.build_backend = ExplodingBackend()
    try:
        sub = h.submit("boom-submit-000000001", files=distinct_submission(1))
        status, body = h.post(
            "/v1/builds",
            {"idempotency_key": "boom-build-0000000001", "candidate_id": sub["candidate_id"]},
        )
        assert status == 200, (status, body)
        assert body["state"] == "FAILED", body
        assert body["reason_code"] == "BUILD_FAILED", body

        # The accepted build keeps its budget (it was genuinely attempted), but
        # nothing stays reserved and the operation reaches a terminal state.
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:builds"] == {"limit": 3, "consumed": 1, "reserved": 0}, snapshot
        assert snapshot[f"tree:{sub['tree_hash']}:builds"] == {
            "limit": 1,
            "consumed": 1,
            "reserved": 0,
        }, snapshot
        open_ops = h.service.store.query_all(
            "SELECT * FROM operations WHERE state IN ('PENDING','RUNNING')"
        )
        assert not open_ops, [dict(r) for r in open_ops]

        # The same key replays the recorded failure rather than re-running it.
        status2, replay = h.post(
            "/v1/builds",
            {"idempotency_key": "boom-build-0000000001", "candidate_id": sub["candidate_id"]},
        )
        assert status2 == 200 and replay == body, (status2, replay, body)
    finally:
        h.close()


def test_reclaimed_operation_is_charged_not_refunded() -> None:
    """Contract §5: an accepted operation is charged even when its outcome is unknown.

    ``outcome_unknown`` means "we do not know what happened", not "it did not
    happen". The side effect may well have run, so the reserved budget is
    *consumed*, and it is never automatically refunded.
    """

    from runner.core.budget import C_BUILDS, tree_builds

    h = Harness()
    try:
        sub = h.submit("aband-submit-000000001", files=distinct_submission(1))
        tree = sub["tree_hash"]
        with h.service.store.txn():
            operation_id = h.service._operation_create(
                h.session_id,
                "build_candidate",
                state="RUNNING",
                candidate_id=sub["candidate_id"],
                deadline_utc="2000-01-01T00:00:00.000Z",  # its own deadline is past
            )
            h.service._reserve(
                h.session_id,
                [(C_BUILDS, 1), (tree_builds(tree), 1)],
                reason="build_candidate",
                ref_type="candidate",
                ref_id=sub["candidate_id"],
                operation_id=operation_id,
            )
        assert h.service.store.counter_snapshot(h.session_id)["session:builds"]["reserved"] == 1

        summary = h.make_supervisor().sweep()
        assert operation_id in summary["dangling_reservations"], summary

        row = h.service.store.query_one(
            "SELECT state FROM operations WHERE operation_id=?", (operation_id,)
        )
        assert row["state"] == "OUTCOME_UNKNOWN", dict(row)

        # Charged, not refunded: reserved returns to zero and consumed becomes 1.
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:builds"] == {"limit": 3, "consumed": 1, "reserved": 0}, snapshot
        assert snapshot[tree_builds(tree)] == {"limit": 1, "consumed": 1, "reserved": 0}, snapshot

        # The charge is auditable in the append-only ledger.
        rows = h.service.store.query_all(
            "SELECT * FROM counter_ledger WHERE reason='outcome_unknown_charged'"
        )
        assert rows, "the charge must be recorded in the counter ledger"
        assert all(r["delta_consumed"] == 1 for r in rows), [dict(r) for r in rows]

        # No automatic replay of a side-effecting action.
        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM builds")["n"] == 0
    finally:
        h.close()


def test_repeated_recovery_settles_each_reservation_exactly_once() -> None:
    """Reclaiming the same operation twice must not double-charge the budget."""

    from runner.core.budget import C_BUILDS

    h = Harness()
    try:
        h.submit("idem-submit-0000000001", files=distinct_submission(1))
        with h.service.store.txn():
            operation_id = h.service._operation_create(
                h.session_id,
                "build_candidate",
                state="RUNNING",
                deadline_utc="2000-01-01T00:00:00.000Z",
            )
            h.service._reserve(
                h.session_id,
                [(C_BUILDS, 1)],
                reason="build_candidate",
                ref_type="candidate",
                ref_id=None,
                operation_id=operation_id,
            )

        supervisor = h.make_supervisor()
        first = supervisor.sweep()
        assert operation_id in first["dangling_reservations"], first
        after_first = h.service.store.counter_snapshot(h.session_id)

        # Three more sweeps: the operation is terminal and its reservations are
        # closed, so nothing may change and no new ledger rows may appear.
        ledger_before = h.service.store.query_one("SELECT COUNT(*) AS n FROM counter_ledger")["n"]
        for _ in range(3):
            later = supervisor.sweep()
            assert operation_id not in later["dangling_reservations"], later
        assert h.service.store.counter_snapshot(h.session_id) == after_first
        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM counter_ledger")["n"] == ledger_before

        # Exactly one charge per reserved counter.
        charges = h.service.store.query_all(
            "SELECT * FROM counter_ledger WHERE reason='outcome_unknown_charged'"
        )
        assert len(charges) == 1, [dict(r) for r in charges]
        assert int(charges[0]["delta_consumed"]) == 1, dict(charges[0])

        # The reservation latch itself is closed exactly once.
        reservations = h.service.store.query_all(
            "SELECT state, close_reason FROM counter_reservations WHERE operation_id=?", (operation_id,)
        )
        assert [r["state"] for r in reservations] == ["SETTLED"], [dict(r) for r in reservations]
    finally:
        h.close()


def test_reservation_latch_never_double_settles_under_repeated_close() -> None:
    """``reservation_close`` is a latch: the second close applies no effect."""

    h = Harness()
    try:
        store = h.service.store
        with store.txn():
            store.counter_ensure(h.session_id, "session:builds", 3)
            store.counter_reserve(
                h.session_id,
                "session:builds",
                1,
                reason="test_reserve",
                ledger_id="evt_TESTRESERVE0000000000000",
                now=h.clock.utc(),
            )
            store.reservation_create(
                reservation_id="rsv_TESTLATCH0000000000000",
                session_id=h.session_id,
                operation_id="op_TESTLATCH0000000000000",
                counter_key="session:builds",
                amount=1,
                reason="test_reserve",
                now=h.clock.utc(),
            )

        first = store.reservation_close(
            "rsv_TESTLATCH0000000000000",
            consume=True,
            reason="test_close",
            ledger_id="evt_TESTCLOSE00000000000000",
            now=h.clock.utc(),
        )
        assert first is not None and first["consumed"] == 1, first
        assert store.counter_snapshot(h.session_id)["session:builds"] == {
            "limit": 3,
            "consumed": 1,
            "reserved": 0,
        }

        ledger_after_first = store.query_one("SELECT COUNT(*) AS n FROM counter_ledger")["n"]
        second = store.reservation_close(
            "rsv_TESTLATCH0000000000000",
            consume=True,
            reason="test_close_again",
            ledger_id="evt_TESTCLOSE00000000000001",
            now=h.clock.utc(),
        )
        assert second is None, second
        # No second charge and no second ledger entry.
        assert store.counter_snapshot(h.session_id)["session:builds"]["consumed"] == 1
        assert store.query_one("SELECT COUNT(*) AS n FROM counter_ledger")["n"] == ledger_after_first
    finally:
        h.close()


def test_counter_ledger_is_self_consistent_after_concurrent_success_and_rejection() -> None:
    """The append-only ledger must reconcile exactly with the counter rows."""

    config = mock_config(budget={**mock_config().budget, "max_distinct_candidates": 2})
    h = Harness(config=config)
    try:
        results, errors = _race(
            6,
            lambda i: h.submit(f"ledger-submit-{i:011d}", files=distinct_submission(i)),
        )
        assert not errors, errors
        assert sum(1 for r in results if r["status"] == "accepted") == 2, results

        for counter in h.service.store.counter_snapshot(h.session_id):
            rows = h.service.store.query_all(
                "SELECT COALESCE(SUM(delta_reserved),0) AS r, COALESCE(SUM(delta_consumed),0) AS c "
                "FROM counter_ledger WHERE session_id=? AND counter_key=?",
                (h.session_id, counter),
            )
            assert int(rows[0]["r"]) == 0, (counter, dict(rows[0]))
            assert int(rows[0]["c"]) == h.service.store.counter_snapshot(h.session_id)[counter]["consumed"], counter
    finally:
        h.close()
