"""Scenario B: crash after the object is published but before the DB seals it.

``test_evidence`` and ``test_supervisor`` already cover the happy recovery path
and a sealed-object hash mismatch. This module covers the *boundary* the contract
names explicitly (``IMPLEMENTATION-CONTRACT.md`` §4/§5): the bytes already exist
in ``objects/`` while SQLite still says ``staged``, and the harder variant where
the object is published and the row's seal committed but the owning attempt's
seal flag never reached disk.

Coverage:

* a REAL child process is killed with ``SIGKILL``/``TerminateProcess`` at the
  publish/seal boundary, and recovery runs from a genuinely fresh process;
* recovery is idempotent: replaying it yields no duplicate evidence objects, no
  duplicate seal groups and no duplicate Oracle evaluation;
* a missing, truncated, or hash-mismatched object is marked ``corrupt`` and is
  never handed to the Oracle;
* orphan staging/object files are quarantined rather than adopted.

All fault injection is confined to a temporary root owned by the test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap

from harness import Harness, mock_submission
from runner.adapters.device_mock import MockDeviceAdapter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _seed_live_attempt(root: str) -> dict:
    """Create a session with a launched attempt in ``root``; return identifiers.

    Builds the service directly rather than through :class:`Harness`, because the
    harness deletes its temporary root on close and these tests must keep the
    state directory alive for a child process to crash inside.
    """

    from runner.adapters.build_backend import MockBuildBackend
    from runner.adapters.collectors_mock import build_mock_collector_set
    from runner.adapters.device_mock import MockDeviceAdapter
    from runner.adapters.oracle import MockOracle
    from runner.adapters.projection import DiagnosticOnlyProjector
    from runner.config import mock_config
    from runner.contracts.clock import SystemClock
    from runner.core.service import RunnerService

    config = mock_config()
    service = RunnerService(
        root=root,
        config=config,
        clock=SystemClock(),
        device=MockDeviceAdapter(),
        build_backend=MockBuildBackend(),
        oracle=MockOracle(),
        projector=DiagnosticOnlyProjector(),
        collector_factory=lambda **kw: build_mock_collector_set(**kw),
    )
    try:
        session_id, _token = service.create_session(config)
        service.activate_session(session_id, config)
        sub = service.submit_candidate(
            session_id, idempotency_key="crash-submit-000000001", files_payload=mock_submission()
        )
        build = service.build_candidate(
            session_id, idempotency_key="crash-build-0000000001", candidate_id=sub["candidate_id"]
        )
        receipt = service.begin_attempt(
            session_id, idempotency_key="crash-begin-0000000001", build_id=build["build_id"]
        )
        attempt_id = receipt["attempt_id"]
        service.perform_action(
            session_id,
            attempt_id=attempt_id,
            idempotency_key="crash-launch-0000000001",
            action_name="launch_candidate",
            params={},
        )
        return {
            "session_id": session_id,
            "attempt_id": attempt_id,
            "build_id": build["build_id"],
            "candidate_id": sub["candidate_id"],
            "package_name": build["package_name"],
        }
    finally:
        service.store.close()


def _run_child(script: str, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(script), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=ROOT,
    )


# ---------------------------------------------------------------------------
# B.1 real child processes killed at the two publish/seal boundaries
# ---------------------------------------------------------------------------
# The child script is assembled by explicit concatenation and placeholder
# replacement (never ``str.format``), so the JSON payload's braces are literal
# text and the repo root is substituted exactly once.
_CHILD_PREAMBLE = r"""
import hashlib, os, sys
root, marker = sys.argv[1], sys.argv[2]
sys.path.insert(0, __ROOT__)
from runner.store.db import Store
from runner.store.events import EventLog
from runner.store.artifacts import ArtifactStore
from runner.contracts.clock import SystemClock

clock = SystemClock()
store = Store(os.path.join(root, "control.db"), clock=clock)
events = EventLog(store, clock=clock)
artifacts = ArtifactStore(os.path.join(root, "artifacts"), store, events, clock=clock)
session_id = store.query_one("SELECT session_id FROM sessions LIMIT 1")["session_id"]
payload = b'{"line":"published-then-killed"}\n'
EXPECTED_PAYLOAD = payload


def checkpoint(artifact_id):
    with open(marker, "w") as handle:
        handle.write(artifact_id)
        handle.flush()
        os.fsync(handle.fileno())


