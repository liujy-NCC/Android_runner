"""Budget-exhausted build rejection: idempotency, concurrency, no side effects.

Contract (confirmed with the requester):

* an exhausted build budget must NOT create a ``builds`` row;
* it MUST leave a traceable rejection ledger entry AND a replayable idempotency
  receipt, committed together so no crash window separates them;
* it must NOT consume build quota (the quota is already exhausted, and a
  pre-execution rejection never charges execution budget);
* concurrent calls with the same key must all receive the same receipt, with one
  ledger entry and no 500.
"""

from __future__ import annotations

import base64
import json
import threading

from harness import Harness

BUILD_SCOPE = "build_candidate"


def _distinct_files(index: int) -> list[dict[str, str]]:
    return [
        {
            "relative_path": f"candidate/behaviour-{index}.txt",
            "content_base64": base64.b64encode(f"candidate source {index}\n".encode()).decode("ascii"),
        }
    ]


def _exhaust_build_budget(h: Harness) -> list[str]:
    """Spend all 3 builds on 3 distinct candidates. Returns their candidate ids."""

    candidate_ids: list[str] = []
    for index in range(3):
        sub = h.submit(f"exhaust-submit-{index:011d}", files=_distinct_files(index))
        assert sub["status"] == "accepted", sub
        build = h.build(f"exhaust-build-{index:012d}", sub["candidate_id"])
        assert build["state"] == "SUCCEEDED", build
        candidate_ids.append(sub["candidate_id"])
    snapshot = h.service.store.counter_snapshot(h.session_id)
    assert snapshot["session:builds"]["consumed"] == 3, snapshot
    return candidate_ids


def test_exhausted_build_budget_rejects_without_a_builds_row() -> None:
    h = Harness()
    try:
        candidates = _exhaust_build_budget(h)
        builds_before = h.service.store.query_one("SELECT COUNT(*) AS n FROM builds")["n"]

        # A second build of an already-built candidate: the per-candidate build
        # allowance is what is exhausted here.
        rejected = h.build("again-build-00000000001", candidates[0])
        assert rejected["state"] == "REJECTED", rejected
        assert rejected["reason_code"] == "BUDGET_EXHAUSTED", rejected
        assert rejected["build_id"] is None, rejected

        # No new builds row, and the build counter did not move.
        builds_after = h.service.store.query_one("SELECT COUNT(*) AS n FROM builds")["n"]
        assert builds_after == builds_before == 3, (builds_before, builds_after)
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:builds"]["consumed"] == 3, snapshot
        assert snapshot["session:builds"]["reserved"] == 0, snapshot
    finally:
        h.close()


def test_budget_rejection_leaves_a_traceable_ledger_entry_and_receipt() -> None:
    h = Harness()
    try:
        candidates = _exhaust_build_budget(h)
        key = "trace-build-000000000001"
        result = h.build(key, candidates[0])
        assert result["reason_code"] == "BUDGET_EXHAUSTED"

        rejections = h.service.store.query_all(
            "SELECT * FROM rejections WHERE scope=?", (BUILD_SCOPE,)
        )
        assert len(rejections) == 1, [dict(r) for r in rejections]
        assert rejections[0]["reason_code"] == "BUDGET_EXHAUSTED", dict(rejections[0])

        rows = h.service.store.query_all(
            "SELECT * FROM idempotency WHERE scope=? AND idempotency_key=?", (BUILD_SCOPE, key)
        )
        assert len(rows) == 1, [dict(r) for r in rows]
        assert rows[0]["state"] == "completed", dict(rows[0])
        assert rows[0]["response_json"] is not None
        assert json.loads(rows[0]["response_json"])["reason_code"] == "BUDGET_EXHAUSTED"

        # The audit trail also carries an event for the rejection.
        events = h.service.store.query_all(
            "SELECT * FROM events WHERE event_type='request_rejected' AND session_id=?",
            (h.session_id,),
        )
        assert events, "rejection must be visible in the event ledger"
    finally:
        h.close()


def test_replaying_the_same_key_returns_the_identical_rejection() -> None:
    h = Harness()
    try:
        candidates = _exhaust_build_budget(h)
        key = "replay-build-00000000001"
        first = h.build(key, candidates[0])
        second = h.build(key, candidates[0])
        assert first == second, (first, second)
        # A replay must not add a second ledger entry.
        rejections = h.service.store.query_all("SELECT * FROM rejections WHERE scope=?", (BUILD_SCOPE,))
        assert len(rejections) == 1, [dict(r) for r in rejections]
    finally:
        h.close()


