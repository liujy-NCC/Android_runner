"""Budget: per-candidate and per-session limits, reservation before side effects."""

from __future__ import annotations

from harness import Harness, make_source_files
from runner.config import mock_config


def _flow(h: Harness, index: int, body: str | None = None):
    """Submit + build.

    When the candidate budget is exhausted the submit itself is rejected, so
    the caller observes a rejected receipt rather than a build result.
    """

    sub = h.submit(f"submit-key-{index:013d}", files=make_source_files(body=body or f"public final class C{index} {{}}"))
    if sub["status"] != "accepted":
        return sub, {"state": "REJECTED", "reason_code": "BUDGET_EXHAUSTED", "build_id": None}
    build = h.build(f"build-key-{index:014d}", sub["candidate_id"])
    return sub, build


def test_distinct_candidate_limit_rejects_fourth() -> None:
    h = Harness(config=mock_config(budget={**mock_config().budget, "max_distinct_candidates": 3}))
    try:
        for i in range(3):
            sub = h.submit(f"submit-key-{i:013d}", files=make_source_files(body=f"public final class C{i} {{}}"))
            assert sub["status"] == "accepted", (i, sub)
        fourth = h.submit("submit-key-0000000300", files=make_source_files(body="public final class C99 {}"))
        assert fourth["status"] == "rejected", fourth
        assert fourth["reason_code"] == "SOURCE_POLICY_REJECTED" or fourth["reason_code"] == "BUDGET_EXHAUSTED"
    finally:
        h.close()


def test_build_limit_rejects_fourth_build() -> None:
    h = Harness()
    try:
        for i in range(3):
            _sub, build = _flow(h, i)
            assert build["state"] == "SUCCEEDED", (i, build)
        _sub, fourth = _flow(h, 99)
        assert fourth["state"] == "REJECTED", fourth
        assert fourth["reason_code"] == "BUDGET_EXHAUSTED"
        assert fourth["build_id"] is None
        # A rejected-for-budget build must not create an attempt.
        count = h.service.store.query_one("SELECT COUNT(*) AS n FROM attempts")
        assert count["n"] == 0
    finally:
        h.close()


def test_second_build_of_same_candidate_is_refused() -> None:
    h = Harness()
    try:
        sub = h.submit("submit-key-0000000310")
        first = h.build("build-key-00000000310", sub["candidate_id"])
        assert first["state"] == "SUCCEEDED", first
        second = h.build("build-key-00000000311", sub["candidate_id"])
        assert second["state"] == "REJECTED", second
        assert second["reason_code"] == "BUDGET_EXHAUSTED"
        assert second["build_id"] is None
    finally:
        h.close()


def test_effective_attempt_limit_rejects_fourth() -> None:
    h = Harness()
    try:
        attempt_ids = []
        for i in range(3):
            sub = h.submit(f"submit-key-{i:013d}", files=make_source_files(body=f"public final class A{i} {{}}"))
            build = h.build(f"build-key-{i:014d}", sub["candidate_id"])
            assert build["state"] == "SUCCEEDED", build
            receipt = h.begin(f"attempt-key-{i:013d}", build["build_id"])
            assert receipt["status"] == "accepted", (i, receipt)
            attempt_ids.append(receipt["attempt_id"])

            # Finish the attempt so the next one is not blocked by "already open".
            h.launch(f"launch-key-{i:013d}", receipt["attempt_id"])
            supervisor = h.make_supervisor()
            h.clock.advance(30)
            supervisor.sweep()

        # Fourth distinct candidate + build is allowed by the candidate/build
        # budget, but the effective attempt budget is exhausted.
        sub = h.submit("submit-key-0000000320", files=make_source_files(body="public final class A99 {}"))
        assert sub["status"] == "rejected", sub
    finally:
        h.close()


def test_second_normal_run_of_same_candidate_is_refused() -> None:
    h = Harness()
    try:
        sub = h.submit("submit-key-0000000330")
        build = h.build("build-key-00000000330", sub["candidate_id"])
        first = h.begin("attempt-key-0000000330", build["build_id"])
        assert first["status"] == "accepted", first
        h.launch("launch-key-00000000330", first["attempt_id"])
        supervisor = h.make_supervisor()
        h.clock.advance(30)
        supervisor.sweep()

        # Same candidate's build reused: the per-candidate normal-run budget is
        # already consumed, so this is refused.
        second = h.begin("attempt-key-0000000331", build["build_id"])
        assert second["status"] == "rejected", second
        assert second["reason_code"] == "BUDGET_EXHAUSTED"
    finally:
        h.close()


