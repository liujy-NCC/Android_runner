"""Attempt execution: prepare, launch, observe, seal, stop, restore, close.

Sealing order is the contract that matters most here (DESIGN.md §3, §4.6):

    observation deadline
      -> close the new-trigger entry point
      -> drain collectors to a checkpoint
      -> seal execution evidence (launch + observation phases)
      -> publish the execution evidence group
      -> stop the candidate package
      -> seal cleanup evidence separately
      -> restore and health-check
      -> CLOSED

Every step reads its inputs from SQLite and from collector stream files on
disk, never from in-process state, so a *separate supervisor process* can
perform finalization after the API process died. Events arriving after the
observation deadline are never counted into that window, and an attempt whose
evidence could not be sealed is never handed to the Oracle.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Iterable

from runner.adapters.base import CollectorChunk, DeviceInfo, SealedEvidence
from runner.config import SessionConfig
from runner.contracts.clock import Clock
from runner.contracts.states import (
    ATTEMPT_STAGES,
    ATTEMPT_TRANSITIONS,
    STAGE_STATUSES,
    assert_transition,
)
from runner.errors import StateError
from runner.store.artifacts import ArtifactStore
from runner.store.db import Store
from runner.store.events import EventLog

PHASE_LAUNCH = "launch"
PHASE_OBSERVE = "observe"
PHASE_CLEANUP = "cleanup"


def empty_stages() -> dict[str, str]:
    return {name: "pending" for name in ATTEMPT_STAGES}


class AttemptRuntime:
    """Mutable per-attempt state, always loaded from SQLite."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def find(self, attempt_id: str) -> dict[str, Any] | None:
        """Load without raising: a missing or foreign id is simply absent."""

        row = self.store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        if row is None:
            return None
        data = dict(row)
        data["stages"] = self.store.loads(row["stages_json"], empty_stages())
        return data

    def load(self, attempt_id: str) -> dict[str, Any]:
        data = self.find(attempt_id)
        if data is None:
            raise StateError(f"unknown attempt: {attempt_id}")
        return data

    def save_stages(self, attempt_id: str, stages: dict[str, str], *, bump_version: bool = True) -> int:
        for status in stages.values():
            if status not in STAGE_STATUSES:
                raise StateError(f"invalid stage status: {status}")
        self.store.execute(
            "UPDATE attempts SET stages_json=?, status_version = status_version + ? WHERE attempt_id=?",
            (self.store.dumps(stages), 1 if bump_version else 0, attempt_id),
        )
        return self.status_version(attempt_id)

    def set_stage(self, attempt_id: str, stage: str, status: str) -> int:
        stages = self.load(attempt_id)["stages"]
        stages[stage] = status
        return self.save_stages(attempt_id, stages)

    def status_version(self, attempt_id: str) -> int:
        row = self.store.query_one("SELECT status_version FROM attempts WHERE attempt_id=?", (attempt_id,))
        return int(row["status_version"]) if row else 0

    def transition(self, attempt_id: str, target: str, **columns: Any) -> None:
        row = self.store.query_one("SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,))
        if row is None:
            raise StateError(f"unknown attempt: {attempt_id}")
        if row["state"] == target:
            if columns:
                self.update(attempt_id, **columns)
            return
        assert_transition(ATTEMPT_TRANSITIONS, row["state"], target, "attempt")
        self.update(attempt_id, state=target, **columns)

    def update(self, attempt_id: str, *, bump_version: bool = True, **columns: Any) -> None:
        sets: list[str] = []
        params: list[Any] = []
        for key, value in columns.items():
            sets.append(f"{key}=?")
            params.append(value)
        if bump_version:
            sets.append("status_version = status_version + 1")
        params.append(attempt_id)
        self.store.execute(f"UPDATE attempts SET {', '.join(sets)} WHERE attempt_id=?", params)


# ---------------------------------------------------------------------------
# Persisted collector streams
# ---------------------------------------------------------------------------
def collector_task_handles(store: Store, attempt_id: str) -> list[dict[str, Any]]:
    """Collector task rows, with their persisted stream bounds."""

    rows = store.query_all(
        "SELECT * FROM tasks WHERE attempt_id=? AND role='collector' ORDER BY created_utc, task_id",
        (attempt_id,),
    )
    handles: list[dict[str, Any]] = []
    for row in rows:
        handle = store.loads(row["handle"], {}) or {}
        handles.append(
            {
                "task_id": row["task_id"],
                "name": row["name"],
                "required": bool(row["required"]),
                "readiness": row["readiness"],
                "stream_path": handle.get("stream_path", ""),
                "bounds": {k: list(v) for k, v in (handle.get("bounds") or {}).items()},
                "gap": bool(handle.get("gap")),
            }
        )
    return handles


def append_cleanup_record(stream_path: str, collector_name: str) -> list[int] | None:
    """Append one cleanup line to a collector stream; return its byte range.

    Used at finalization after the observation window is sealed: stop/recovery
    output must land in a *different* evidence object and must never extend the
    already-sealed execution window.
    """

    if not stream_path or not os.path.exists(stream_path):
        return None
    record = {
        "collector": collector_name,
        "phase": PHASE_CLEANUP,
        "source": collector_name,
        "event_type": "collector_stopped",
    }
    blob = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    start = os.path.getsize(stream_path)
    with open(stream_path, "ab") as handle:
        handle.write(blob)
        handle.flush()
        os.fsync(handle.fileno())
    return [start, start + len(blob)]


def chunks_for_phase(
    handles: Iterable[dict[str, Any]], phase: str
) -> tuple[list[CollectorChunk], list[dict[str, Any]], bool]:
    """Build chunk references for one phase from persisted bounds."""

    chunks: list[CollectorChunk] = []
    gaps: list[dict[str, Any]] = []
    complete = True
    for handle in handles:
        bounds = handle["bounds"].get(phase)
        if bounds and bounds[1] > bounds[0]:
            chunks.append(
                CollectorChunk(
                    label=f"{handle['name']}:{phase}",
                    file_path=handle["stream_path"],
                    start_offset=int(bounds[0]),
                    end_offset=int(bounds[1]),
                    phase=phase,
                    within_observation_window=(phase == PHASE_OBSERVE),
                )
            )
        if handle["gap"]:
            gaps.append({"collector": handle["name"], "detail_code": "GAP_DETECTED"})
            if handle["required"]:
                complete = False
    return chunks, gaps, complete


class AttemptFinalizer:
    """Seals, stops, restores and closes an attempt exactly once.

    Constructed from persisted state only: safe to run from a restarted
    supervisor process with no shared memory from the request handler.
    """

    def __init__(
        self,
        *,
        store: Store,
        artifacts: ArtifactStore,
        events: EventLog,
        clock: Clock,
        runtime: AttemptRuntime,
        config: SessionConfig,
        device: Any,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.events = events
        self.clock = clock
        self.runtime = runtime
        self.config = config
        self.device = device

    # -- evidence ------------------------------------------------------------
    def _seal_chunks(
        self,
        *,
        session_id: str,
        attempt_id: str,
        candidate_id: str,
        kind: str,
        chunks: list[CollectorChunk],
        gaps: list[dict[str, Any]],
        phase_label: str,
    ) -> list[str]:
        artifact_ids: list[str] = []
        with self.store.txn():
            for chunk in chunks:
                if chunk.size <= 0 or not os.path.exists(chunk.file_path):
                    continue
                with open(chunk.file_path, "rb") as handle:
                    handle.seek(chunk.start_offset)
                    data = handle.read(chunk.size)
                if not data:
                    continue
                artifact_ids.append(
                    self.artifacts.put_in_txn(
                        session_id=session_id,
                        kind=kind,
                        media_type="application/x-ndjson",
                        data=data,
                        source="collector",
                        provenance="device_observed",
                        attempt_id=attempt_id,
                        candidate_id=candidate_id,
                    )
                )
            if gaps:
                gap_blob = json.dumps(
                    {"phase": phase_label, "gaps": gaps}, ensure_ascii=False, sort_keys=True
                ).encode("utf-8")
                artifact_ids.append(
                    self.artifacts.put_in_txn(
                        session_id=session_id,
                        kind="collection_gap",
                        media_type="application/json",
                        data=gap_blob,
                        source="collector",
                        provenance="runner_generated",
                        attempt_id=attempt_id,
                        candidate_id=candidate_id,
                    )
                )
        return artifact_ids

    def seal_execution_evidence(self, attempt: dict[str, Any]) -> str:
        """Seal launch + observation evidence. Must run BEFORE stopping."""

        session_id = attempt["session_id"]
        attempt_id = attempt["attempt_id"]
        candidate_id = attempt["candidate_id"]
        handles = collector_task_handles(self.store, attempt_id)
        all_ids: list[str] = []
        for phase in (PHASE_LAUNCH, PHASE_OBSERVE):
            chunks, gaps, _complete = chunks_for_phase(handles, phase)
            all_ids.extend(
                self._seal_chunks(
                    session_id=session_id,
                    attempt_id=attempt_id,
                    candidate_id=candidate_id,
                    kind="execution_evidence",
                    chunks=chunks,
                    gaps=gaps,
                    phase_label=phase,
                )
            )
        with self.store.txn():
            seal_id = self.artifacts.open_seal_group(
                session_id=session_id, kind="execution_evidence", attempt_id=attempt_id
            )
            self.artifacts.seal_group(
                seal_id, member_artifact_ids=all_ids, session_id=session_id, attempt_id=attempt_id
            )
        self.runtime.update(attempt_id, evidence_sealed=1)
        return seal_id

    def seal_cleanup_evidence(self, attempt: dict[str, Any]) -> str:
        """Seal stop/restore output into a SEPARATE evidence group."""

        session_id = attempt["session_id"]
        attempt_id = attempt["attempt_id"]
        candidate_id = attempt["candidate_id"]
        handles = collector_task_handles(self.store, attempt_id)
        chunks: list[CollectorChunk] = []
        gaps: list[dict[str, Any]] = []
        for handle in handles:
            bounds = append_cleanup_record(handle["stream_path"], handle["name"])
            if bounds is None:
                continue
            chunks.append(
                CollectorChunk(
                    label=f"{handle['name']}:{PHASE_CLEANUP}",
                    file_path=handle["stream_path"],
                    start_offset=bounds[0],
                    end_offset=bounds[1],
                    phase=PHASE_CLEANUP,
                    within_observation_window=False,
                )
            )
            if handle["gap"]:
                gaps.append({"collector": handle["name"], "detail_code": "GAP_DETECTED"})
        ids = self._seal_chunks(
            session_id=session_id,
            attempt_id=attempt_id,
            candidate_id=candidate_id,
            kind="cleanup_evidence",
            chunks=chunks,
            gaps=gaps,
            phase_label=PHASE_CLEANUP,
        )
        with self.store.txn():
            seal_id = self.artifacts.open_seal_group(
                session_id=session_id, kind="cleanup_evidence", attempt_id=attempt_id
            )
            self.artifacts.seal_group(
                seal_id, member_artifact_ids=ids, session_id=session_id, attempt_id=attempt_id
            )
        self.runtime.update(attempt_id, cleanup_sealed=1)
        return seal_id

    def sealed_evidence(self, attempt_id: str) -> list[SealedEvidence]:
        """Load sealed evidence for the Oracle. Unsealed objects are refused."""

        rows = self.store.query_all(
            "SELECT artifact_id, kind, content_sha256 FROM artifacts "
            "WHERE attempt_id=? AND state='sealed' AND kind IN "
            "('execution_evidence','cleanup_evidence','collection_gap') "
            "ORDER BY created_utc, artifact_id",
            (attempt_id,),
        )
        result: list[SealedEvidence] = []
        for row in rows:
            data = self.artifacts.read_object(row["artifact_id"])
            result.append(
                SealedEvidence(
                    artifact_id=row["artifact_id"],
                    kind=row["kind"],
                    sha256=row["content_sha256"],
                    data=data,
                    attempt_id=attempt_id,
                )
            )
        return result

    # -- stop / restore ------------------------------------------------------
    def stop_and_restore(self, *, attempt_id: str, package_name: str) -> dict[str, Any]:
        """Stop the candidate package, verify the stop, then restore + health."""

        outcome: dict[str, Any] = {
            "stop_ok": False,
            "restore_ok": False,
            "health": "unknown",
            "reason_code": None,
        }
        try:
            outcome["stop_ok"] = bool(self.device.stop_package("target", package_name))
        except Exception:
            outcome["stop_ok"] = False
        if not outcome["stop_ok"]:
            # Stopping the host-side client is not proof the device-side task
            # ended: without a verified stop we quarantine the resource.
            outcome["reason_code"] = "CLEANUP_FAILED"
            outcome["health"] = "unavailable"
            self._quarantine()
            self.runtime.update(attempt_id, device_health="unavailable")
            return outcome

        try:
            info: DeviceInfo = self.device.restore("target")
            outcome["health"] = info.health
            outcome["restore_ok"] = info.health == "ready"
        except Exception:
            outcome["restore_ok"] = False
            outcome["health"] = "unavailable"
        if not outcome["restore_ok"]:
            outcome["reason_code"] = outcome["reason_code"] or "CLEANUP_FAILED"
            self._quarantine()
        self.runtime.update(attempt_id, device_health=outcome["health"])
        return outcome

    def _quarantine(self) -> None:
        try:
            self.device.quarantine()
        except Exception:
            pass
