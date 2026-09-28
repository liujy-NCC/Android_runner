"""Build failure: located, sanitized diagnostics with no attempt fabricated."""

from __future__ import annotations

import json

from harness import JAVA_ROOT, Harness, make_source_files


def test_build_failure_keeps_build_record_and_creates_no_attempt() -> None:
    h = Harness(build_fails=True)
    try:
        sub = h.submit("submit-key-0000000801")
        build = h.build("build-key-00000000801", sub["candidate_id"])
        assert build["state"] == "FAILED", build
        assert build["reason_code"] == "BUILD_FAILED"

        # The build row survives with its consumed budget.
        row = h.service.store.query_one("SELECT * FROM builds WHERE build_id=?", (build["build_id"],))
        assert row["state"] == "FAILED"
        assert row["exit_code"] == 1
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:builds"]["consumed"] == 1
        assert snapshot["session:builds"]["reserved"] == 0

        # A failed build must never be dressed up as an execution attempt.
        count = h.service.store.query_one("SELECT COUNT(*) AS n FROM attempts")
        assert count["n"] == 0

        # And it cannot start an attempt.
        receipt = h.begin("attempt-key-0000000801", build["build_id"])
        assert receipt["status"] == "rejected", receipt
        assert receipt["attempt_id"] is None
        assert receipt["reason_code"] == "BUILD_FAILED"
    finally:
        h.close()


def test_build_failure_diagnostics_are_located_and_sanitized() -> None:
    h = Harness(build_fails=True)
    try:
        sub = h.submit("submit-key-0000000802")
        build = h.build("build-key-00000000802", sub["candidate_id"])

        # Build diagnostics are readable WITHOUT an attempt_id.
        _s, feedback = h.get(
            "/v1/feedback", query={"candidate_id": sub["candidate_id"], "kind": "build", "build_id": build["build_id"]}
        )
        assert feedback["entries"], feedback
        for entry in feedback["entries"]:
            assert entry["kind"] == "build_error", entry
            assert entry["relative_candidate_source_path"].startswith("src/main/java/"), entry
            assert isinstance(entry["line"], int) and entry["line"] > 0
            assert isinstance(entry["column"], int) and entry["column"] > 0
            assert entry["sanitized_summary"], entry

        blob = json.dumps(feedback, ensure_ascii=False)
        # No absolute path, no command line, no toolchain/template internals.
        assert "D:\\" not in blob and "/home/" not in blob
        assert "javac" not in blob.lower()
        assert "gradle" not in blob.lower()
        assert "toolchain" not in blob.lower()
        assert "template" not in blob.lower()
    finally:
        h.close()


def test_build_failure_diagnostic_pagination_and_billing() -> None:
    h = Harness(build_fails=True)
    try:
        sub = h.submit("submit-key-0000000803")
        build = h.build("build-key-00000000803", sub["candidate_id"])
        _s, page1 = h.get(
            "/v1/feedback", query={"candidate_id": sub["candidate_id"], "kind": "build", "build_id": build["build_id"]}
        )
        assert page1["entries"]

        # Repeated reads are charged even when the content is unchanged.
        _s, page2 = h.get(
            "/v1/feedback", query={"candidate_id": sub["candidate_id"], "kind": "build", "build_id": build["build_id"]}
        )
        ledger = h.service.store.query_one(
            "SELECT * FROM feedback_ledger WHERE session_id=? AND tree_hash=?",
            (h.session_id, sub["tree_hash"]),
        )
        assert ledger["pages_used"] >= 2, dict(ledger)
        assert ledger["bytes_used"] > 0, dict(ledger)
    finally:
        h.close()


def test_build_diagnostic_cannot_be_read_without_candidate_scope() -> None:
    h = Harness(build_fails=True)
    try:
        sub = h.submit("submit-key-0000000804")
        build = h.build("build-key-00000000804", sub["candidate_id"])
        status, body = h.get(
            "/v1/feedback", query={"candidate_id": "", "kind": "build", "build_id": build["build_id"]}
        )
        assert status in (404, 400), body
    finally:
        h.close()


