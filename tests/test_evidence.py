"""Evidence: SHA-256 addressing, sealing order, hash verification, crash recovery."""

from __future__ import annotations

import hashlib
import os

from harness import Harness, make_source_files
from runner.errors import StorageError


def _attempt(h: Harness, index: int = 1) -> str:
    sub = h.submit(f"submit-key-{index:013d}", files=make_source_files(body=f"public final class E{index} {{}}"))
    build = h.build(f"build-key-{index:014d}", sub["candidate_id"])
    return h.begin(f"attempt-key-{index:013d}", build["build_id"])["attempt_id"]


def test_objects_are_content_addressed_and_verified() -> None:
    h = Harness()
    try:
        payload = b"evidence-bytes-for-hashing\n"
        artifact_id = h.service.artifacts.put(
            session_id=h.session_id,
            kind="metadata",
            media_type="text/plain",
            data=payload,
            source="test",
        )
        expected = hashlib.sha256(payload).hexdigest()
        row = h.service.store.query_one("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
        assert row["content_sha256"] == expected
        assert row["state"] == "sealed"
        assert row["size_bytes"] == len(payload)

        # The object lives at objects/<aa>/<sha256>.
        path = h.service.artifacts.object_path(expected)
        assert os.path.exists(path)
        assert os.path.basename(path) == expected
        assert os.path.basename(os.path.dirname(path)) == expected[:2]
        assert h.service.artifacts.read_object(artifact_id) == payload

        # Identical content with a different declaration reuses the same object.
        again = h.service.artifacts.put(
            session_id=h.session_id,
            kind="metadata",
            media_type="text/plain",
            data=payload,
            source="test",
        )
        row2 = h.service.store.query_one("SELECT * FROM artifacts WHERE artifact_id=?", (again,))
        assert row2["content_sha256"] == expected
        assert h.service.artifacts.read_object(again) == payload
    finally:
        h.close()


def test_expected_digest_mismatch_is_refused() -> None:
    h = Harness()
    try:
        try:
            h.service.artifacts.put(
                session_id=h.session_id,
                kind="metadata",
                media_type="text/plain",
                data=b"actual",
                source="test",
                expected_sha256=hashlib.sha256(b"different").hexdigest(),
            )
        except StorageError:
            pass
        else:  # pragma: no cover
            raise AssertionError("digest mismatch should have been refused")
        row = h.service.store.query_one(
            "SELECT state FROM artifacts WHERE source='test' ORDER BY created_utc DESC LIMIT 1"
        )
        assert row["state"] == "corrupt", dict(row)
    finally:
        h.close()


def test_unsealed_object_is_never_handed_to_the_oracle() -> None:
    h = Harness()
    try:
        attempt_id = _attempt(h, 2)
        h.launch("launch-key-0000000601", attempt_id)
        supervisor = h.make_supervisor()
        h.clock.advance(30)
        supervisor.sweep()

        # Add a staged (never sealed) evidence object to a closed attempt and
        # confirm the Oracle input excludes it.
        with h.service.store.txn():
            staged_id, staging_path = h.service.artifacts.declare(
                session_id=h.session_id,
                kind="execution_evidence",
                media_type="application/x-ndjson",
                source="collector",
                provenance="device_observed",
                attempt_id=attempt_id,
            )
        with open(staging_path, "wb") as handle:
            handle.write(b'{"never":"sealed"}\n')

        evidence = h.service.finalizer_evidence_for_test(attempt_id) if hasattr(
            h.service, "finalizer_evidence_for_test"
        ) else None

        from runner.core.attempt import AttemptFinalizer

        finalizer = AttemptFinalizer(
            store=h.service.store,
            artifacts=h.service.artifacts,
            events=h.service.events,
            clock=h.clock,
            runtime=h.service.runtime,
            config=h.config,
            device=h.device,
        )
        sealed = finalizer.sealed_evidence(attempt_id)
        assert all(e.artifact_id != staged_id for e in sealed), "staged object leaked to the Oracle"

        # And direct reads of an unsealed object are refused.
        try:
            h.service.artifacts.read_object(staged_id)
        except StorageError:
            pass
        else:  # pragma: no cover
            raise AssertionError("unsealed object was readable")
    finally:
        h.close()


def test_evidence_group_digest_covers_sorted_member_hashes() -> None:
    h = Harness()
    try:
        a = h.service.artifacts.put(
            session_id=h.session_id, kind="metadata", media_type="text/plain", data=b"aa", source="test"
        )
        b = h.service.artifacts.put(
            session_id=h.session_id, kind="metadata", media_type="text/plain", data=b"bb", source="test"
        )
        with h.service.store.txn():
            seal_id = h.service.artifacts.open_seal_group(session_id=h.session_id, kind="metadata")
            digest = h.service.artifacts.seal_group(
                seal_id, member_artifact_ids=[a, b], session_id=h.session_id
            )

        hashes = sorted(
            h.service.store.query_one("SELECT content_sha256 FROM artifacts WHERE artifact_id=?", (x,))[
                "content_sha256"
            ]
            for x in (a, b)
        )
        expected = hashlib.sha256("\n".join(hashes).encode("utf-8")).hexdigest()
        assert digest == expected
        row = h.service.store.query_one("SELECT * FROM seal_groups WHERE seal_id=?", (seal_id,))
        assert row["state"] == "sealed"
        assert row["group_sha256"] == expected
    finally:
        h.close()


def test_sealing_a_group_with_an_unsealed_member_is_refused() -> None:
    h = Harness()
    try:
        with h.service.store.txn():
            artifact_id, staging_path = h.service.artifacts.declare(
                session_id=h.session_id,
                kind="metadata",
                media_type="text/plain",
                source="test",
            )
        with open(staging_path, "wb") as handle:
            handle.write(b"pending")
        with h.service.store.txn():
            seal_id = h.service.artifacts.open_seal_group(session_id=h.session_id, kind="metadata")
            try:
                h.service.artifacts.seal_group(
                    seal_id, member_artifact_ids=[artifact_id], session_id=h.session_id
                )
            except StorageError:
                pass
            else:  # pragma: no cover
                raise AssertionError("group sealed with an unsealed member")
    finally:
        h.close()


def test_execution_evidence_hash_matches_sealed_bytes() -> None:
    """The recorded digest is reproducible from the sealed evidence bytes."""

    h = Harness()
    try:
        attempt_id = _attempt(h, 3)
        h.launch("launch-key-0000000602", attempt_id)
        supervisor = h.make_supervisor()
        h.clock.advance(30)
        supervisor.sweep()

        rows = h.service.store.query_all(
            "SELECT artifact_id, content_sha256, size_bytes FROM artifacts "
            "WHERE attempt_id=? AND kind IN ('execution_evidence','cleanup_evidence')",
            (attempt_id,),
        )
        assert rows, "no sealed evidence"
        for row in rows:
            data = h.service.artifacts.read_object(row["artifact_id"])
            assert hashlib.sha256(data).hexdigest() == row["content_sha256"]
            assert len(data) == row["size_bytes"]

        # The launch-phase evidence must still carry its launch records and must
        # not have been extended by cleanup output.
        launch_rows = h.service.store.query_all(
            "SELECT artifact_id FROM artifacts WHERE attempt_id=? AND kind='execution_evidence'",
            (attempt_id,),
        )
        combined = b"".join(h.service.artifacts.read_object(r["artifact_id"]) for r in launch_rows)
        assert b"launch" in combined
        assert b"collector_stopped" not in combined, "cleanup output leaked into the execution window"
    finally:
        h.close()


def test_byte_range_sealing_does_not_duplicate_content_across_phases() -> None:
    h = Harness()
    try:
        attempt_id = _attempt(h, 4)
        h.launch("launch-key-0000000603", attempt_id)
        supervisor = h.make_supervisor()
        h.clock.advance(30)
        supervisor.sweep()

        exec_hashes = {
            r["content_sha256"]
            for r in h.service.store.query_all(
                "SELECT content_sha256 FROM artifacts WHERE attempt_id=? AND kind='execution_evidence'",
                (attempt_id,),
            )
        }
        cleanup_hashes = {
            r["content_sha256"]
            for r in h.service.store.query_all(
                "SELECT content_sha256 FROM artifacts WHERE attempt_id=? AND kind='cleanup_evidence'",
                (attempt_id,),
            )
        }
        assert exec_hashes
        assert cleanup_hashes
        assert not (exec_hashes & cleanup_hashes)

        # Recovering twice must be idempotent and must not corrupt anything.
        supervisor.sweep()
        rows = h.service.store.query_all(
            "SELECT state, COUNT(*) AS n FROM artifacts WHERE attempt_id=? GROUP BY state", (attempt_id,)
        )
        assert all(r["state"] == "sealed" for r in rows), [dict(r) for r in rows]
    finally:
        h.close()


def test_source_snapshot_hash_round_trip() -> None:
    h = Harness()
    try:
        sub = h.submit("submit-key-0000000604")
        row = h.service.store.query_one(
            "SELECT * FROM artifacts WHERE kind='source_snapshot' AND candidate_id=?",
            (sub["candidate_id"],),
        )
        assert row is not None and row["state"] == "sealed"
        data = h.service.artifacts.read_object(row["artifact_id"])
        assert hashlib.sha256(data).hexdigest() == row["content_sha256"]

        # Re-building reads the frozen snapshot and re-verifies every file hash.
        build = h.build("build-key-00000000604", sub["candidate_id"])
        assert build["state"] == "SUCCEEDED", build
    finally:
        h.close()


def test_source_snapshot_tampering_is_detected() -> None:
    h = Harness()
    try:
        sub = h.submit("submit-key-0000000605")
        row = h.service.store.query_one(
            "SELECT * FROM artifacts WHERE kind='source_snapshot' AND candidate_id=?",
            (sub["candidate_id"],),
        )
        # Overwrite the object in place: the read must fail verification.
        path = h.service.artifacts.object_path(row["content_sha256"])
        with open(path, "wb") as handle:
            handle.write(b'{"tree_hash":"tampered","files":[]}')

        from runner.errors import StorageError

        try:
            h.service.artifacts.read_object(row["artifact_id"])
        except StorageError:
            pass
        else:  # pragma: no cover
            raise AssertionError("tampered object was readable")

        build = h.build("build-key-00000000605", sub["candidate_id"])
        assert build["state"] == "FAILED", build
        assert build["reason_code"] == "BUILD_FAILED"
    finally:
        h.close()


def test_sealed_artifact_with_missing_object_is_corrupt_and_blocks_oracle() -> None:
    h = Harness()
    try:
        attempt_id = _attempt(h, 5)
        h.launch("launch-key-0000000604", attempt_id)
        supervisor = h.make_supervisor()
        h.clock.advance(30)
        supervisor.sweep()

        row = h.service.store.query_one(
            "SELECT artifact_id, content_sha256 FROM artifacts "
            "WHERE attempt_id=? AND kind='execution_evidence' LIMIT 1",
            (attempt_id,),
        )
        os.remove(h.service.artifacts.object_path(row["content_sha256"]))

        # Force a re-evaluation pass: the corrupt object must not reach the Oracle.
        h.service.store.execute("DELETE FROM oracle_evaluations WHERE attempt_id=?", (attempt_id,))
        h.service.store.execute(
            "UPDATE attempts SET evaluation_status=NULL, oracle_verdict=NULL WHERE attempt_id=?",
            (attempt_id,),
        )
        summary = supervisor.sweep()
        assert summary["storage"]["corrupt"] >= 1, summary

        group = h.service.store.query_one(
            "SELECT state FROM seal_groups WHERE attempt_id=? AND kind='execution_evidence'",
            (attempt_id,),
        )
        assert group["state"] == "corrupt", dict(group)
    finally:
        h.close()