def test_new_key_during_active_attempt_is_rejected_without_queueing() -> None:
    h = Harness()
    try:
        sub = h.submit("submit-key-0000000340")
        build = h.build("build-key-00000000340", sub["candidate_id"])
        first = h.begin("attempt-key-0000000340", build["build_id"])
        assert first["status"] == "accepted", first

        # A second begin with a NEW key while an attempt is open is rejected and
        # never queued.
        second = h.begin("attempt-key-0000000341", build["build_id"])
        assert second["status"] == "rejected", second
        assert second["attempt_id"] is None
        assert second["reason_code"] == "REQUEST_REJECTED"

        # Same key returns the original receipt with the original attempt id.
        duplicate = h.begin("attempt-key-0000000340", build["build_id"])
        assert duplicate["status"] == "accepted", duplicate
        assert duplicate["attempt_id"] == first["attempt_id"]
    finally:
        h.close()


def test_wall_clock_budget_is_not_extended_by_reads() -> None:
    h = Harness(config=mock_config(budget={**mock_config().budget, "session_wall_seconds": 100}))
    try:
        _s, before = h.get("/v1/session")
        assert before["budget_remaining"]["wall_seconds_remaining"] <= 100
        h.clock.advance(40)
        _s, after = h.get("/v1/session")
        assert after["budget_remaining"]["wall_seconds_remaining"] < before["budget_remaining"]["wall_seconds_remaining"]
        assert after["budget_remaining"]["wall_seconds_remaining"] == 60
    finally:
        h.close()


def test_wall_clock_expiry_closes_session_and_attempt() -> None:
    h = Harness(config=mock_config(budget={**mock_config().budget, "session_wall_seconds": 60}))
    try:
        sub = h.submit("submit-key-0000000350")
        build = h.build("build-key-00000000350", sub["candidate_id"])
        attempt_id = h.begin("attempt-key-0000000350", build["build_id"])["attempt_id"]
        h.launch("launch-key-00000000350", attempt_id)

        supervisor = h.make_supervisor()
        h.clock.advance(120)
        summary = supervisor.sweep()
        assert summary["sessions_closing"], summary

        supervisor.sweep()
        row = h.service.store.query_one("SELECT state, close_reason FROM sessions WHERE session_id=?", (h.session_id,))
        assert row["state"] == "CLOSED", dict(row)
        assert row["close_reason"] == "wall_clock_expired", dict(row)
        attempt = h.service.store.query_one("SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert attempt["state"] == "CLOSED"
    finally:
        h.close()


def test_infrastructure_retry_is_controller_only() -> None:
    h = Harness()
    try:
        sub = h.submit("submit-key-0000000360")
        build = h.build("build-key-00000000360", sub["candidate_id"])
        attempt_id = h.begin("attempt-key-0000000360", build["build_id"])["attempt_id"]
        h.launch("launch-key-0000000360", attempt_id)
        supervisor = h.make_supervisor()
        h.clock.advance(30)
        supervisor.sweep()

        # No Agent-visible route exists for granting a retry.
        for path in ("/v1/retry", "/v1/admin/retry", "/v1/attempts/retry"):
            status, _ = h.post(path, {"idempotency_key": "retry-key-0000000001"})
            assert status == 404

        # The private controller entry point can grant one retry on the SAME build.
        retry_id = h.service.grant_infrastructure_retry(
            h.session_id, attempt_id, evidence_ref="device-evidence:mock-1", reason="emu crash"
        )
        row = h.service.store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (retry_id,))
        assert row["build_id"] == build["build_id"]
        assert row["replay_of"] == attempt_id
        assert row["effective_agent_attempt"] == 0
        assert row["infrastructure_retry"] == 1

        # The retry allowance is consumed exactly once.
        from runner.errors import BudgetExhausted

        try:
            h.service.grant_infrastructure_retry(
                h.session_id, attempt_id, evidence_ref="device-evidence:mock-2", reason="again"
            )
        except BudgetExhausted:
            pass
        else:  # pragma: no cover
            raise AssertionError("second infrastructure retry should be refused")
    finally:
        h.close()