def test_runtime_diagnostics_require_the_candidates_own_attempt() -> None:
    h = Harness()
    try:
        sub_a = h.submit("submit-key-0000000805", files=make_source_files(body="public final class BA {}"))
        build_a = h.build("build-key-00000000805", sub_a["candidate_id"])
        attempt_a = h.begin("attempt-key-0000000805", build_a["build_id"])["attempt_id"]
        h.launch("launch-key-0000000805", attempt_a)

        # Only one attempt may be open per session, so A must close before B
        # can start.
        supervisor = h.make_supervisor()
        h.clock.advance(30)
        supervisor.sweep()

        sub_b = h.submit("submit-key-0000000806", files=make_source_files(body="public final class BB {}"))
        build_b = h.build("build-key-00000000806", sub_b["candidate_id"])
        attempt_b = h.begin("attempt-key-0000000806", build_b["build_id"])["attempt_id"]
        assert attempt_b != attempt_a
        h.launch("launch-key-0000000806", attempt_b)
        h.clock.advance(30)
        supervisor.sweep()

        # Cross-candidate attempt IDs are refused as not-found, not malformed.
        status, body = h.get(
            "/v1/feedback",
            query={"candidate_id": sub_a["candidate_id"], "kind": "runtime", "attempt_id": attempt_b},
        )
        assert status == 404, body

        # Runtime reads without an attempt_id are refused.
        status, body = h.get(
            "/v1/feedback", query={"candidate_id": sub_a["candidate_id"], "kind": "runtime"}
        )
        assert status == 400, body

        # The candidate's own attempt is accepted.
        status, body = h.get(
            "/v1/feedback",
            query={"candidate_id": sub_a["candidate_id"], "kind": "runtime", "attempt_id": attempt_a},
        )
        assert status == 200, body
        assert body["entries"], body
    finally:
        h.close()


def test_identical_source_tree_does_not_get_a_second_build() -> None:
    """The same frozen tree hash cannot be built twice via a new candidate id."""

    h = Harness()
    try:
        first = h.submit("submit-key-0000000807", files=make_source_files(body="public final class Same {}"))
        build1 = h.build("build-key-00000000807", first["candidate_id"])
        assert build1["state"] == "SUCCEEDED", build1

        duplicate = h.submit("submit-key-0000000808", files=make_source_files(body="public final class Same {}"))
        assert duplicate["candidate_id"] == first["candidate_id"]
        assert duplicate["status"] == "duplicate", duplicate

        # A new idempotency key on the same candidate cannot buy a second build.
        build2 = h.build("build-key-00000000808", duplicate["candidate_id"])
        assert build2["state"] == "REJECTED", build2
        assert build2["reason_code"] == "BUDGET_EXHAUSTED"
        assert build2["build_id"] is None

        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:builds"]["consumed"] == 1, snapshot
    finally:
        h.close()


def test_policy_rejection_is_visible_as_diagnostics_without_an_attempt() -> None:
    from runner.config import and000008_config

    h = Harness(config=and000008_config())
    try:
        sub = h.submit(
            "submit-key-0000000809",
            files=[
                {
                    "relative_path": "src/main/java/org/androidpoc/and000008/candidate/Bad.java",
                    "content_base64": __import__("base64").b64encode(
                        b"package com.wrong.namespace;\n"
                    ).decode("ascii"),
                }
            ],
        )
        assert sub["status"] == "rejected", sub
        assert sub["reason_code"] == "SOURCE_POLICY_REJECTED"
        # No candidate, no build, no attempt was created for a policy rejection.
        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM candidates")["n"] == 0
        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM builds")["n"] == 0
        assert h.service.store.query_one("SELECT COUNT(*) AS n FROM attempts")["n"] == 0
    finally:
        h.close()


def test_build_failure_diagnostics_are_billed_to_the_candidate_tree_hash() -> None:
    """Build and runtime diagnostics share one per-candidate budget."""

    h = Harness(build_fails=True)
    try:
        sub = h.submit("submit-key-0000000810")
        build = h.build("build-key-00000000810", sub["candidate_id"])
        for _ in range(2):
            h.get(
                "/v1/feedback",
                query={"candidate_id": sub["candidate_id"], "kind": "build", "build_id": build["build_id"]},
            )
        rows = h.service.store.query_all("SELECT * FROM feedback_ledger WHERE session_id=?", (h.session_id,))
        assert len(rows) == 1, [dict(r) for r in rows]
        assert rows[0]["tree_hash"] == sub["tree_hash"], dict(rows[0])
        assert rows[0]["pages_used"] == 2, dict(rows[0])
    finally:
        h.close()
