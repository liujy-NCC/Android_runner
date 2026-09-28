"""Idempotency: same key + same request returns the original result.

The record is written in the same transaction as the budget reservation, before
any side effect. A concurrent duplicate waits for the winner's stored response
and returns it verbatim, so the side effect happens once.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from runner.errors import ProtocolError
from runner.store.db import Store

STATE_PENDING = "pending"
STATE_COMPLETED = "completed"

# How long a duplicate may wait for the winning request to finish.
DUPLICATE_WAIT_SECONDS = 10.0
_POLL_SECONDS = 0.02


def request_hash(method: str, payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(f"{method}\n{canonical}".encode("utf-8")).hexdigest()


class DuplicateInFlight(Exception):
    """Raised inside a transaction when another request already claimed the key.

    Signals the caller to abort its transaction (so no reservation or row is
    created) and then wait for the winner's stored response.
    """


class IdempotencyGuard:
    def __init__(self, store: Store) -> None:
        self.store = store

    def lookup(self, session_id: str, scope: str, key: str, request_hash_value: str) -> dict[str, Any] | None:
        row = self.store.query_one(
            "SELECT * FROM idempotency WHERE session_id=? AND scope=? AND idempotency_key=?",
            (session_id, scope, key),
        )
        if row is None:
            return None
        if row["request_hash"] != request_hash_value:
            raise ProtocolError(
                "idempotency_conflict",
                detail=f"scope={scope} key reused with different body",
            )
        return dict(row)

    def wait_for_completion(self, session_id: str, scope: str, key: str) -> dict[str, Any]:
        """Block (bounded) until the winning request stores its response."""

        deadline = time.monotonic() + DUPLICATE_WAIT_SECONDS
        while time.monotonic() < deadline:
            row = self.store.query_one(
                "SELECT * FROM idempotency WHERE session_id=? AND scope=? AND idempotency_key=?",
                (session_id, scope, key),
            )
            if row is not None and row["state"] == STATE_COMPLETED and row["response_json"] is not None:
                return dict(row)
            time.sleep(_POLL_SECONDS)
        raise ProtocolError(
            "internal_error",
            detail=f"duplicate request for scope={scope} did not complete in time",
        )

    def claim(
        self,
        session_id: str,
        scope: str,
        key: str,
        request_hash_value: str,
        *,
        now: str,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Try to become the winner.

        Returns ``(True, None)`` for the winner, or ``(False, existing_row)``
        when the key is already claimed. Caller must be inside a transaction;
        on ``False`` it must roll back rather than create any side effect.
        """

        existing = self.lookup(session_id, scope, key, request_hash_value)
        if existing is not None:
            return False, existing
        self.store.execute(
            """
            INSERT INTO idempotency(session_id, scope, idempotency_key, request_hash, state, created_utc)
            VALUES (?,?,?,?,?,?)
            """,
            (session_id, scope, key, request_hash_value, STATE_PENDING, now),
        )
        return True, None

    def claim_or_rollback(
        self,
        session_id: str,
        scope: str,
        key: str,
        request_hash_value: str,
        *,
        now: str,
    ) -> None:
        """Claim the key inside the caller's transaction or abort it.

        Raises :class:`DuplicateInFlight` after marking the transaction for
        rollback so no budget reservation or object row can survive.
        """

        won, existing = self.claim(session_id, scope, key, request_hash_value, now=now)
        if not won:
            raise DuplicateInFlight(scope)

    def complete(
        self,
        session_id: str,
        scope: str,
        key: str,
        *,
        response: Any,
        http_status: int,
        operation_id: str | None,
        now: str,
    ) -> str:
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        self.store.execute(
            """
            UPDATE idempotency
               SET state=?, operation_id=?, http_status=?, response_json=?, response_sha256=?, completed_utc=?
             WHERE session_id=? AND scope=? AND idempotency_key=?
            """,
            (STATE_COMPLETED, operation_id, http_status, encoded, digest, now, session_id, scope, key),
        )
        return encoded
