"""Append-only event ledger.

Every event carries the minimum field set required by DESIGN.md §4.4 so that
events can be correlated across actions, tasks and resources without relying on
wall-clock ordering.
"""

from __future__ import annotations

from typing import Any

from runner.contracts.ids import new_id
from runner.store.db import Store

EVENT_VERSION = 1

NODE = "runner-core"


class EventLog:
    def __init__(self, store: Store, *, clock: Any) -> None:
        self.store = store
        self.clock = clock

    def append(
        self,
        session_id: str,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
        candidate_id: str | None = None,
        build_id: str | None = None,
        attempt_id: str | None = None,
        action_id: str | None = None,
        task_id: str | None = None,
        resource_id: str | None = None,
        node: str = NODE,
    ) -> str:
        """Append one event inside the caller's transaction."""

        node_boot_id = self.clock.boot_id()
        seq = self.store.node_seq_next(node_boot_id)
        event_id = new_id("event")
        self.store.insert_event(
            {
                "event_id": event_id,
                "session_id": session_id,
                "event_type": event_type,
                "node": node,
                "node_boot_id": node_boot_id,
                "node_seq": seq,
                "utc": self.clock.utc(),
                "monotonic": self.clock.monotonic(),
                "event_version": EVENT_VERSION,
                "candidate_id": candidate_id,
                "build_id": build_id,
                "attempt_id": attempt_id,
                "action_id": action_id,
                "task_id": task_id,
                "resource_id": resource_id,
                "payload_json": self.store.dumps(payload or {}),
            }
        )
        return event_id

    # -- private/read-side helpers (never exposed to the Agent projection) ---
    def count_for_attempt(self, attempt_id: str, event_type: str | None = None) -> int:
        if event_type is None:
            row = self.store.query_one(
                "SELECT COUNT(*) AS n FROM events WHERE attempt_id=?", (attempt_id,)
            )
        else:
            row = self.store.query_one(
                "SELECT COUNT(*) AS n FROM events WHERE attempt_id=? AND event_type=?",
                (attempt_id, event_type),
            )
        return int(row["n"]) if row else 0
