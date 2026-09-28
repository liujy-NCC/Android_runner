"""Oracle access control: the verdict is private and the adapter is replaceable."""

from __future__ import annotations

from harness import Harness, make_source_files
from runner.adapters.base import OracleVerdict
from runner.adapters.oracle import NotIntegratedOracle


def _run(h: Harness, index: int = 1) -> tuple[str, str]:
    sub = h.submit(f"submit-key-{index:013d}", files=make_source_files(body=f"public final class O{index} {{}}"))
    build = h.build(f"build-key-{index:014d}", sub["candidate_id"])
    attempt_id = h.begin(f"attempt-key-{index:013d}", build["build_id"])["attempt_id"]
    h.launch(f"launch-key-{index:013d}", attempt_id)
    supervisor = h.make_supervisor()
    h.clock.advance(30)
    supervisor.sweep()
    return attempt_id, sub["candidate_id"]


def test_private_oracle_is_readable_only_through_the_local_admin_path() -> None:
    h = Harness()
    try:
        attempt_id, _c = _run(h)
        private = h.service.read_private_oracle(attempt_id)
        assert private is not None
        assert private["verdict"] in ("success", "failure", "inconclusive", "not_evaluated")
        assert private["evidence_refs_json"]

        # No HTTP route returns it.
        for path in ("/v1/oracle", f"/v1/attempts/{attempt_id}/oracle", "/v1/admin/oracle"):
            status, _body = h.get(path)
            assert status == 404
    finally:
        h.close()


def test_oracle_consumes_only_sealed_evidence() -> None:
    """The Oracle adapter receives sealed objects, and only those."""

    seen: list[list] = []

    class RecordingOracle:
        oracle_id = "recording"
        version = "1"
        criteria_version = "test"

        def evaluate(self, evidence, *, environment_valid):
            seen.append(list(evidence))
            return OracleVerdict(
                verdict="inconclusive",
                evaluation_status="completed",
                oracle_id=self.oracle_id,
                oracle_version=self.version,
                criteria_version=self.criteria_version,
                observations={},
                reason_private="recording",
                evidence_refs=[{"artifact_id": e.artifact_id} for e in evidence],
            )

    h = Harness()
    try:
        h.service.oracle = RecordingOracle()
        h.make_supervisor().oracle = h.service.oracle
        _run(h, 2)
        assert seen, "oracle was never invoked"
        evidence = seen[0]
        assert evidence, "oracle received no evidence"
        for item in evidence:
            row = h.service.store.query_one(
                "SELECT state FROM artifacts WHERE artifact_id=?", (item.artifact_id,)
            )
            assert row["state"] == "sealed", (item.artifact_id, dict(row))
            assert item.kind in ("execution_evidence", "cleanup_evidence", "collection_gap")
    finally:
        h.close()


def test_oracle_never_sees_candidate_reported_success_as_authority() -> None:
    """Candidate `[OK] ...` self-report stays untrusted diagnostic text."""

    h = Harness()
    try:
        attempt_id, candidate_id = _run(h, 3)
        # The candidate's self-report exists in the sealed evidence stream...
        rows = h.service.store.query_all(
            "SELECT artifact_id FROM artifacts WHERE attempt_id=? AND kind='execution_evidence'",
            (attempt_id,),
        )
        blob = b"".join(h.service.artifacts.read_object(r["artifact_id"]) for r in rows)
        assert b"[OK] startService accepted" in blob

        # ...but the Oracle's criteria version is what decides, and the Agent
        # only ever sees candidate-scoped diagnostic text.
        private = h.service.read_private_oracle(attempt_id)
        assert private["criteria_version"]
        _s, feedback = h.get(
            "/v1/feedback",
            query={"candidate_id": candidate_id, "kind": "runtime", "attempt_id": attempt_id},
        )
        for entry in feedback["entries"]:
            assert "oracle" not in str(entry).lower()
    finally:
        h.close()


