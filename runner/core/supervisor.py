"""Independent supervisor: finalizes by persisted deadline, with no Agent polling.

This is a separate process (``python -m runner supervisor``) that reads only
SQLite and on-disk collector streams. It therefore completes sealing, stopping
and state reconciliation for a mock attempt even if the API process died or the
client never polled again.

Sweep order (IMPLEMENTATION-CONTRACT.md 搂7):
  1. reconcile staging/objects against SQLite
  2. sessions whose wall clock or liveness expired -> CLOSING -> drain -> CLOSED
  3. attempts past their prepare / launch / observation deadlines -> FINALIZING
  4. every FINALIZING attempt -> seal execution evidence, stop candidate,
     seal cleanup evidence, restore + health-check, CLOSED
  5. dangling reservations -> settle and mark OUTCOME_UNKNOWN
  6. sealed, unevaluated attempts -> private Oracle evaluation
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from runner.adapters.base import SealedEvidence
from runner.config import SessionConfig
from runner.contracts.clock import Clock, epoch_from_iso, iso_from_epoch
from runner.contracts.ids import new_id
from runner.contracts.states import (
    ATTEMPT_ACCEPTED,
    ATTEMPT_CLOSED,
    ATTEMPT_FINALIZING,
    ATTEMPT_PREPARING,
    ATTEMPT_RUNNING,
    OP_OUTCOME_UNKNOWN,
    SESSION_ACTIVE,
    SESSION_CLOSED,
    SESSION_CLOSING,
    SESSION_CREATED,
)
from runner.core.attempt import AttemptFinalizer, AttemptRuntime, collector_task_handles
from runner.store.artifacts import ArtifactStore
from runner.store.db import Store
from runner.store.events import EventLog

SUPERVISOR_ID = "runner-supervisor"
DEFAULT_INTERVAL_SECONDS = 0.05
# Agent liveness: how long without contact before the session is closed.
AGENT_LIVENESS_GRACE_SECONDS = 900.0


class Supervisor:
    def __init__(
        self,
        *,
        store: Store,
        artifacts: ArtifactStore,
        events: EventLog,
        clock: Clock,
        config: SessionConfig,
        device: Any,
        oracle: Any,
        projector: Any | None = None,
        session_service: Any | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.events = events
        self.clock = clock
        self.config = config
        self.device = device
        self.oracle = oracle
        if projector is None:
            from runner.adapters.projection import DiagnosticOnlyProjector

            projector = DiagnosticOnlyProjector()
        self.projector = projector
        self.runtime = AttemptRuntime(store)
        self.finalizer = AttemptFinalizer(
            store=store,
            artifacts=artifacts,
            events=events,
            clock=clock,
            runtime=self.runtime,
            config=config,
            device=device,
        )
        self.session_service = session_service

    # ------------------------------------------------------------------
    def register(self) -> None:
        now = self.clock.utc()
        with self.store.txn():
            self.store.execute(
                """
                INSERT INTO supervisor_state(supervisor_id, node_boot_id, started_utc, heartbeat_utc, last_sweep_utc)
                VALUES (?,?,?,?,NULL)
                ON CONFLICT(supervisor_id) DO UPDATE SET
                    node_boot_id=excluded.node_boot_id,
                    started_utc=excluded.started_utc,
                    heartbeat_utc=excluded.heartbeat_utc
                """,
                (SUPERVISOR_ID, self.clock.boot_id(), now, now),
            )

    def heartbeat(self) -> None:
        self.store.execute(
            "UPDATE supervisor_state SET heartbeat_utc=?, last_sweep_utc=? WHERE supervisor_id=?",
            (self.clock.utc(), self.clock.utc(), SUPERVISOR_ID),
        )

    # ------------------------------------------------------------------
    def sweep(self) -> dict[str, Any]:
        """One supervisor pass. Returns a summary for logging/tests."""

        summary: dict[str, Any] = {
            "storage": {},
            "sessions_closing": [],
            "sessions_closed": [],
            "attempts_finalized": [],
            "attempts_interrupted": [],
            "dangling_reservations": [],
            "evaluations": [],
        }
        summary["storage"] = self.artifacts.recover()
        # Confirm which host boot this sweep is running in before any persisted
        # monotonic deadline is compared.
        self.store.refresh_host_boot()
        self._sweep_sessions(summary)
        self._sweep_attempts(summary)
        self._sweep_dangling_reservations(summary)
        self._sweep_evaluations(summary)
        self.heartbeat()
        return summary

    # -- sessions ------------------------------------------------------------
    def _sweep_sessions(self, summary: dict[str, Any]) -> None:
        rows = self.store.query_all(
            "SELECT * FROM sessions WHERE state IN (?,?,?)",
            (SESSION_CREATED, SESSION_ACTIVE, SESSION_CLOSING),
        )
        for row in rows:
            session_id = row["session_id"]
            if row["state"] == SESSION_CLOSING:
                self._drain_session(session_id, summary)
                continue
            reason = self._expiry_reason(row)
            if reason is None:
                continue
            with self.store.txn():
                self.store.execute(
                    "UPDATE sessions SET state=?, close_reason=?, status_version = status_version + 1 "
                    "WHERE session_id=?",
                    (SESSION_CLOSING, reason, session_id),
                )
                self.events.append(session_id, "session_closing", payload={"close_reason": reason})
            summary["sessions_closing"].append({"session_id": session_id, "reason": reason})

    def _expiry_reason(self, row: Any) -> str | None:
        if row["state"] == SESSION_CREATED:
            # A never-activated session is closed by the supervisor; it has no
            # wall clock to expire.
            created = epoch_from_iso(row["created_utc"])
            if created is not None and self.clock.epoch() - created > 3600.0:
                return "supervisor_recovery"
            return None
        deadline = epoch_from_iso(row["wall_deadline_utc"])
        if deadline is not None and self.clock.epoch() >= deadline:
            return "wall_clock_expired"
        last_seen = epoch_from_iso(row["last_seen_utc"])
        if last_seen is not None and self.clock.epoch() - last_seen > AGENT_LIVENESS_GRACE_SECONDS:
            return "agent_lost"
        return None

    def _drain_session(self, session_id: str, summary: dict[str, Any]) -> None:
        """Close the action entry point, drain activity, then close the session."""

        open_attempts = self.store.query_all(
            "SELECT attempt_id, state FROM attempts WHERE session_id=? AND state<>?",
            (session_id, ATTEMPT_CLOSED),
        )
        all_closed = True
        for attempt in open_attempts:
            attempt_id = attempt["attempt_id"]
            if attempt["state"] != ATTEMPT_FINALIZING:
                self.runtime.transition(attempt_id, ATTEMPT_FINALIZING)
            self._finalize_attempt(attempt_id, summary)
            row = self.store.query_one("SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,))
            if row is None or row["state"] != ATTEMPT_CLOSED:
                all_closed = False
        if not all_closed:
            return
        with self.store.txn():
            self.store.execute(
                "UPDATE sessions SET state=?, closed_utc=?, active_attempt_id=NULL, "
                "status_version = status_version + 1 WHERE session_id=?",
                (SESSION_CLOSED, self.clock.utc(), session_id),
            )
            self.events.append(session_id, "session_closed", payload={"drained": True})
        summary["sessions_closed"].append(session_id)

    # -- attempts ------------------------------------------------------------
    def _sweep_attempts(self, summary: dict[str, Any]) -> None:
        rows = self.store.query_all(
            "SELECT * FROM attempts WHERE state<>? ORDER BY attempt_no", (ATTEMPT_CLOSED,)
        )
        now_epoch = self.clock.epoch()
        now_monotonic = self.clock.monotonic()
        for row in rows:
            attempt_id = row["attempt_id"]
            state = row["state"]

            if state in (ATTEMPT_ACCEPTED, ATTEMPT_PREPARING):
                deadline = epoch_from_iso(row["prepare_deadline_utc"])
                if deadline is not None and now_epoch >= deadline:
                    with self.store.txn():
                        self.runtime.update(
                            attempt_id,
                            reason_code="DEVICE_UNAVAILABLE",
                            execution_status="setup_failed",
                        )
                        self.events.append(
                            session_id=row["session_id"],
                            event_type="attempt_prepare_timeout",
                            payload={"stage": "prepare"},
                            attempt_id=attempt_id,
                        )
                    self.runtime.transition(attempt_id, ATTEMPT_FINALIZING)

            elif state == ATTEMPT_RUNNING:
                confirmed = row["observation_deadline_monotonic"]
                if confirmed is None:
                    launch_deadline = epoch_from_iso(row["launch_deadline_utc"])
                    prepared_epoch = epoch_from_iso(row["created_utc"])
                    if launch_deadline is not None and now_epoch >= launch_deadline:
                        # No launch happened within the attempt's window at all.
                        self.runtime.set_stage(attempt_id, "launch", "failed")
                        self.runtime.set_stage(attempt_id, "observe", "skipped")
                        self.runtime.update(
                            attempt_id, reason_code="LAUNCH_TIMEOUT", execution_status="setup_failed"
                        )
                        with self.store.txn():
                            self.events.append(
                                row["session_id"], "attempt_launch_window_expired",
                                payload={"reason_code": "LAUNCH_TIMEOUT"}, attempt_id=attempt_id,
                            )
                        self.runtime.transition(attempt_id, ATTEMPT_FINALIZING)
                    elif prepared_epoch is not None and self.config.timeouts.get("attempt_total") is None:
                        # Without an attempt_total timeout the attempt waits for
                        # an Agent action; the session wall clock still bounds it.
                        pass
                elif not self._monotonic_deadline_is_usable(row):
                    # The persisted deadline was taken on a monotonic clock that
                    # has since restarted: the observation window was interrupted
                    # by a host reboot. Comparing the two clocks is meaningless,
                    # and the window must never be recorded as normally complete.
                    self._finalize_after_host_reboot(row, summary)
                else:
                    # Observation window: the supervisor finalizes exactly at the
                    # persisted deadline without waiting for any client poll.
                    if now_monotonic >= float(confirmed):
                        # The window closes exactly once. A supervisor restarted
                        # after a crash may find the flag already set (the update
                        # committed, the transition did not). It stays set: never
                        # downgrade a genuinely completed window to "incomplete",
                        # which would discard a full observation and could flip a
                        # valid verdict.
                        self.runtime.set_stage(attempt_id, "observe", "completed")
                        self.runtime.update(
                            attempt_id,
                            observation_window_complete=1,
                            execution_status="completed",
                        )
                        self.runtime.transition(attempt_id, ATTEMPT_FINALIZING)

            if self.runtime.load(attempt_id)["state"] == ATTEMPT_FINALIZING:
                self._finalize_attempt(attempt_id, summary)

    def _monotonic_deadline_is_usable(self, row: Any) -> bool:
        """Can this attempt's persisted monotonic deadline be compared safely?

        Only if it was produced by the same host boot as this process. Rows
        written before boot tracking existed (``monotonic_boot_anchor IS NULL``)
        are accepted when this process has no competing boot to compare against,
        so an upgrade does not strand an in-flight attempt.
        """

        anchor = row["monotonic_boot_anchor"] if "monotonic_boot_anchor" in row.keys() else None
        if anchor is None:
            return True
        # Re-confirm the live host boot first, so this is correct regardless of
        # what ran earlier in the sweep.
        self.store.refresh_host_boot()
        return not self.store.host_rebooted_since(float(anchor))

    def _finalize_after_host_reboot(self, row: Any, summary: dict[str, Any]) -> None:
        """Interrupted-observation path: preserve evidence, admit incompleteness.

        The 15 s window was cut short by a host reboot, so we must NOT claim a
        complete observation. Whatever was collected before the reboot is still
        sealed and preserved, the stage is marked explicitly incomplete, and the
        device is treated as unhealthy so the next attempt cold-boots instead of
        reusing possibly-dirty state.
        """

        attempt_id = row["attempt_id"]
        session_id = row["session_id"]
        self.runtime.set_stage(attempt_id, "observe", "unknown")
        self.runtime.update(
            attempt_id,
            observation_window_complete=0,
            reason_code="DEVICE_UNAVAILABLE",
            execution_status="outcome_unknown",
            device_health="unavailable",
        )
        with self.store.txn():
            self.events.append(
                session_id,
                "attempt_interrupted_by_host_reboot",
                payload={
                    "reason_code": "DEVICE_UNAVAILABLE",
                    "observation_window_complete": False,
                    "persisted_boot_anchor": row["monotonic_boot_anchor"],
                    "current_boot_anchor": self.store.boot_anchor(),
                },
                candidate_id=row["candidate_id"],
                attempt_id=attempt_id,
            )
        # The device state cannot be trusted across a host reboot: quarantine so
        # the next attempt performs a full cold boot rather than resuming.
        try:
            self.device.quarantine()
        except Exception:
            pass
        summary.setdefault("attempts_interrupted", []).append({"attempt_id": attempt_id})
        self.runtime.transition(attempt_id, ATTEMPT_FINALIZING)

    def _finalize_attempt(self, attempt_id: str, summary: dict[str, Any]) -> None:
        """Seal -> stop -> seal cleanup -> restore -> CLOSED, exactly once."""

        attempt = self.runtime.load(attempt_id)
        if attempt["state"] == ATTEMPT_CLOSED:
            return
        if attempt["state"] != ATTEMPT_FINALIZING:
            self.runtime.transition(attempt_id, ATTEMPT_FINALIZING)
            attempt = self.runtime.load(attempt_id)

        session_id = attempt["session_id"]
        candidate_id = attempt["candidate_id"]
        build = self.store.query_one("SELECT * FROM builds WHERE build_id=?", (attempt["build_id"],))
        package_name = build["package_name"] if build is not None else ""

        # (1) close the new-trigger entry point, then seal execution evidence
        #     BEFORE stopping the candidate package.
        if not attempt["evidence_sealed"]:
            try:
                self.finalizer.seal_execution_evidence(attempt)
            except Exception as exc:
                self.events.append(
                    session_id, "evidence_seal_failed",
                    payload={"phase": "execution", "detail_code": type(exc).__name__},
                    attempt_id=attempt_id,
                )
                self.runtime.update(
                    attempt_id,
                    reason_code=attempt["reason_code"] or "EVIDENCE_COLLECTION_INCOMPLETE",
                    execution_status=attempt["execution_status"] or "outcome_unknown",
                )
            attempt = self.runtime.load(attempt_id)

        # (2) stop the candidate package and verify the stop.
        if package_name:
            outcome = self.finalizer.stop_and_restore(attempt_id=attempt_id, package_name=package_name)
        else:
            outcome = {"stop_ok": False, "restore_ok": False, "health": "unknown", "reason_code": "CLEANUP_FAILED"}

        # (3) seal cleanup evidence into a SEPARATE object after the stop.
        if not attempt["cleanup_sealed"]:
            try:
                self.finalizer.seal_cleanup_evidence(self.runtime.load(attempt_id))
            except Exception as exc:
                self.events.append(
                    session_id, "evidence_seal_failed",
                    payload={"phase": "cleanup", "detail_code": type(exc).__name__},
                    attempt_id=attempt_id,
                )

        # (4) project the sealed evidence into Agent-visible diagnostics.
        self._project_diagnostics(attempt, package_name)

        # (5) stage/result reconciliation and close.
        attempt = self.runtime.load(attempt_id)
        stages = attempt["stages"]
        if stages.get("launch") == "pending":
            stages["launch"] = "skipped"
        if stages.get("observe") == "pending":
            stages["observe"] = "skipped"
        stages["cleanup"] = "completed" if outcome.get("restore_ok") else "failed"
        self.runtime.save_stages(attempt_id, stages)

        recovery_status = "succeeded" if outcome.get("restore_ok") else "failed"
        execution_status = attempt["execution_status"] or "outcome_unknown"
        if not outcome.get("stop_ok"):
            execution_status = "device_lost"
        reason_code = attempt["reason_code"] or outcome.get("reason_code")
        if not outcome.get("restore_ok"):
            reason_code = reason_code or "CLEANUP_FAILED"

        with self.store.txn():
            self.store.execute(
                """
                UPDATE attempts
                   SET state=?, execution_status=?, recovery_status=?, reason_code=?, device_health=?,
                       closed_utc=?, status_version = status_version + 1
                 WHERE attempt_id=?
                """,
                (
                    ATTEMPT_CLOSED,
                    execution_status,
                    recovery_status,
                    reason_code,
                    outcome.get("health", "unknown"),
                    self.clock.utc(),
                    attempt_id,
                ),
            )
            self.store.execute(
                "UPDATE sessions SET active_attempt_id=NULL, status_version = status_version + 1 "
                "WHERE session_id=? AND active_attempt_id=?",
                (session_id, attempt_id),
            )
            for handle in collector_task_handles(self.store, attempt_id):
                self.store.execute(
                    "UPDATE tasks SET state=?, exit_reason=?, updated_utc=? WHERE task_id=?",
                    ("EXITED", "completed", self.clock.utc(), handle["task_id"]),
                )
            self.events.append(
                session_id, "attempt_closed",
                payload={
                    "execution_status": execution_status,
                    "recovery_status": recovery_status,
                    "reason_code": reason_code,
                },
                candidate_id=candidate_id,
                attempt_id=attempt_id,
            )
        summary["attempts_finalized"].append(
            {
                "attempt_id": attempt_id,
                "execution_status": execution_status,
                "recovery_status": recovery_status,
                "reason_code": reason_code,
            }
        )

    # -- diagnostics projection ---------------------------------------------
    def _project_diagnostics(self, attempt: dict[str, Any], package_name: str) -> None:
        """Turn sealed evidence into whitelisted Agent-visible diagnostics.

        The raw evidence stream stays private; only the projector's output is
        persisted, and it is billed by frozen tree hash when read.
        """

        attempt_id = attempt["attempt_id"]
        session_id = attempt["session_id"]
        candidate_id = attempt["candidate_id"]
        existing = self.store.query_one(
            "SELECT COUNT(*) AS n FROM diagnostics WHERE attempt_id=? AND kind IN ('candidate_log','system_event')",
            (attempt_id,),
        )
        if existing is not None and int(existing["n"]) > 0:
            return

        candidate = self.store.query_one(
            "SELECT tree_hash FROM candidates WHERE candidate_id=?", (candidate_id,)
        )
        if candidate is None:
            return
        tree_hash = candidate["tree_hash"]

        uid_row = self.store.query_one(
            "SELECT payload_json FROM events WHERE attempt_id=? AND event_type='attempt_prepared' "
            "ORDER BY node_seq DESC LIMIT 1",
            (attempt_id,),
        )
        actual_uid = None
        if uid_row is not None:
            payload = self.store.loads(uid_row["payload_json"], {})
            actual_uid = payload.get("candidate_uid")
        if not isinstance(actual_uid, int):
            return

        records: list[dict[str, Any]] = []
        for row in self.store.query_all(
            "SELECT * FROM artifacts WHERE attempt_id=? AND state='sealed' "
            "AND kind IN ('execution_evidence','cleanup_evidence') ORDER BY created_utc, artifact_id",
            (attempt_id,),
        ):
            try:
                data = self.artifacts.read_object(row["artifact_id"])
            except Exception:
                continue
            for line in data.split(b"\n"):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(record, dict):
                    record.setdefault("attempt_id", attempt_id)
                    records.append(record)

        projection = self.projector.project_runtime_logs(
            candidate_id=candidate_id,
            attempt_id=attempt_id,
            package_name=package_name,
            actual_uid=actual_uid,
            raw_records=records,
            limit=int(self.config.feedback_limits["diagnostics_per_page"]) * int(
                self.config.feedback_limits["diagnostic_pages_per_candidate"]
            ),
        )
        with self.store.txn():
            for entry in projection.entries:
                self.store.execute(
                    """
                    INSERT INTO diagnostics(diagnostic_id, session_id, candidate_id, tree_hash, build_id,
                                            attempt_id, scope, kind, ordinal, payload_json, sanitized,
                                            incomplete, created_utc)
                    VALUES (?,?,?,?,NULL,?,'runtime',?,?,?,1,?,?)
                    """,
                    (
                        new_id("diagnostic"),
                        session_id,
                        candidate_id,
                        tree_hash,
                        attempt_id,
                        entry.kind,
                        entry.ordinal,
                        self.store.dumps(entry.payload),
                        1 if projection.diagnostics_incomplete else 0,
                        self.clock.utc(),
                    ),
                )

    # -- dangling reservations ----------------------------------------------
    def _sweep_dangling_reservations(self, summary: dict[str, Any]) -> None:
        """Settle reservations whose operation never completed.

        A crash after a side effect but before recording the outcome leaves a
        reservation behind. The design requires ``outcome_unknown`` here and
        forbids automatic replay of a side-effecting action.
        """

        rows = self.store.query_all(
            """
            SELECT o.* FROM operations o
            WHERE o.state IN ('PENDING','RUNNING')
              AND o.deadline_utc IS NOT NULL
            """
        )
        for row in rows:
            deadline = epoch_from_iso(row["deadline_utc"])
            if deadline is None or self.clock.epoch() < deadline:
                continue
            with self.store.txn():
                self.store.execute(
                    "UPDATE operations SET state=?, reason_code=?, updated_utc=? WHERE operation_id=?",
                    (OP_OUTCOME_UNKNOWN, "REQUEST_REJECTED", self.clock.utc(), row["operation_id"]),
                )
                self.events.append(
                    row["session_id"], "operation_outcome_unknown",
                    payload={"operation_id": row["operation_id"], "method": row["method"]},
                    attempt_id=row["attempt_id"],
                )
            summary["dangling_reservations"].append(row["operation_id"])

    # -- private Oracle evaluation ------------------------------------------
    def _sweep_evaluations(self, summary: dict[str, Any]) -> None:
        """Evaluate sealed attempts. Never blocks recovery on Oracle success."""

        rows = self.store.query_all(
            "SELECT * FROM attempts WHERE state=? AND evidence_sealed=1", (ATTEMPT_CLOSED,)
        )
        for row in rows:
            attempt_id = row["attempt_id"]
            existing = self.store.query_one(
                "SELECT evaluation_id FROM oracle_evaluations WHERE attempt_id=?", (attempt_id,)
            )
            if existing is not None:
                continue
            environment_valid = row["recovery_status"] != "failed"
            try:
                evidence = self.finalizer.sealed_evidence(attempt_id)
            except Exception:
                evidence = []
            try:
                verdict = self.oracle.evaluate(evidence, environment_valid=environment_valid)
            except Exception:
                from runner.adapters.oracle import NotIntegratedOracle

                verdict = NotIntegratedOracle().evaluate(evidence, environment_valid=environment_valid)
            with self.store.txn():
                self.store.execute(
                    """
                    INSERT INTO oracle_evaluations(evaluation_id, attempt_id, session_id, oracle_id,
                                                   oracle_version, criteria_version, verdict,
                                                   evaluation_status, observations_json, reason_private,
                                                   evidence_refs_json, created_utc, completed_utc)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        new_id("oracle"),
                        attempt_id,
                        row["session_id"],
                        verdict.oracle_id,
                        verdict.oracle_version,
                        verdict.criteria_version,
                        verdict.verdict,
                        verdict.evaluation_status,
                        self.store.dumps(verdict.observations),
                        verdict.reason_private[:500],
                        self.store.dumps(verdict.evidence_refs),
                        self.clock.utc(),
                        self.clock.utc(),
                    ),
                )
                # The private verdict is mirrored onto the attempt row for the
                # researcher; every Agent projection drops these columns.
                self.store.execute(
                    "UPDATE attempts SET oracle_verdict=?, evaluation_status=? WHERE attempt_id=?",
                    (verdict.verdict, verdict.evaluation_status, attempt_id),
                )
                self.events.append(
                    row["session_id"], "oracle_evaluated",
                    payload={"evaluation_status": verdict.evaluation_status, "oracle_id": verdict.oracle_id},
                    attempt_id=attempt_id,
                )
            summary["evaluations"].append(
                {"attempt_id": attempt_id, "evaluation_status": verdict.evaluation_status}
            )


def run_forever(
    supervisor: Supervisor,
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    stop_after: float | None = None,
) -> None:
    """Supervisor loop. ``stop_after`` bounds the run for tests/CI."""

    supervisor.register()
    started = time.monotonic()
    try:
        while True:
            supervisor.sweep()
            if stop_after is not None and time.monotonic() - started >= stop_after:
                return
            time.sleep(interval_seconds)
    except KeyboardInterrupt:  # pragma: no cover - interactive use
        return