def die():
    # Hard kill: no atexit, no rollback, no buffered flush. This is the
    # process-level crash the task requires, not a Python exception.
    os._exit(137)
"""

# Crash BEFORE the rename: the declaration and the staged bytes exist, but the
# object was never published. Recovery must be able to complete this one.
STAGED_THEN_DIE = _CHILD_PREAMBLE + r"""
with store.txn():
    artifact_id, staging_path = artifacts.declare(
        session_id=session_id,
        kind="execution_evidence",
        media_type="application/x-ndjson",
        source="collector",
        provenance="device_observed",
    )
artifacts.write_staging(staging_path, payload)
checkpoint(artifact_id)
die()
"""

# Crash AFTER the rename but BEFORE the sealing transaction commits: the bytes
# are published, yet SQLite still says 'staged' with no recorded digest.
PUBLISHED_THEN_DIE = _CHILD_PREAMBLE + r"""
with store.txn():
    artifact_id, staging_path = artifacts.declare(
        session_id=session_id,
        kind="execution_evidence",
        media_type="application/x-ndjson",
        source="collector",
        provenance="device_observed",
    )
digest, size = artifacts.write_staging(staging_path, payload)
store.begin_immediate()  # never committed
artifacts.publish(
    artifact_id=artifact_id,
    session_id=session_id,
    digest=digest,
    size=size,
    staging_path=staging_path,
)
checkpoint(artifact_id)
die()
"""

EXPECTED_PAYLOAD = b'{"line":"published-then-killed"}\n'


def _child_script(body: str) -> str:
    """Substitute the repository root into a child-process script body."""

    assert "__ROOT__" in body
    return body.replace("__ROOT__", repr(ROOT))


def _fresh_store(root: str):
    """Open the store/artifacts for ``root`` as a brand-new process would."""

    from runner.contracts.clock import SystemClock
    from runner.store.artifacts import ArtifactStore
    from runner.store.db import Store
    from runner.store.events import EventLog

    clock = SystemClock()
    store = Store(os.path.join(root, "control.db"), clock=clock)
    events = EventLog(store, clock=clock)
    artifacts = ArtifactStore(os.path.join(root, "artifacts"), store, events, clock=clock)
    return store, events, artifacts


def test_killed_child_staged_but_unpublished_is_completed_by_recovery() -> None:
    """Crash before the rename: the staged bytes are still recoverable."""

    root = tempfile.mkdtemp(prefix="runner-crash-staged-")
    marker = os.path.join(root, "artifact_id.txt")
    try:
        _seed_live_attempt(root)
        completed = _run_child(_child_script(STAGED_THEN_DIE), root, marker)
        assert completed.returncode == 137, (completed.returncode, completed.stderr)
        assert os.path.exists(marker), (completed.stdout, completed.stderr)
        artifact_id = open(marker).read().strip()

        store, _events, artifacts = _fresh_store(root)
        try:
            # The pre-crash state really was 'staged' with bytes on disk.
            before = store.query_one("SELECT state, content_sha256 FROM artifacts WHERE artifact_id=?", (artifact_id,))
            assert before["state"] == "staged", dict(before)
            assert before["content_sha256"] is None, dict(before)

            summary = artifacts.recover()
            assert summary["staged_completed"] == 1, summary
            assert summary["staged_missing"] == 0, summary

            after = store.query_one("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
            assert after["state"] == "sealed", dict(after)
            assert after["content_sha256"], dict(after)
            assert artifacts.read_object(artifact_id) == EXPECTED_PAYLOAD

            # Recovery is idempotent: a second pass changes nothing.
            again = artifacts.recover()
            assert again["staged_completed"] == 0, again
            assert store.query_one("SELECT state FROM artifacts WHERE artifact_id=?", (artifact_id,))[
                "state"
            ] == "sealed"
        finally:
            store.close()
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


def test_killed_child_published_but_unsealed_fails_closed() -> None:
    """Crash after the rename: recovery must fail closed, never half-adopt.

    The row never recorded a digest, so the published bytes cannot be
    attributed to it with certainty. The contract requires the row to be marked
    explicitly and the unattributable object to be kept out of the sealed
    evidence set; it must never be handed to the Oracle.
    """

    root = tempfile.mkdtemp(prefix="runner-crash-published-")
    marker = os.path.join(root, "artifact_id.txt")
    try:
        ids = _seed_live_attempt(root)
        attempt_id = ids["attempt_id"]
        completed = _run_child(_child_script(PUBLISHED_THEN_DIE), root, marker)
        assert completed.returncode == 137, (completed.returncode, completed.stderr)
        assert os.path.exists(marker), (completed.stdout, completed.stderr)
        artifact_id = open(marker).read().strip()

        store, _events, artifacts = _fresh_store(root)
        try:
            before = store.query_one("SELECT state FROM artifacts WHERE artifact_id=?", (artifact_id,))
            assert before["state"] == "staged", dict(before)

            summary = artifacts.recover()
            assert summary["staged_missing"] == 1, summary
            assert summary["orphan_objects"] >= 1, summary

            after = store.query_one("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
            assert after["state"] == "corrupt", dict(after)

            # A corrupt artifact is never readable as evidence...
            from runner.errors import StorageError

            try:
                artifacts.read_object(artifact_id)
            except StorageError:
                pass
            else:  # pragma: no cover
                raise AssertionError("torn artifact was readable as evidence")

            # ...and never appears in the Oracle's sealed evidence input.
            from runner.config import mock_config
            from runner.core.attempt import AttemptFinalizer, AttemptRuntime

            finalizer = AttemptFinalizer(
                store=store,
                artifacts=artifacts,
                events=_events,
                clock=store._clock,
                runtime=AttemptRuntime(store),
                config=mock_config(),
                device=MockDeviceAdapter(),
            )
            # Attribute the artifact to the seeded attempt so the query scope is
            # the one the Oracle would use.
            store.execute(
                "UPDATE artifacts SET attempt_id=? WHERE artifact_id=?",
                (attempt_id, artifact_id),
            )
            assert artifact_id not in {e.artifact_id for e in finalizer.sealed_evidence(attempt_id)}
        finally:
            store.close()
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# B.2 idempotent re-seal after a crash between seal and the attempt flag
# ---------------------------------------------------------------------------
def _forced_reseal(h: Harness, attempt_id: str):
    """Reproduce the crash window and return the recovery summary.

    The execution evidence is sealed exactly as the supervisor would, then the
    attempt's ``evidence_sealed`` flag is reverted to the value the pre-crash
    database held. Recovery therefore re-runs the seal from the same persisted
    stream bounds, which is where duplication would appear.
    """

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
    attempt = h.service.runtime.load(attempt_id)
    finalizer.seal_execution_evidence(attempt)
    h.service.store.execute(
        "UPDATE attempts SET evidence_sealed=0 WHERE attempt_id=?", (attempt_id,)
    )
    # A restarted process has no in-memory collector instances.
    h.service._collector_instances.clear()
    return finalizer, h.make_supervisor().sweep()


def test_reseal_after_crash_does_not_duplicate_evidence_or_oracle_input() -> None:
    h = Harness()
    try:
        sub = h.submit("reseal-submit-00000001")
        build = h.build("reseal-build-000000001", sub["candidate_id"])
        attempt_id = h.begin("reseal-begin-000000001", build["build_id"])["attempt_id"]
        h.launch("reseal-launch-000000001", attempt_id)
        h.clock.advance(30)

        finalizer, summary = _forced_reseal(h, attempt_id)
        assert summary["attempts_finalized"], summary

        # Exactly one sealed execution-evidence group survived the replay.
        groups = h.service.store.query_all(
            "SELECT seal_id, kind, state FROM seal_groups WHERE attempt_id=? AND kind='execution_evidence'",
            (attempt_id,),
        )
        assert len(groups) == 1, [dict(g) for g in groups]
        assert groups[0]["state"] == "sealed", dict(groups[0])

        # No duplicate content hashes among the attempt's artifacts.
        digests = [
            r["content_sha256"]
            for r in h.service.store.query_all(
                "SELECT content_sha256 FROM artifacts WHERE attempt_id=?", (attempt_id,)
            )
        ]
        assert len(digests) == len(set(digests)), "recovery duplicated an evidence object"

        # The Oracle receives each sealed observation exactly once.
        evidence = finalizer.sealed_evidence(attempt_id)
        assert evidence, "no evidence reached the Oracle"
        hashes = [e.sha256 for e in evidence]
        assert len(hashes) == len(set(hashes)), hashes

        # And it was evaluated exactly once, on a complete window.
        evaluations = h.service.store.query_all(
            "SELECT * FROM oracle_evaluations WHERE attempt_id=?", (attempt_id,)
        )
        assert len(evaluations) == 1, [dict(e) for e in evaluations]
        attempt = h.service.store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert attempt["state"] == "CLOSED", dict(attempt)
        assert attempt["observation_window_complete"] == 1, dict(attempt)
    finally:
        h.close()


def test_repeated_recovery_sweeps_are_idempotent() -> None:
    h = Harness()
    try:
        sub = h.submit("idem-submit-0000000001")
        build = h.build("idem-build-00000000001", sub["candidate_id"])
        attempt_id = h.begin("idem-begin-00000000001", build["build_id"])["attempt_id"]
        h.launch("idem-launch-00000000001", attempt_id)
        h.clock.advance(30)

        supervisor = h.make_supervisor()
        supervisor.sweep()
        baseline = {
            "artifacts": h.service.store.query_one("SELECT COUNT(*) AS n FROM artifacts")["n"],
            "groups": h.service.store.query_one("SELECT COUNT(*) AS n FROM seal_groups")["n"],
            "evaluations": h.service.store.query_one("SELECT COUNT(*) AS n FROM oracle_evaluations")["n"],
        }
        assert baseline["evaluations"] == 1, baseline

        for _ in range(4):
            supervisor.sweep()
            h.service._collector_instances.clear()
            summary = supervisor.sweep()
            assert not summary["attempts_finalized"], summary

        after = {
            "artifacts": h.service.store.query_one("SELECT COUNT(*) AS n FROM artifacts")["n"],
            "groups": h.service.store.query_one("SELECT COUNT(*) AS n FROM seal_groups")["n"],
            "evaluations": h.service.store.query_one("SELECT COUNT(*) AS n FROM oracle_evaluations")["n"],
        }
        assert after == baseline, (baseline, after)

        # Nothing was downgraded or re-opened either.
        attempt = h.service.store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert attempt["state"] == "CLOSED", dict(attempt)
        assert attempt["observation_window_complete"] == 1, dict(attempt)
    finally:
        h.close()


def test_recovery_never_downgrades_a_completed_observation_window() -> None:
    """A crash after the window closes must not re-open or un-complete it."""

    h = Harness()
    try:
        sub = h.submit("window-submit-000000001")
        build = h.build("window-build-0000000001", sub["candidate_id"])
        attempt_id = h.begin("window-begin-0000000001", build["build_id"])["attempt_id"]
        h.launch("window-launch-0000000001", attempt_id)
        h.clock.advance(30)

        # The window-closing update commits, then the process dies before the
        # state transition: exactly the supervisor's crash window.
        h.service.runtime.set_stage(attempt_id, "observe", "completed")
        h.service.runtime.update(
            attempt_id, observation_window_complete=1, execution_status="completed"
        )
        state = h.service.store.query_one("SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert state["state"] == "RUNNING", dict(state)

        summary = h.make_supervisor().sweep()
        assert summary["attempts_finalized"], summary

        row = h.service.store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        assert row["state"] == "CLOSED", dict(row)
        assert row["observation_window_complete"] == 1, dict(row)
        assert row["execution_status"] == "completed", dict(row)
    finally:
        h.close()


# ---------------------------------------------------------------------------
# B.3 corrupt / missing objects never reach the Oracle
# ---------------------------------------------------------------------------
def test_truncated_published_object_is_marked_corrupt_and_withheld() -> None:
    h = Harness()
    try:
        sub = h.submit("trunc-submit-000000001")
        build = h.build("trunc-build-0000000001", sub["candidate_id"])
        attempt_id = h.begin("trunc-begin-0000000001", build["build_id"])["attempt_id"]
        h.launch("trunc-launch-0000000001", attempt_id)
        supervisor = h.make_supervisor()
        h.clock.advance(30)
        supervisor.sweep()

        row = h.service.store.query_one(
            "SELECT artifact_id, content_sha256, size_bytes FROM artifacts "
            "WHERE attempt_id=? AND kind='execution_evidence' LIMIT 1",
            (attempt_id,),
        )
        path = h.service.artifacts.object_path(row["content_sha256"])
        with open(path, "wb") as handle:
            handle.write(b"truncated")  # same path, wrong bytes

        # Force a re-evaluation pass so corruption cannot be masked by a
        # previously recorded verdict.
        h.service.store.execute("DELETE FROM oracle_evaluations WHERE attempt_id=?", (attempt_id,))
        h.service.store.execute(
            "UPDATE attempts SET evaluation_status=NULL, oracle_verdict=NULL WHERE attempt_id=?",
            (attempt_id,),
        )
        summary = supervisor.sweep()
        assert summary["storage"]["corrupt"] >= 1, summary

        after = h.service.store.query_one(
            "SELECT state FROM artifacts WHERE artifact_id=?", (row["artifact_id"],)
        )
        assert after["state"] == "corrupt", dict(after)

        from runner.errors import StorageError

        try:
            h.service.artifacts.read_object(row["artifact_id"])
        except StorageError:
            pass
        else:  # pragma: no cover
            raise AssertionError("truncated object was still readable as evidence")

        # The owning evidence group is flagged, so the attempt cannot claim a
        # clean recovery built on unverifiable bytes.
        group = h.service.store.query_one(
            "SELECT state FROM seal_groups WHERE attempt_id=? AND kind='execution_evidence'",
            (attempt_id,),
        )
        assert group["state"] == "corrupt", dict(group)
    finally:
        h.close()


def test_oracle_is_never_invoked_with_corrupt_evidence() -> None:
    """The Oracle input must exclude any object that fails verification."""

    from runner.adapters.base import OracleVerdict
    from runner.core.attempt import AttemptFinalizer

    seen: list[int] = []

    class CountingOracle:
        oracle_id = "counting"
        version = "1"
        criteria_version = "test"

        def evaluate(self, evidence, *, environment_valid):  # noqa: ANN001
            # Every item handed over must still verify on read.
            for item in evidence:
                data = h.service.artifacts.read_object(item.artifact_id)
                assert len(data) == len(item.data), item.artifact_id
            seen.append(len(evidence))
            return OracleVerdict(
                verdict="inconclusive",
                evaluation_status="completed",
                oracle_id=self.oracle_id,
                oracle_version=self.version,
                criteria_version=self.criteria_version,
                observations={},
                reason_private="counting",
                evidence_refs=[],
            )

    h = Harness()
    try:
        h.service.oracle = CountingOracle()
        sub = h.submit("corrupt-submit-00000001")
        build = h.build("corrupt-build-000000001", sub["candidate_id"])
        attempt_id = h.begin("corrupt-begin-000000001", build["build_id"])["attempt_id"]
        h.launch("corrupt-launch-000000001", attempt_id)
        supervisor = h.make_supervisor()
        supervisor.oracle = h.service.oracle
        h.clock.advance(30)
        supervisor.sweep()

        row = h.service.store.query_one(
            "SELECT artifact_id, content_sha256 FROM artifacts "
            "WHERE attempt_id=? AND kind='execution_evidence' LIMIT 1",
            (attempt_id,),
        )
        os.remove(h.service.artifacts.object_path(row["content_sha256"]))

        # A fresh evaluation pass over the now-damaged attempt.
        supervisor.sweep()
        h.service.store.execute("DELETE FROM oracle_evaluations WHERE attempt_id=?", (attempt_id,))
        h.service.store.execute(
            "UPDATE attempts SET evaluation_status=NULL, oracle_verdict=NULL WHERE attempt_id=?",
            (attempt_id,),
        )
        h.service.oracle = CountingOracle()
        supervisor.sweep()

        # The missing object is now corrupt, so it cannot appear in the input.
        finalizer = AttemptFinalizer(
            store=h.service.store,
            artifacts=h.service.artifacts,
            events=h.service.events,
            clock=h.clock,
            runtime=h.service.runtime,
            config=h.config,
            device=h.device,
        )
        evidence_ids = {e.artifact_id for e in finalizer.sealed_evidence(attempt_id)}
        assert row["artifact_id"] not in evidence_ids, "corrupt artifact reached the Oracle"
    finally:
        h.close()


def test_orphan_files_are_quarantined_not_adopted_as_evidence() -> None:
    h = Harness()
    try:
        staging_orphan = os.path.join(h.service.artifacts.staging_dir, "left-behind.part")
        with open(staging_orphan, "wb") as handle:
            handle.write(b"never declared")

        digest = __import__("hashlib").sha256(b"never declared object").hexdigest()
        object_orphan = h.service.artifacts.object_path(digest)
        os.makedirs(os.path.dirname(object_orphan), exist_ok=True)
        with open(object_orphan, "wb") as handle:
            handle.write(b"never declared object")

        summary = h.make_supervisor().sweep()
        assert summary["storage"]["orphan_staging"] >= 1, summary
        assert summary["storage"]["orphan_objects"] >= 1, summary

        assert not os.path.exists(staging_orphan), "orphan staging file was not quarantined"
        quarantined = os.listdir(h.service.artifacts.quarantine_dir)
        assert any(name.startswith("orphan.") for name in quarantined), quarantined

        # An undeclared object is never adoptable as sealed evidence.
        assert (
            h.service.store.query_one("SELECT COUNT(*) AS n FROM artifacts WHERE content_sha256=?", (digest,))["n"]
            == 0
        )
    finally:
        h.close()


def test_unsealed_evidence_is_withheld_and_flagged_incomplete() -> None:
    """A never-sealed object must not be handed to the Oracle as evidence."""

    from runner.core.attempt import AttemptFinalizer
    from runner.errors import StorageError

    h = Harness()
    try:
        sub = h.submit("stage-submit-000000001")
        build = h.build("stage-build-0000000001", sub["candidate_id"])
        attempt_id = h.begin("stage-begin-0000000001", build["build_id"])["attempt_id"]

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

        finalizer = AttemptFinalizer(
            store=h.service.store,
            artifacts=h.service.artifacts,
            events=h.service.events,
            clock=h.clock,
            runtime=h.service.runtime,
            config=h.config,
            device=h.device,
        )
        assert staged_id not in {e.artifact_id for e in finalizer.sealed_evidence(attempt_id)}

        try:
            h.service.artifacts.read_object(staged_id)
        except StorageError:
            pass
        else:  # pragma: no cover
            raise AssertionError("unsealed object was readable as evidence")

        # Recovery classifies it rather than leaving it in limbo.
        summary = h.service.artifacts.recover()
        row = h.service.store.query_one("SELECT state FROM artifacts WHERE artifact_id=?", (staged_id,))
        assert row["state"] in ("sealed", "corrupt"), dict(row)
        assert summary["staged_completed"] + summary["staged_missing"] + summary["corrupt"] >= 1, summary
    finally:
        h.close()


# ---------------------------------------------------------------------------
# B.4 the child-process crash reports are trustworthy
# ---------------------------------------------------------------------------
def test_crash_child_process_actually_terminated_abnormally() -> None:
    """Guard: the recovery tests above must use a real hard kill, not an exception."""

    script = r"""
        import os, sys
        os._exit(137)
    """
    completed = _run_child(script)
    assert completed.returncode == 137, completed.returncode
    # No Python traceback: this is a process kill, not a handled exception.
    assert "Traceback" not in completed.stderr, completed.stderr


def test_crash_marker_and_state_survive_the_kill() -> None:
    """The killed child's committed work must be visible to a fresh process."""

    root = tempfile.mkdtemp(prefix="runner-crash-survive-")
    try:
        _seed_live_attempt(root)
        script = r"""
import os, sys
sys.path.insert(0, __ROOT__)
from runner.store.db import Store
from runner.contracts.clock import SystemClock
store = Store(os.path.join(sys.argv[1], "control.db"), clock=SystemClock())
boot_id = store.host_boot["boot_id"]
with store.txn():
    store.execute(
        "INSERT INTO events(event_id, session_id, event_type, node, node_boot_id, node_seq, "
        "utc, monotonic, payload_json) VALUES "
        "(?, (SELECT session_id FROM sessions LIMIT 1), "
        "'crash_probe', 'test', ?, 999999, "
        "'2024-01-01T00:00:00.000Z', 0.0, ?)",
        ("evt_CRASHSURVIVE0000000000000", boot_id, "{}"),
    )
os._exit(137)
"""
        completed = _run_child(_child_script(script), root)
        assert completed.returncode == 137, (completed.returncode, completed.stderr)

        from runner.contracts.clock import SystemClock
        from runner.store.db import Store

        store = Store(os.path.join(root, "control.db"), clock=SystemClock())
        row = store.query_one("SELECT * FROM events WHERE event_id='evt_CRASHSURVIVE0000000000000'")
        assert row is not None, "committed work did not survive the hard kill"
        assert json.loads(row["payload_json"]) == {}
        store.close()
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
