"""Session lifecycle: creation, activation, credentials and lease ownership.

Session creation/activation is a researcher/controller administrative action and
is intentionally NOT exposed on the HTTP API.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any

from runner.config import SessionConfig
from runner.contracts.clock import Clock
from runner.contracts.ids import new_id
from runner.contracts.states import (
    SESSION_ACTIVE,
    SESSION_CLOSED,
    SESSION_CLOSING,
    SESSION_CREATED,
    SESSION_TRANSITIONS,
    assert_transition,
)
from runner.errors import ProtocolError
from runner.store.db import Store
from runner.store.events import EventLog

CREDENTIAL_BYTES = 32


@dataclass
class SessionRecord:
    session_id: str
    state: str
    config: dict[str, Any]
    config_sha256: str
    allowed_methods: frozenset[str]
    wall_deadline_utc: str | None
    active_attempt_id: str | None
    finalized_candidate_id: str | None
    node_boot_id: str | None
    close_reason: str | None
    status_version: int
    created_utc: str
    credential_expires_utc: str | None


class SessionService:
    def __init__(self, store: Store, events: EventLog, clock: Clock) -> None:
        self.store = store
        self.events = events
        self.clock = clock

    # -- creation / activation ----------------------------------------------
    def create(
        self,
        config: SessionConfig,
        *,
        allowed_methods: tuple[str, ...] | None = None,
        credential_ttl_seconds: float | None = None,
    ) -> tuple[str, str]:
        """Create a session in CREATED state. Returns ``(session_id, token)``.

        The plaintext token is returned exactly once; only a salted hash is
        persisted. The token binds session, permitted methods, object ownership
        and an expiry — the request body's ``session_id`` is only a consistency
        check.
        """

        config.validate()
        session_id = new_id("session")
        token = _new_token()
        salt = os.urandom(16).hex()
        now = self.clock.utc()
        methods = frozenset(allowed_methods or _default_methods())
        expires = None
        if credential_ttl_seconds is not None:
            expires = _iso(self.clock.epoch() + float(credential_ttl_seconds))

        with self.store.txn():
            self.store.execute(
                """
                INSERT INTO sessions(session_id, state, sample_id, sample_version, config_sha256,
                                     feedback_profile, credential_salt, credential_hash,
                                     credential_expires_utc, allowed_methods_json, resource_roles_json,
                                     status_version, created_utc, node_boot_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?)
                """,
                (
                    session_id,
                    SESSION_CREATED,
                    config.sample_id,
                    config.sample_version,
                    config.sha256(),
                    config.feedback_profile,
                    salt,
                    _hash_token(salt, token),
                    expires,
                    self.store.dumps(sorted(methods)),
                    self.store.dumps([r.to_json() for r in config.resource_roles]),
                    now,
                    self.clock.boot_id(),
                ),
            )
            self._allocate_counters(session_id, config)
            self.events.append(
                session_id,
                "session_created",
                payload={"sample_id": config.sample_id, "config_sha256": config.sha256()},
            )
        return session_id, token

    def activate(self, session_id: str, *, config: SessionConfig) -> None:
        """Start the non-pausable wall clock.

        ``session_wall_clock_starts_at = session_activation``: model waiting,
        building, running and feedback reading all consume the same budget.
        """

        wall = config.budget.get("session_wall_seconds")
        with self.store.txn():
            row = self._row(session_id)
            assert_transition(SESSION_TRANSITIONS, row["state"], SESSION_ACTIVE, "session")
            now = self.clock.utc()
            deadline = None if wall is None else _iso(self.clock.epoch() + float(wall))
            self.store.execute(
                """
                UPDATE sessions SET state=?, activated_utc=?, wall_deadline_utc=?,
                                    status_version = status_version + 1
                 WHERE session_id=?
                """,
                (SESSION_ACTIVE, now, deadline, session_id),
            )
            for role in config.resource_roles:
                self.store.execute(
                    """
                    INSERT INTO device_leases(resource_role, session_id, attempt_id, device_id,
                                              generation, health, acquired_utc, released_utc)
                    VALUES (?,?,NULL,NULL,1,'unknown',NULL,NULL)
                    ON CONFLICT(resource_role) DO NOTHING
                    """,
                    (role.role, session_id),
                )
            self.events.append(
                session_id,
                "session_activated",
                payload={"wall_deadline_utc": deadline, "wall_seconds": wall},
            )

    # -- authentication ------------------------------------------------------
    def authenticate(self, session_id: str, token: str) -> SessionRecord:
        row = self._row(session_id)
        expected = row["credential_hash"]
        salt = row["credential_salt"]
        if not _constant_time_eq(expected, _hash_token(salt, token)):
            raise ProtocolError("unauthenticated")
        expires = row["credential_expires_utc"]
        if expires is not None:
            from runner.contracts.clock import epoch_from_iso

            at = epoch_from_iso(expires)
            if at is not None and self.clock.epoch() > at:
                raise ProtocolError("unauthenticated", detail="credential expired")
        allowed = frozenset(self.store.loads(row["allowed_methods_json"], []))
        return SessionRecord(
            session_id=row["session_id"],
            state=row["state"],
            config={"config_sha256": row["config_sha256"], "sample_id": row["sample_id"]},
            config_sha256=row["config_sha256"],
            allowed_methods=allowed,
            wall_deadline_utc=row["wall_deadline_utc"],
            active_attempt_id=row["active_attempt_id"],
            finalized_candidate_id=row["finalized_candidate_id"],
            node_boot_id=row["node_boot_id"],
            close_reason=row["close_reason"],
            status_version=int(row["status_version"]),
            created_utc=row["created_utc"],
            credential_expires_utc=row["credential_expires_utc"],
        )

    def authorize(self, record: SessionRecord, method: str) -> None:
        if method not in record.allowed_methods:
            raise ProtocolError("permission_denied", detail=f"method not in credential scope: {method}")

    # -- state ---------------------------------------------------------------
    def touch(self, session_id: str) -> None:
        self.store.execute(
            "UPDATE sessions SET last_seen_utc=? WHERE session_id=?", (self.clock.utc(), session_id)
        )

    def require_open(self, session_id: str) -> None:
        row = self._row(session_id)
        if row["state"] in (SESSION_CLOSING, SESSION_CLOSED):
            raise ProtocolError("permission_denied", detail="session is closing or closed")

    def set_closing(self, session_id: str, reason: str) -> bool:
        """Enter CLOSING. Returns True when the transition was performed."""

        row = self._row(session_id)
        if row["state"] in (SESSION_CLOSING, SESSION_CLOSED):
            return False
        assert_transition(SESSION_TRANSITIONS, row["state"], SESSION_CLOSING, "session")
        with self.store.txn():
            self.store.execute(
                """
                UPDATE sessions SET state=?, close_reason=?, status_version = status_version + 1
                 WHERE session_id=?
                """,
                (SESSION_CLOSING, reason, session_id),
            )
            self.events.append(session_id, "session_closing", payload={"close_reason": reason})
        return True

    def set_closed(self, session_id: str) -> None:
        row = self._row(session_id)
        if row["state"] == SESSION_CLOSED:
            return
        with self.store.txn():
            self.store.execute(
                """
                UPDATE sessions SET state=?, closed_utc=?, status_version = status_version + 1
                 WHERE session_id=?
                """,
                (SESSION_CLOSED, self.clock.utc(), session_id),
            )
            self.events.append(session_id, "session_closed", payload={})

    def wall_deadline_epoch(self, session_id: str) -> float | None:
        from runner.contracts.clock import epoch_from_iso

        row = self._row(session_id)
        return epoch_from_iso(row["wall_deadline_utc"])

    def wall_expired(self, session_id: str) -> bool:
        deadline = self.wall_deadline_epoch(session_id)
        return deadline is not None and self.clock.epoch() >= deadline

    def set_active_attempt(self, session_id: str, attempt_id: str | None) -> None:
        self.store.execute(
            "UPDATE sessions SET active_attempt_id=?, status_version = status_version + 1 WHERE session_id=?",
            (attempt_id, session_id),
        )

    def finalize_candidate(
        self, session_id: str, *, candidate_id: str, build_id: str, recipe_hash: str
    ) -> None:
        self.store.execute(
            """
            UPDATE sessions
               SET finalized_candidate_id=?, finalized_build_id=?, recipe_hash=?,
                   status_version = status_version + 1
             WHERE session_id=?
            """,
            (candidate_id, build_id, recipe_hash, session_id),
        )

    # -- internals -----------------------------------------------------------
    def _row(self, session_id: str):
        row = self.store.query_one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        if row is None:
            raise ProtocolError("not_found", detail="session not found")
        return row

    def _allocate_counters(self, session_id: str, config: SessionConfig) -> None:
        from runner.core.budget import session_counter_limits

        for key, limit in session_counter_limits(config).items():
            self.store.counter_ensure(session_id, key, limit)


def _default_methods() -> tuple[str, ...]:
    return (
        "get_capabilities",
        "get_session",
        "get_operation",
        "get_attempt",
        "cancel_operation",
        "submit_candidate",
        "build_candidate",
        "begin_attempt",
        "perform_action",
        "list_tasks",
        "get_task",
        "wait_task",
        "stop_task",
        "read_feedback",
        "finalize_candidate",
        "finish_attempt",
        "abort_attempt",
        "close_session",
    )


def _new_token() -> str:
    return os.urandom(CREDENTIAL_BYTES).hex()


def _hash_token(salt: str, token: str) -> str:
    return hmac.new(bytes.fromhex(salt), token.encode("utf-8"), hashlib.sha256).hexdigest()


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _iso(epoch_seconds: float) -> str:
    from runner.contracts.clock import iso_from_epoch

    return iso_from_epoch(epoch_seconds)