def test_not_integrated_oracle_reports_not_evaluated_without_pretending() -> None:
    h = Harness()
    try:
        h.service.oracle = NotIntegratedOracle()
        supervisor = h.make_supervisor()
        supervisor.oracle = h.service.oracle
        attempt_id, _c = _run(h, 4)
        private = h.service.read_private_oracle(attempt_id)
        assert private["verdict"] == "not_evaluated"
        assert private["evaluation_status"] == "error"
        assert private["criteria_version"] == "NOT_FROZEN"

        # Evidence is preserved for a later human/automatic evaluation.
        count = h.service.store.query_one(
            "SELECT COUNT(*) AS n FROM artifacts WHERE attempt_id=? AND state='sealed'", (attempt_id,)
        )
        assert count["n"] > 0
    finally:
        h.close()


def test_oracle_adapter_is_replaceable_without_changing_execution_results() -> None:
    """Replacing the Oracle must not change any control-flow outcome."""

    h = Harness()
    try:
        attempt_id, _c = _run(h, 5)
        baseline = h.service.store.query_one(
            "SELECT state, execution_status, recovery_status FROM attempts WHERE attempt_id=?", (attempt_id,)
        )

        class AlternateOracle:
            oracle_id = "alternate"
            version = "9"
            criteria_version = "alt-v9"

            def evaluate(self, evidence, *, environment_valid):
                return OracleVerdict(
                    verdict="failure",
                    evaluation_status="completed",
                    oracle_id=self.oracle_id,
                    oracle_version=self.version,
                    criteria_version=self.criteria_version,
                    observations={"action_view_requested": "absent", "viewer_received": "absent"},
                    reason_private="alternate",
                    evidence_refs=[],
                )

        h.service.oracle = AlternateOracle()
        supervisor = h.make_supervisor()
        supervisor.oracle = h.service.oracle
        h.service.store.execute("DELETE FROM oracle_evaluations WHERE attempt_id=?", (attempt_id,))
        h.service.store.execute(
            "UPDATE attempts SET evaluation_status=NULL, oracle_verdict=NULL WHERE attempt_id=?",
            (attempt_id,),
        )
        supervisor.sweep()

        after = h.service.store.query_one(
            "SELECT state, execution_status, recovery_status FROM attempts WHERE attempt_id=?", (attempt_id,)
        )
        assert dict(baseline) == dict(after), (dict(baseline), dict(after))
        private = h.service.read_private_oracle(attempt_id)
        assert private["oracle_id"] == "alternate"
        assert private["verdict"] == "failure"
    finally:
        h.close()


def test_read_feedback_quota_never_implies_private_evidence_exists() -> None:
    """Exhaustion responses contain no business fields at all."""

    from runner.config import mock_config

    limits = dict(mock_config().feedback_limits)
    limits["diagnostic_pages_per_candidate"] = 1
    config = mock_config(feedback_limits=limits)
    h = Harness(config=config)
    try:
        sub = h.submit("submit-key-0000000901")
        build = h.build("build-key-00000000901", sub["candidate_id"])
        attempt_id = h.begin("attempt-key-0000000901", build["build_id"])["attempt_id"]
        h.launch("launch-key-0000000901", attempt_id)
        supervisor = h.make_supervisor()
        h.clock.advance(30)
        supervisor.sweep()

        _s, first = h.get(
            "/v1/feedback",
            query={"candidate_id": sub["candidate_id"], "kind": "runtime", "attempt_id": attempt_id},
        )
        _s, second = h.get(
            "/v1/feedback",
            query={"candidate_id": sub["candidate_id"], "kind": "runtime", "attempt_id": attempt_id},
        )
        assert second["truncated"] is True, second
        assert second["truncation_reason"], second
        assert second["entries"] == []
        assert set(second) <= {"truncated", "truncation_reason", "entries", "error"}, second
        assert "candidate_id" not in second
    finally:
        h.close()
