"""SQLite control store: schema, migrations and transactional primitives.

Nothing in this module knows about HTTP, adapters or the Agent projection. It
only guarantees the durable invariants the design requires:

  * one open attempt per session
  * budget reserved before a side effect, settled afterwards
  * idempotency records written in the same transaction as the reservation
  * append-only event and budget ledgers
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 15_000

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id              TEXT PRIMARY KEY,
    state                   TEXT NOT NULL,
    sample_id               TEXT NOT NULL,
    sample_version          TEXT NOT NULL,
    config_sha256           TEXT NOT NULL,
    feedback_profile        TEXT NOT NULL,
    credential_salt         TEXT NOT NULL,
    credential_hash         TEXT NOT NULL,
    credential_expires_utc  TEXT,
    allowed_methods_json    TEXT NOT NULL,
    resource_roles_json     TEXT NOT NULL,
    status_version          INTEGER NOT NULL DEFAULT 1,
    active_attempt_id       TEXT,
    finalized_candidate_id  TEXT,
    finalized_build_id      TEXT,
    recipe_hash             TEXT,
    close_reason            TEXT,
    created_utc             TEXT NOT NULL,
    activated_utc           TEXT,
    wall_deadline_utc       TEXT,
    last_seen_utc           TEXT,
    closed_utc              TEXT,
    node_boot_id            TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_active_attempt
    ON sessions(active_attempt_id) WHERE active_attempt_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS counters (
    session_id     TEXT NOT NULL,
    counter_key    TEXT NOT NULL,
    limit_value    INTEGER,
    consumed_value INTEGER NOT NULL DEFAULT 0,
    reserved_value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, counter_key)
);

CREATE TABLE IF NOT EXISTS counter_ledger (
    entry_id      TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    counter_key   TEXT NOT NULL,
    delta_reserved INTEGER NOT NULL DEFAULT 0,
    delta_consumed INTEGER NOT NULL DEFAULT 0,
    reason        TEXT NOT NULL,
    ref_type      TEXT,
    ref_id        TEXT,
    created_utc   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    session_id      TEXT NOT NULL,
    scope           TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    state           TEXT NOT NULL,
    operation_id    TEXT,
    http_status     INTEGER,
    response_json   TEXT,
    response_sha256 TEXT,
    created_utc     TEXT NOT NULL,
    completed_utc   TEXT,
    PRIMARY KEY (session_id, scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    method       TEXT NOT NULL,
    state        TEXT NOT NULL,
    result_json  TEXT,
    reason_code  TEXT,
    error_code   TEXT,
    attempt_id   TEXT,
    candidate_id TEXT,
    build_id     TEXT,
    task_id      TEXT,
    created_utc  TEXT NOT NULL,
    updated_utc  TEXT NOT NULL,
    deadline_utc TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id        TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL,
    build_profile       TEXT NOT NULL,
    tree_hash           TEXT NOT NULL,
    file_manifest_json  TEXT NOT NULL,
    manifest_sha256     TEXT NOT NULL,
    path_policy_version TEXT NOT NULL,
    file_count          INTEGER NOT NULL,
    source_bytes        INTEGER NOT NULL,
    state               TEXT NOT NULL,
    created_utc         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidates_session ON candidates(session_id, created_utc);

CREATE TABLE IF NOT EXISTS candidate_files (
    candidate_id  TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    ordinal       INTEGER NOT NULL,
    size_bytes    INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    PRIMARY KEY (candidate_id, relative_path)
);

CREATE TABLE IF NOT EXISTS tree_hashes (
    session_id     TEXT NOT NULL,
    tree_hash      TEXT NOT NULL,
    candidate_id   TEXT NOT NULL,
    first_seen_utc TEXT NOT NULL,
    PRIMARY KEY (session_id, tree_hash)
);

CREATE TABLE IF NOT EXISTS builds (
    build_id                TEXT PRIMARY KEY,
    session_id              TEXT NOT NULL,
    candidate_id            TEXT NOT NULL,
    tree_hash               TEXT NOT NULL,
    state                   TEXT NOT NULL,
    application_id          TEXT,
    template_id             TEXT,
    template_sha256         TEXT,
    toolchain_id            TEXT,
    toolchain_fingerprint   TEXT,
    dependency_lock_sha256  TEXT,
    signing_profile         TEXT,
    environment_fingerprint TEXT,
    exit_code               INTEGER,
    log_artifact_id         TEXT,
    apk_artifact_id         TEXT,
    apk_sha256              TEXT,
    apk_size_bytes          INTEGER,
    package_name            TEXT,
    version_code            INTEGER,
    signer_fingerprint      TEXT,
    manifest_sha256         TEXT,
    permissions_json        TEXT,
    components_json         TEXT,
    queries_json            TEXT,
    entry_component         TEXT,
    build_provenance        TEXT,
    failure_json            TEXT,
    attempt_deadline_utc    TEXT,
    created_utc             TEXT NOT NULL,
    completed_utc           TEXT
);

CREATE INDEX IF NOT EXISTS idx_builds_candidate ON builds(candidate_id, created_utc);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id                  TEXT PRIMARY KEY,
    session_id                  TEXT NOT NULL,
    attempt_no                  INTEGER NOT NULL,
    candidate_id                TEXT NOT NULL,
    build_id                    TEXT NOT NULL,
    replay_of                   TEXT,
    state                       TEXT NOT NULL,
    execution_status            TEXT,
    recovery_status             TEXT,
    oracle_verdict              TEXT,
    evaluation_status           TEXT,
    device_health               TEXT NOT NULL DEFAULT 'unknown',
    reason_code                 TEXT,
    effective_agent_attempt     INTEGER NOT NULL DEFAULT 1,
    infrastructure_retry        INTEGER NOT NULL DEFAULT 0,
    stages_json                 TEXT NOT NULL,
    status_version              INTEGER NOT NULL DEFAULT 1,
    device_id                   TEXT,
    device_boot_id              TEXT,
    launch_dispatch_utc         TEXT,
    launch_dispatch_monotonic   REAL,
    launch_confirmed_monotonic  REAL,
    launch_confirmed_utc        TEXT,
    observation_deadline_monotonic REAL,
    observation_deadline_utc    TEXT,
    observation_window_complete INTEGER NOT NULL DEFAULT 0,
    launch_count                INTEGER NOT NULL DEFAULT 0,
    prepare_deadline_utc        TEXT,
    launch_deadline_utc         TEXT,
    finalize_deadline_utc       TEXT,
    evidence_sealed             INTEGER NOT NULL DEFAULT 0,
    cleanup_sealed              INTEGER NOT NULL DEFAULT 0,
    created_utc                 TEXT NOT NULL,
    closed_utc                  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_session_no ON attempts(session_id, attempt_no);
CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_open ON attempts(session_id)
    WHERE state <> 'CLOSED';

CREATE TABLE IF NOT EXISTS attempt_actions (
    action_id    TEXT PRIMARY KEY,
    attempt_id   TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    action_name  TEXT NOT NULL,
    params_json  TEXT NOT NULL,
    params_sha256 TEXT NOT NULL,
    state        TEXT NOT NULL,
    operation_id TEXT,
    result_json  TEXT,
    reason_code  TEXT,
    created_utc  TEXT NOT NULL,
    updated_utc  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id           TEXT PRIMARY KEY,
    attempt_id        TEXT NOT NULL,
    session_id        TEXT NOT NULL,
    role              TEXT NOT NULL,
    name              TEXT NOT NULL,
    state             TEXT NOT NULL,
    readiness         TEXT NOT NULL,
    exit_reason       TEXT,
    required          INTEGER NOT NULL DEFAULT 0,
    dependencies_json TEXT NOT NULL,
    deadline_utc      TEXT,
    handle            TEXT,
    evidence_refs_json TEXT NOT NULL,
    adapter_version   TEXT,
    artifact_sha256   TEXT,
    run_identity      TEXT,
    created_utc       TEXT NOT NULL,
    updated_utc       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id          TEXT PRIMARY KEY,
    session_id           TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    content_sha256       TEXT,
    size_bytes           INTEGER NOT NULL DEFAULT 0,
    media_type           TEXT NOT NULL,
    state                TEXT NOT NULL,
    staging_path         TEXT,
    relative_object_path TEXT,
    source               TEXT NOT NULL,
    provenance           TEXT NOT NULL,
    attempt_id           TEXT,
    candidate_id         TEXT,
    build_id             TEXT,
    created_utc          TEXT NOT NULL,
    sealed_utc           TEXT
);

CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id, kind);

CREATE TABLE IF NOT EXISTS seal_groups (
    seal_id           TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    attempt_id        TEXT,
    kind              TEXT NOT NULL,
    state             TEXT NOT NULL,
    member_hashes_json TEXT NOT NULL,
    group_sha256      TEXT,
    created_utc       TEXT NOT NULL,
    sealed_utc        TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    node          TEXT NOT NULL,
    node_boot_id  TEXT NOT NULL,
    node_seq      INTEGER NOT NULL,
    utc           TEXT NOT NULL,
    monotonic     REAL NOT NULL,
    event_version INTEGER NOT NULL DEFAULT 1,
    candidate_id  TEXT,
    build_id      TEXT,
    attempt_id    TEXT,
    action_id     TEXT,
    task_id       TEXT,
    resource_id   TEXT,
    payload_json  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, node_seq);
CREATE INDEX IF NOT EXISTS idx_events_attempt ON events(attempt_id, utc);

CREATE TABLE IF NOT EXISTS diagnostics (
    diagnostic_id  TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    candidate_id   TEXT NOT NULL,
    tree_hash      TEXT NOT NULL,
    build_id       TEXT,
    attempt_id     TEXT,
    scope          TEXT NOT NULL,
    kind           TEXT NOT NULL,
    ordinal        INTEGER NOT NULL,
    payload_json   TEXT NOT NULL,
    sanitized      INTEGER NOT NULL DEFAULT 1,
    incomplete     INTEGER NOT NULL DEFAULT 0,
    created_utc    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_diagnostics_scope
    ON diagnostics(session_id, tree_hash, scope, ordinal);

CREATE TABLE IF NOT EXISTS feedback_ledger (
    session_id TEXT NOT NULL,
    tree_hash  TEXT NOT NULL,
    pages_used INTEGER NOT NULL DEFAULT 0,
    bytes_used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, tree_hash)
);

CREATE TABLE IF NOT EXISTS read_ledger (
    session_id  TEXT NOT NULL,
    counter_key TEXT NOT NULL,
    count_used  INTEGER NOT NULL DEFAULT 0,
    bytes_used  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, counter_key)
);

CREATE TABLE IF NOT EXISTS rejections (
    rejection_id     TEXT PRIMARY KEY,
    session_id       TEXT,
    scope            TEXT NOT NULL,
    reason_code      TEXT,
    error_code       TEXT,
    detail_sanitized TEXT,
    created_utc      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rejections_session ON rejections(session_id, created_utc);

CREATE TABLE IF NOT EXISTS device_leases (
    resource_role TEXT PRIMARY KEY,
    session_id    TEXT,
    attempt_id    TEXT,
    device_id     TEXT,
    generation    INTEGER NOT NULL DEFAULT 1,
    health        TEXT NOT NULL DEFAULT 'unknown',
    acquired_utc  TEXT,
    released_utc  TEXT
);

CREATE TABLE IF NOT EXISTS oracle_evaluations (
    evaluation_id     TEXT PRIMARY KEY,
    attempt_id        TEXT NOT NULL,
    session_id        TEXT NOT NULL,
    oracle_id         TEXT NOT NULL,
    oracle_version    TEXT NOT NULL,
    criteria_version  TEXT NOT NULL,
    verdict           TEXT NOT NULL,
    evaluation_status TEXT NOT NULL,
    observations_json TEXT,
    reason_private    TEXT,
    evidence_refs_json TEXT NOT NULL,
    created_utc       TEXT NOT NULL,
    completed_utc     TEXT
);

CREATE TABLE IF NOT EXISTS supervisor_state (
    supervisor_id  TEXT PRIMARY KEY,
    node_boot_id   TEXT NOT NULL,
    started_utc    TEXT NOT NULL,
    heartbeat_utc  TEXT NOT NULL,
    last_sweep_utc TEXT
);

CREATE TABLE IF NOT EXISTS node_seq (
    node_boot_id TEXT PRIMARY KEY,
    last_seq     INTEGER NOT NULL DEFAULT 0
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class Store:
    """Thread-safe SQLite wrapper with explicit transaction helpers."""

    def __init__(self, db_path: str, *, clock: Any | None = None) -> None:
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._clock = clock
        self._init_schema()

    # -- connection handling -------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA synchronous=FULL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _init_schema(self) -> None:
        with self._lock:
            conn = self.conn
            conn.executescript(SCHEMA_SQL)
            row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            current = row["v"] if row and row["v"] is not None else 0
            if current < SCHEMA_VERSION:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version, applied_utc) VALUES (?,?)",
                    (SCHEMA_VERSION, self._now()),
                )

    def _now(self) -> str:
        if self._clock is not None:
            return self._clock.utc()
        from runner.contracts.clock import utc_now_iso

        return utc_now_iso()

    # -- transaction helpers -------------------------------------------------
    def begin_immediate(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.OperationalError:  # pragma: no cover - no active txn
            pass

    class _Txn:
        """Transaction scope.

        ``DuplicateInFlight`` is raised by the idempotency guard to mean "another
        request already owns this key". The transaction rolls back so that no
        budget reservation, build row, attempt row or artifact survives; the
        caller then waits for the winner's stored response.
        """

        def __init__(self, store: "Store") -> None:
            self.store = store

        def __enter__(self) -> sqlite3.Connection:
            self.store._lock.acquire()
            self.store.begin_immediate()
            return self.store.conn

        def __exit__(self, exc_type, exc, tb) -> bool:
            try:
                if exc_type is None:
                    self.store.commit()
                else:
                    self.store.rollback()
            finally:
                self.store._lock.release()
            return False

    def txn(self) -> "Store._Txn":
        return Store._Txn(self)

    # -- generic access ------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    # -- row helpers ---------------------------------------------------------
    @staticmethod
    def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def loads(value: str | None, default: Any = None) -> Any:
        if value is None or value == "":
            return default
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def dumps(value: Any) -> str:
        return _json(value)

    # -- counters ------------------------------------------------------------
    def counter_ensure(self, session_id: str, counter_key: str, limit_value: int | None) -> None:
        self.conn.execute(
            """
            INSERT INTO counters(session_id, counter_key, limit_value, consumed_value, reserved_value)
            VALUES (?,?,?,0,0)
            ON CONFLICT(session_id, counter_key) DO UPDATE SET
                limit_value = COALESCE(excluded.limit_value, counters.limit_value)
            """,
            (session_id, counter_key, limit_value),
        )

    def counter_row(self, session_id: str, counter_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM counters WHERE session_id=? AND counter_key=?",
            (session_id, counter_key),
        ).fetchone()

    def counter_available(self, session_id: str, counter_key: str, amount: int) -> bool:
        """Return True when ``amount`` fits within the configured limit."""

        row = self.counter_row(session_id, counter_key)
        if row is None:
            return True
        limit_value = row["limit_value"]
        if limit_value is None:
            return True
        return row["consumed_value"] + row["reserved_value"] + amount <= limit_value

    def counter_reserve(
        self,
        session_id: str,
        counter_key: str,
        amount: int,
        *,
        reason: str,
        ref_type: str | None = None,
        ref_id: str | None = None,
        ledger_id: str,
        now: str,
    ) -> None:
        self.conn.execute(
            "UPDATE counters SET reserved_value = reserved_value + ? WHERE session_id=? AND counter_key=?",
            (amount, session_id, counter_key),
        )
        self.conn.execute(
            """
            INSERT INTO counter_ledger(entry_id, session_id, counter_key, delta_reserved, delta_consumed,
                                       reason, ref_type, ref_id, created_utc)
            VALUES (?,?,?,?,0,?,?,?,?)
            """,
            (ledger_id, session_id, counter_key, amount, reason, ref_type, ref_id, now),
        )

    def counter_settle(
        self,
        session_id: str,
        counter_key: str,
        *,
        reserved: int,
        consumed: int,
        reason: str,
        ref_type: str | None = None,
        ref_id: str | None = None,
        ledger_id: str,
        now: str,
    ) -> None:
        self.conn.execute(
            """
            UPDATE counters
               SET reserved_value = MAX(0, reserved_value - ?),
                   consumed_value = consumed_value + ?
             WHERE session_id=? AND counter_key=?
            """,
            (reserved, consumed, session_id, counter_key),
        )
        self.conn.execute(
            """
            INSERT INTO counter_ledger(entry_id, session_id, counter_key, delta_reserved, delta_consumed,
                                       reason, ref_type, ref_id, created_utc)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (ledger_id, session_id, counter_key, -reserved, consumed, reason, ref_type, ref_id, now),
        )

    def counter_snapshot(self, session_id: str) -> dict[str, dict[str, int | None]]:
        rows = self.conn.execute(
            "SELECT counter_key, limit_value, consumed_value, reserved_value FROM counters WHERE session_id=?",
            (session_id,),
        ).fetchall()
        return {
            row["counter_key"]: {
                "limit": row["limit_value"],
                "consumed": row["consumed_value"],
                "reserved": row["reserved_value"],
            }
            for row in rows
        }

    # -- events --------------------------------------------------------------
    def node_seq_next(self, node_boot_id: str) -> int:
        self.conn.execute(
            "INSERT INTO node_seq(node_boot_id, last_seq) VALUES (?, 1) "
            "ON CONFLICT(node_boot_id) DO UPDATE SET last_seq = last_seq + 1",
            (node_boot_id,),
        )
        row = self.conn.execute(
            "SELECT last_seq FROM node_seq WHERE node_boot_id=?", (node_boot_id,)
        ).fetchone()
        return int(row["last_seq"])

    def insert_event(self, event: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO events(event_id, session_id, event_type, node, node_boot_id, node_seq,
                               utc, monotonic, event_version, candidate_id, build_id, attempt_id,
                               action_id, task_id, resource_id, payload_json)
            VALUES (:event_id,:session_id,:event_type,:node,:node_boot_id,:node_seq,
                    :utc,:monotonic,:event_version,:candidate_id,:build_id,:attempt_id,
                    :action_id,:task_id,:resource_id,:payload_json)
            """,
            event,
        )

    def rejections_since(self, session_id: str, since_utc: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM rejections WHERE session_id=? AND created_utc >= ?",
            (session_id, since_utc),
        ).fetchone()
        return int(row["n"]) if row else 0

    def read_remaining(
        self,
        session_id: str,
        counter_key: str,
        *,
        max_count: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, int | None]:
        """Return remaining read quota for a ledger counter (caller's txn)."""

        row = self.conn.execute(
            "SELECT count_used, bytes_used FROM read_ledger WHERE session_id=? AND counter_key=?",
            (session_id, counter_key),
        ).fetchone()
        used_count = int(row["count_used"]) if row else 0
        used_bytes = int(row["bytes_used"]) if row else 0
        return {
            "count_left": None if max_count is None else max(0, max_count - used_count),
            "bytes_left": None if max_bytes is None else max(0, max_bytes - used_bytes),
        }

    def consume_read(
        self,
        session_id: str,
        counter_key: str,
        *,
        count: int = 1,
        bytes_: int = 0,
        max_count: int | None = None,
        max_bytes: int | None = None,
    ) -> str | None:
        """Atomically charge a read against a ledger counter.

        Quota is charged on the *actually delivered* UTF-8 JSON byte length, so
        the caller serializes first and charges second. Returns ``None`` on
        success, otherwise a ``truncation_reason``.
        """

        self.conn.execute(
            "INSERT OR IGNORE INTO read_ledger(session_id, counter_key, count_used, bytes_used) VALUES (?,?,0,0)",
            (session_id, counter_key),
        )
        row = self.conn.execute(
            "SELECT count_used, bytes_used FROM read_ledger WHERE session_id=? AND counter_key=?",
            (session_id, counter_key),
        ).fetchone()
        if row is None:  # pragma: no cover - defensive
            return "STATUS_CALL_LIMIT"
        if max_count is not None and row["count_used"] + count > max_count:
            return "STATUS_CALL_LIMIT"
        if max_bytes is not None and row["bytes_used"] + bytes_ > max_bytes:
            return "STATUS_RESPONSE_BYTES_LIMIT"
        self.conn.execute(
            "UPDATE read_ledger SET count_used = count_used + ?, bytes_used = bytes_used + ? "
            "WHERE session_id=? AND counter_key=?",
            (count, bytes_, session_id, counter_key),
        )
        return None

    def feedback_remaining(
        self, session_id: str, tree_hash: str, *, max_pages: int, max_bytes: int
    ) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT pages_used, bytes_used FROM feedback_ledger WHERE session_id=? AND tree_hash=?",
            (session_id, tree_hash),
        ).fetchone()
        used_pages = int(row["pages_used"]) if row else 0
        used_bytes = int(row["bytes_used"]) if row else 0
        return {
            "pages_left": max(0, max_pages - used_pages),
            "bytes_left": max(0, max_bytes - used_bytes),
            "pages_used": used_pages,
            "bytes_used": used_bytes,
        }

    def feedback_charge(
        self,
        session_id: str,
        tree_hash: str,
        *,
        pages: int,
        bytes_: int,
        max_pages: int,
        max_bytes: int,
    ) -> str | None:
        """Charge diagnostic quota for a frozen candidate, atomically.

        The billing key is the frozen source tree hash, so all attempts and
        infrastructure retries of the same candidate share one budget.
        """

        self.conn.execute(
            "INSERT OR IGNORE INTO feedback_ledger(session_id, tree_hash, pages_used, bytes_used) VALUES (?,?,0,0)",
            (session_id, tree_hash),
        )
        row = self.conn.execute(
            "SELECT pages_used, bytes_used FROM feedback_ledger WHERE session_id=? AND tree_hash=?",
            (session_id, tree_hash),
        ).fetchone()
        assert row is not None
        if row["pages_used"] + pages > max_pages:
            return "DIAGNOSTIC_CANDIDATE_PAGE_LIMIT"
        if row["bytes_used"] + bytes_ > max_bytes:
            return "DIAGNOSTIC_CANDIDATE_BYTES_LIMIT"
        self.conn.execute(
            "UPDATE feedback_ledger SET pages_used = pages_used + ?, bytes_used = bytes_used + ? "
            "WHERE session_id=? AND tree_hash=?",
            (pages, bytes_, session_id, tree_hash),
        )
        return None

    def read_ledger_row(self, session_id: str, counter_key: str) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT count_used, bytes_used FROM read_ledger WHERE session_id=? AND counter_key=?",
            (session_id, counter_key),
        ).fetchone()
        if row is None:
            return {"count_used": 0, "bytes_used": 0}
        return {"count_used": int(row["count_used"]), "bytes_used": int(row["bytes_used"])}

    def read_ledger_forfeit(
        self, session_id: str, counter_key: str, *, count: int = 1, bytes_: int = 0
    ) -> None:
        """Consume quota for a response we are about to deliver anyway.

        Used when a reservation already succeeded and the final serialized
        length differs from the estimate; the ledger must reflect the bytes
        actually delivered.
        """

        self.conn.execute(
            "UPDATE read_ledger SET count_used = count_used + ?, bytes_used = bytes_used + ? "
            "WHERE session_id=? AND counter_key=?",
            (count, bytes_, session_id, counter_key),
        )