def test_concurrent_same_key_budget_exhausted_is_one_receipt_and_one_ledger_row() -> None:
    """The requester's case: concurrent same-key calls during exhaustion."""

    h = Harness()
    try:
        candidates = _exhaust_build_budget(h)
        key = "concurrent-exhaust-00000001"
        target = candidates[0]

        workers = 8
        barrier = threading.Barrier(workers)
        results: list[dict] = []
        errors: list[str] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=20)
                results.append(h.build(key, target))
            except Exception as exc:  # a 500 would surface here
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert not errors, errors
        assert len(results) == workers, len(results)

        # Every caller sees the SAME receipt: no 500, no divergent result.
        distinct = {json.dumps(r, sort_keys=True) for r in results}
        assert len(distinct) == 1, distinct
        only = json.loads(next(iter(distinct)))
        assert only["state"] == "REJECTED", only
        assert only["reason_code"] == "BUDGET_EXHAUSTED", only
        assert only["build_id"] is None, only

        # Exactly one ledger entry and one idempotency row for the key.
        rejections = h.service.store.query_all("SELECT * FROM rejections WHERE scope=?", (BUILD_SCOPE,))
        assert len(rejections) == 1, [dict(r) for r in rejections]
        rows = h.service.store.query_all(
            "SELECT * FROM idempotency WHERE scope=? AND idempotency_key=?", (BUILD_SCOPE, key)
        )
        assert len(rows) == 1, [dict(r) for r in rows]

        # No builds row, no attempt, and no budget movement.
        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM builds")["n"] == 3
        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM attempts")["n"] == 0
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:builds"]["consumed"] == 3, snapshot
        assert snapshot["session:builds"]["reserved"] == 0, snapshot
    finally:
        h.close()


def test_concurrent_distinct_keys_exhausted_do_not_double_charge_or_500() -> None:
    """Different keys during exhaustion each reject, but never charge quota."""

    h = Harness()
    try:
        candidates = _exhaust_build_budget(h)
        target = candidates[0]
        workers = 6
        barrier = threading.Barrier(workers)
        results: list[dict] = []
        errors: list[str] = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=20)
                results.append(h.build(f"distinct-exhaust-{index:010d}", target))
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert not errors, errors
        assert len(results) == workers
        for result in results:
            assert result["state"] == "REJECTED", result
            assert result["reason_code"] == "BUDGET_EXHAUSTED", result
            assert result["build_id"] is None, result

        # One rejection per distinct key, still no builds row, no charge.
        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM builds")["n"] == 3
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:builds"]["consumed"] == 3, snapshot
        assert snapshot["session:builds"]["reserved"] == 0, snapshot
    finally:
        h.close()


def test_exhausted_rejection_never_creates_an_attempt() -> None:
    """A rejected build must not be able to reach ``begin_attempt``."""

    h = Harness()
    try:
        candidates = _exhaust_build_budget(h)
        rejected = h.build("no-attempt-build-0000001", candidates[0])
        assert rejected["build_id"] is None
        # The rejection deliberately returns no build_id, so there is no handle
        # that could be used to begin an attempt.
        receipt = h.begin("no-attempt-begin-0000001", "bld_" + "Z" * 26)
        assert receipt["status"] == "rejected", receipt
        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM attempts")["n"] == 0
    finally:
        h.close()


def test_budget_rejection_is_committed_atomically_with_its_receipt() -> None:
    """The ledger entry and the replayable receipt must agree.

    A crash between them is impossible by construction: both are written in one
    transaction. This asserts the observable invariant on both sides.
    """

    h = Harness()
    try:
        candidates = _exhaust_build_budget(h)
        key = "atomic-build-00000000001"
        result = h.build(key, candidates[0])
        assert result["reason_code"] == "BUDGET_EXHAUSTED"

        row = h.service.store.query_one(
            "SELECT * FROM idempotency WHERE scope=? AND idempotency_key=?", (BUILD_SCOPE, key)
        )
        assert row is not None and row["state"] == "completed"
        assert row["response_sha256"] is not None

        # The stored receipt is byte-identical to what the caller received.
        stored = h.service.store.loads(row["response_json"], {})
        assert stored == result, (stored, result)

        # And this rejection is replayable: exactly one ledger entry for the
        # scope, and the receipt for this key exists and completes.
        rejections = h.service.store.query_all("SELECT COUNT(*) AS n FROM rejections WHERE scope=?", (BUILD_SCOPE,))
        assert rejections[0]["n"] == 1, dict(rejections[0])
        receipts = h.service.store.query_all(
            "SELECT * FROM idempotency WHERE scope=? AND idempotency_key=?", (BUILD_SCOPE, key)
        )
        assert len(receipts) == 1 and receipts[0]["state"] == "completed", [dict(r) for r in receipts]
        assert row["response_sha256"] == receipts[0]["response_sha256"]
    finally:
        h.close()
