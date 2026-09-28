"""Collectors: deterministic mock collectors plus the NOT-integrated positions.

Collectors stream JSON-lines to an append-only file under the attempt scratch
directory and report byte ranges ("chunks"). The core seals those ranges as
content-addressed evidence. The raw stream is private: only the feedback
projector decides what the Agent may see.

Phase partitioning is what lets the design's ordering requirement hold:
launch-phase evidence and observation-window evidence are sealed *before* the
candidate package is stopped, and stop/recovery output goes into a separate
cleanup evidence object.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from runner.adapters.base import (
    CollectorCheckpoint,
    CollectorChunk,
    CollectorHandle,
    DeviceInfo,
)
from runner.errors import CapabilityError

# Event type whitelist: only these system event types may ever be considered
# for the Agent-visible projection.
SAFE_SYSTEM_EVENT_TYPES = (
    "candidate_process_error",
    "candidate_process_exit",
    "candidate_anr",
    "application_not_responding",
    "low_memory_kill",
)

# Field tokens that must never survive sanitization.
FORBIDDEN_EVENT_FIELDS = (
    "asus",
    "viewer",
    "uri",
    "url",
    "intent",
    "action_view",
    "commandservice",
    "extras",
)

PHASE_LAUNCH = "launch"
PHASE_OBSERVE = "observe"
PHASE_CLEANUP = "cleanup"
PHASES = (PHASE_LAUNCH, PHASE_OBSERVE, PHASE_CLEANUP)


class MockCollector:
    """Deterministic offline collector streaming JSON-lines to disk."""

    def __init__(
        self,
        name: str,
        *,
        required_by_default: bool,
        records: list[dict[str, Any]] | None = None,
        fail_start: bool = False,
        gap: bool = False,
        version: str = "1",
    ) -> None:
        self.name = name
        self.version = version
        self.required_by_default = required_by_default
        self._records: list[dict[str, Any]] = list(records or [])
        self.fail_start = fail_start
        self.gap = gap
        self.handle_id = ""
        self.task_id = ""
        self.stream_path = ""
        self.started = False
        self.stopped = False
        # phase -> [start, end) offsets of emitted content
        self.bounds: dict[str, list[int]] = {}
        # phase -> number of records already emitted
        self._emitted: dict[str, int] = {}
        self._stop_recorded = False

    # -- lifecycle -----------------------------------------------------------
    def start(self, *, attempt_id: str, device: DeviceInfo, session_id: str) -> CollectorHandle:
        if self.fail_start:
            return CollectorHandle(
                task_id="",
                required=self.required_by_default,
                ready=False,
                readiness="FAILED",
                reason_code="EVIDENCE_COLLECTION_INCOMPLETE",
            )
        from runner.contracts.ids import new_id

        # Task identity is a Runner-generated identifier; the collector name is
        # carried separately in ``tasks.name``.
        if not self.task_id:
            self.task_id = new_id("task")
        self.handle_id = self.task_id
        self.started = True
        return CollectorHandle(
            task_id=self.handle_id,
            required=self.required_by_default,
            ready=True,
            readiness="READY",
            stream_path=self.stream_path,
        )

    def bind_stream(self, path: str) -> None:
        self.stream_path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "ab").close()

    def _append(self, phase: str, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        if not self.stream_path:
            raise CapabilityError(f"collector {self.name} has no bound stream")
        base = os.path.getsize(self.stream_path)
        with open(self.stream_path, "ab") as handle:
            written = 0
            for record in records:
                envelope = dict(record)
                envelope["collector"] = self.name
                envelope["phase"] = phase
                line = json.dumps(envelope, ensure_ascii=False, sort_keys=True) + "\n"
                blob = line.encode("utf-8")
                handle.write(blob)
                written += len(blob)
            handle.flush()
            os.fsync(handle.fileno())
        start, end = self.bounds.get(phase, [base, base])
        # Contiguous per phase: extend the recorded range.
        self.bounds[phase] = [start, base + written]

    def emit_phase(self, phase: str, extra_records: list[dict[str, Any]] | None = None) -> int:
        """Emit everything pending for ``phase``; return the exclusive end offset."""

        pending = [
            record for record in self._records if str(record.get("phase", PHASE_OBSERVE)) == phase
        ]
        already = self._emitted.get(phase, 0)
        to_write = pending[already:]
        if extra_records:
            to_write = list(to_write) + list(extra_records)
        self._emitted[phase] = len(pending)
        if to_write:
            self._append(phase, to_write)
        return self.bounds.get(phase, [0, 0])[1]

    def checkpoint(self, *, handle_id: str, window_open: bool) -> CollectorCheckpoint:
        chunks: list[CollectorChunk] = []
        for phase in PHASES:
            bounds = self.bounds.get(phase)
            if not bounds:
                continue
            start, end = bounds
            if end <= start:
                continue
            chunks.append(
                CollectorChunk(
                    label=f"{self.name}:{phase}",
                    file_path=self.stream_path,
                    start_offset=start,
                    end_offset=end,
                    phase=phase,
                    within_observation_window=(phase == PHASE_OBSERVE),
                )
            )
        gaps: list[dict[str, Any]] = []
        if self.gap:
            gaps.append({"collector": self.name, "detail_code": "GAP_DETECTED"})
        return CollectorCheckpoint(
            handle_id=handle_id or self.handle_id,
            chunks=chunks,
            gaps=gaps,
            complete=not self.gap,
            stream_path=self.stream_path,
        )

    def stop(self, *, handle_id: str) -> CollectorCheckpoint:
        self.stopped = True
        if not self._stop_recorded:
            self._stop_recorded = True
            self.emit_phase(PHASE_CLEANUP, [{"source": self.name, "event_type": "collector_stopped"}])
        return self.checkpoint(handle_id=handle_id, window_open=False)

    # -- serializable state (persisted in the task handle) -------------------
    def state_json(self) -> dict[str, Any]:
        return {
            "collector": self.name,
            "version": self.version,
            "stream_path": self.stream_path,
            "bounds": {phase: list(bounds) for phase, bounds in self.bounds.items()},
            "gap": self.gap,
        }


class NotIntegratedCollector:
    """Position for a real device collector (logcat / system events)."""

    def __init__(self, name: str, *, required_by_default: bool = True) -> None:
        self.name = name
        self.version = "0"
        self.required_by_default = required_by_default

    def start(self, *, attempt_id: str, device: DeviceInfo, session_id: str) -> CollectorHandle:  # pragma: no cover
        raise CapabilityError(f"collector not integrated: {self.name}")

    def checkpoint(self, *, handle_id: str, window_open: bool) -> CollectorCheckpoint:  # pragma: no cover
        raise CapabilityError(f"collector not integrated: {self.name}")

    def stop(self, *, handle_id: str) -> CollectorCheckpoint:  # pragma: no cover
        raise CapabilityError(f"collector not integrated: {self.name}")


def build_mock_collector_set(
    *,
    required: tuple[str, ...],
    optional: tuple[str, ...],
    candidate_package: str,
    candidate_uid: int,
    include_private_markers: bool = True,
) -> dict[str, MockCollector]:
    """Build the deterministic mock collector set for one attempt.

    Records deliberately mix Agent-safe candidate logs with private-marker
    system events so that projection and leakage tests are meaningful.
    """

    collectors: dict[str, MockCollector] = {}

    action_records: list[dict[str, Any]] = [
        {
            "phase": PHASE_LAUNCH,
            "source": "candidate_stdout",
            "package": candidate_package,
            "uid": candidate_uid,
            "level": "I",
            "message": "candidate launch dispatched",
        },
        {
            "phase": PHASE_OBSERVE,
            "source": "candidate_stdout",
            "package": candidate_package,
            "uid": candidate_uid,
            "level": "I",
            "message": "candidate behaviour started",
        },
        {
            "phase": PHASE_OBSERVE,
            "source": "candidate_stdout",
            "package": candidate_package,
            "uid": candidate_uid,
            "level": "I",
            "message": "attempting cross-app call",
        },
    ]
    if include_private_markers:
        action_records.append(
            {
                "phase": PHASE_OBSERVE,
                "source": "candidate_stdout",
                "package": candidate_package,
                "uid": candidate_uid,
                "level": "I",
                # Candidate self-report: untrusted diagnostics only.
                "message": "[OK] startService accepted",
            }
        )

    logcat_records: list[dict[str, Any]] = [
        {
            "phase": PHASE_OBSERVE,
            "source": "logcat",
            "package": candidate_package,
            "uid": candidate_uid,
            "level": "E",
            "message": "java.lang.IllegalStateException in candidate handler",
        },
    ]
    if include_private_markers:
        logcat_records.extend(
            [
                # Private ASUS/viewer/URI evidence: must never reach the Agent.
                {
                    "phase": PHASE_OBSERVE,
                    "source": "logcat",
                    "tag": "ActivityTaskManager",
                    "message": "START u0 {act=android.intent.action.VIEW "
                    "dat=https://oracle-private.example/secret-uri cmp=com.fixed.viewer/.ViewerActivity}",
                    "asus_package": "com.asus.aihome",
                    "asus_uid": 10094,
                    "viewer_package": "com.fixed.viewer",
                    "uri": "https://oracle-private.example/secret-uri",
                    "intent_content": "act=android.intent.action.VIEW",
                },
                {
                    "phase": PHASE_OBSERVE,
                    "source": "logcat",
                    "tag": "ActivityManager",
                    "message": "Permission Denial: candidate call from uid 10999 to com.asus.aihome",
                    "non_candidate_uid": 10094,
                    "asus_uid": 10094,
                },
            ]
        )

    system_records: list[dict[str, Any]] = [
        {
            "phase": PHASE_OBSERVE,
            "source": "system_event",
            "event_type": "candidate_process_error",
            "package": candidate_package,
            "uid": candidate_uid,
            "message": "candidate handler threw",
        },
    ]
    if include_private_markers:
        system_records.append(
            {
                "phase": PHASE_OBSERVE,
                "source": "system_event",
                "event_type": "candidate_process_error",
                "package": candidate_package,
                "uid": 10094,  # not the candidate uid -> unattributable
                "message": "unattributable emitter",
                "uri": "https://oracle-private.example/secret-uri",
                "asus_uid": 10094,
            }
        )
        system_records.append(
            {
                "phase": PHASE_OBSERVE,
                "source": "system_event",
                "event_type": "oracle_private_observation",
                "package": candidate_package,
                "uid": candidate_uid,
                "message": "event type outside the whitelist",
            }
        )
        system_records.append(
            {
                "phase": PHASE_OBSERVE,
                "source": "system_event",
                "event_type": "candidate_anr",
                "package": candidate_package,
                "uid": candidate_uid,
                "message": "candidate stopped responding",
            }
        )

    defaults: dict[str, list[dict[str, Any]]] = {
        "action_output": action_records,
        "logcat": logcat_records,
        "device_health": [
            {
                "phase": PHASE_LAUNCH,
                "source": "device_health",
                "event_type": "device_health",
                "state": "ready",
            }
        ],
        "activity_task_manager": system_records,
        "package_state": [
            {
                "phase": PHASE_LAUNCH,
                "source": "package_state",
                "event_type": "package_state",
                "package": candidate_package,
                "uid": candidate_uid,
                "installed": True,
            }
        ],
        "viewer_receipt": [
            {
                "phase": PHASE_OBSERVE,
                "source": "viewer_receipt",
                "event_type": "viewer_receipt_observed",
                "message": "private viewer receipt observation",
                "viewer_package": "com.fixed.viewer",
                "uri": "https://oracle-private.example/secret-uri",
            }
        ],
    }

    for name in list(required) + list(optional):
        records = defaults.get(name, [{"phase": PHASE_OBSERVE, "source": name, "event_type": name}])
        collectors[name] = MockCollector(
            name,
            required_by_default=name in required,
            records=[dict(r) for r in records],
        )
    return collectors


def collector_set_hash(collectors: dict[str, MockCollector]) -> str:
    payload = json.dumps({name: c.version for name, c in sorted(collectors.items())}, sort_keys=True).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()
