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

-- One row per (operation, counter) reservation. Recovery must act on the exact
-- reservations of ONE operation and never on the whole session: a session-wide
-- sweep would refund budget belonging to a *different*, still-running request.
-- ``state`` is the once-only latch that makes repeated recovery a no-op.
CREATE TABLE IF NOT EXISTS counter_reservations (
    reservation_id TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    operation_id   TEXT,
    counter_key    TEXT NOT NULL,
    amount         INTEGER NOT NULL,
    state          TEXT NOT NULL,
    reason         TEXT NOT NULL,
    ref_type       TEXT,
    ref_id         TEXT,
    created_utc    TEXT NOT NULL,
    closed_utc     TEXT,
    close_reason   TEXT,
    close_ledger_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_counter_reservations_operation
    ON counter_reservations(operation_id, state);
CREATE INDEX IF NOT EXISTS idx_counter_reservations_session
    ON counter_reservations(session_id, state);

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
    deadline_utc TEXT,
    -- The host boot that owned this operation while it was in flight. Used to
    -- tell "the owner process is gone" (previous boot, safe to reconcile) from
    -- "the owner may still be alive" (same boot: never reclaim on age alone).
    owner_boot_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_operations_open
    ON operations(state, deadline_utc);

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
    -- The host boot that produced the monotonic values above. A different boot
    -- means those values were taken on a clock that has since restarted.
    monotonic_boot_id           TEXT,
    monotonic_boot_anchor       REAL,
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

-- The host boot this control database belongs to. ``boot_anchor_epoch`` is the
-- estimated host boot instant (wall - monotonic). Monotonic deadlines persisted
-- by one boot must never be compared against another boot's monotonic clock, so
-- every process checks this anchor before trusting them.
CREATE TABLE IF NOT EXISTS host_boot (
    boot_id            TEXT PRIMARY KEY,
    boot_anchor_epoch  REAL NOT NULL,
    first_seen_utc     TEXT NOT NULL,
    last_seen_utc      TEXT NOT NULL,
    seen_count         INTEGER NOT NULL DEFAULT 1,
    superseded_utc     TEXT
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
        self.host_boot: dict[str, Any] = {}
        self._init_schema()
        self._bind_host_boot()

    def _bind_host_boot(self) -> None:
        """Pin this process to one persisted host-boot identity.

        Monotonic values are only comparable within a boot, so the process and
        the database must agree on which boot they belong to *before* anything
        reads a persisted monotonic deadline.
        """

        anchor = None
        if self._clock is not None and hasattr(self._clock, "boot_anchor"):
            anchor = self._clock.boot_anchor()
        if anchor is None:  # pragma: no cover - only for clock-less stores
            return
        with self.txn():
            active = self.active_host_boot()
            from runner.contracts.boot import classify_boot_anchor, boot_id_for_anchor

            verdict = classify_boot_anchor(
                None if active is None else float(active["boot_anchor_epoch"]), float(anchor)
            )
            if verdict == "same_boot" and active is not None:
                # Adopt the persisted identity so every process in one host boot
                # reports the same node_boot_id and cannot split the boot.
                boot_id = active["boot_id"]
                anchor = float(active["boot_anchor_epoch"])
            else:
                # First boot for this database (or a genuine reboot): the clock's
                # own identity wins when it has one, so an explicitly supplied
                # identity is honoured and remains verifiable against the anchor.
                boot_id = getattr(self._clock, "boot_id", lambda: None)() or boot_id_for_anchor(
                    float(anchor)
                )
            row = self.bind_host_boot(boot_id=boot_id, anchor_epoch=float(anchor), now_utc=self._now())
        self.host_boot = dict(row)
        if self._clock is not None and hasattr(self._clock, "adopt_boot_id"):
            self._clock.adopt_boot_id(boot_id, float(anchor))

    def boot_anchor(self) -> float | None:
        value = self.host_boot.get("boot_anchor_epoch")
        return None if value is None else float(value)

    def refresh_host_boot(self) -> dict[str, Any]:
        """Re-check the live clock's boot anchor and rebind if it has moved.

        In production a host reboot means a new process, so this is normally a
        no-op. Re-checking makes the invariant explicit and testable: any read
        path that is about to compare a persisted monotonic deadline first
        confirms which host boot it is actually running in.
        """

        if self._clock is None or not hasattr(self._clock, "boot_anchor"):
            return self.host_boot
        live = self._clock.boot_anchor()
        if live is None:
            return self.host_boot
        if self.host_boot.get("boot_anchor_epoch") is None or not self.monotonic_deadline_is_comparable(
            float(live)
        ):
            self._bind_host_boot()
        return self.host_boot

    def host_rebooted_since(self, anchor_epoch: float | None) -> bool:
        """True when a persisted monotonic deadline predates this host boot."""

        if anchor_epoch is None:
            return False
        active = self.host_boot.get("boot_anchor_epoch")
        if active is None:
            return True
        from runner.contracts.boot import classify_boot_anchor

        return classify_boot_anchor(float(active), float(anchor_epoch)) == "host_reboot"

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
            self._add_missing_columns(conn)
            row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            current = row["v"] if row and row["v"] is not None else 0
            if current < SCHEMA_VERSION:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version, applied_utc) VALUES (?,?)",
                    (SCHEMA_VERSION, self._now()),
                )

    # Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` does
    # not change an existing table, so an already-created control database needs
    # an explicit ALTER. Additive and idempotent on purpose: no data is touched.
    _ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("operations", "owner_boot_id", "TEXT"),
    )

    def _add_missing_columns(self, conn: sqlite3.Connection) -> None:
        for table, column, decl in self._ADDED_COLUMNS:
            existing = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

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

    def counter_reserved_breakdown(self, session_id: str) -> list[sqlite3.Row]:
        """Every counter of one session that still holds a reserved amount.

        Diagnostic only. Recovery must never act on this list: it aggregates
        reservations from every request in the session, so settling from it
        would refund budget belonging to a different, still-running request.
        """

        return list(
            self.conn.execute(
                "SELECT counter_key, reserved_value FROM counters "
                "WHERE session_id=? AND reserved_value > 0 ORDER BY counter_key",
                (session_id,),
            ).fetchall()
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

    # -- per-operation reservations -----------------------------------------
    def reservation_create(
        self,
        *,
        reservation_id: str,
        session_id: str,
        operation_id: str | None,
        counter_key: str,
        amount: int,
        reason: str,
        ref_type: str | None = None,
        ref_id: str | None = None,
        now: str,
    ) -> None:
        """Record one reserved counter amount against the operation that holds it.

        The row is the audit trail that lets recovery settle *exactly* the
        reservations of one dead operation, and the ``state`` column is the
        once-only latch that keeps repeated recovery from double-settling.
        """

        self.conn.execute(
            """
            INSERT INTO counter_reservations(reservation_id, session_id, operation_id, counter_key,
                                             amount, state, reason, ref_type, ref_id, created_utc)
            VALUES (?,?,?,?,?, 'OPEN', ?,?,?,?)
            """,
            (
                reservation_id,
                session_id,
                operation_id,
                counter_key,
                amount,
                reason,
                ref_type,
                ref_id,
                now,
            ),
        )

    def reservations_for_operation(self, operation_id: str, *, state: str = "OPEN") -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM counter_reservations WHERE operation_id=? AND state=? "
                "ORDER BY counter_key, reservation_id",
                (operation_id, state),
            ).fetchall()
        )

    def reservation_load(self, reservation_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM counter_reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()

    def reservation_close(
        self,
        reservation_id: str,
        *,
        consume: bool,
        reason: str,
        ledger_id: str,
        now: str,
    ) -> dict[str, Any] | None:
        """Close one reservation exactly once, charging or refunding it.

        ``consume=True`` charges the reserved amount (the operation was accepted,
        so the budget is spent even if the outcome is unknown). ``consume=False``
        refunds it (nothing with a side effect ever ran).

        Returns the applied effect, or ``None`` when the reservation was already
        closed. The ``state='OPEN'`` predicate in the UPDATE is the latch: two
        concurrent or repeated recoveries cannot both apply an effect, so the
        counters can never be charged or refunded twice.
        """

        row = self.reservation_load(reservation_id)
        if row is None or row["state"] != "OPEN":
            return None
        cursor = self.conn.execute(
            "UPDATE counter_reservations SET state=?, closed_utc=?, close_reason=?, close_ledger_id=? "
            "WHERE reservation_id=? AND state='OPEN'",
            ("SETTLED" if consume else "RELEASED", now, reason, ledger_id, reservation_id),
        )
        if cursor.rowcount != 1:  # pragma: no cover - lost the latch race
            return None
        self.counter_settle(
            row["session_id"],
            row["counter_key"],
            reserved=int(row["amount"]),
            consumed=int(row["amount"]) if consume else 0,
            reason=reason,
            ref_type="operation",
            ref_id=row["operation_id"],
            ledger_id=ledger_id,
            now=now,
        )
        return {
            "reservation_id": reservation_id,
            "counter_key": row["counter_key"],
            "amount": int(row["amount"]),
            "consumed": int(row["amount"]) if consume else 0,
        }

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

    # -- host boot identity --------------------------------------------------
    def active_host_boot(self) -> dict[str, Any] | None:
        """The current (not superseded) host boot anchor, if one is persisted."""

        row = self.conn.execute(
            "SELECT * FROM host_boot WHERE superseded_utc IS NULL "
            "ORDER BY first_seen_utc DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None

    def bind_host_boot(self, *, boot_id: str, anchor_epoch: float, now_utc: str) -> dict[str, Any]:
        """Persist ``anchor_epoch`` as active, superseding any other boot.

        Called once per process start, inside a transaction, so two processes
        starting concurrently agree on exactly one active boot. A different
        anchor means the host rebooted: the previous boot is marked superseded
        and its monotonic deadlines become unusable.
        """

        active = self.active_host_boot()
        if active is not None and float(active["boot_anchor_epoch"]) == float(anchor_epoch):
            self.conn.execute(
                "UPDATE host_boot SET last_seen_utc=?, seen_count = seen_count + 1 WHERE boot_id=?",
                (now_utc, active["boot_id"]),
            )
            return active
        if active is not None:
            self.conn.execute(
                "UPDATE host_boot SET superseded_utc=? WHERE boot_id=?", (now_utc, active["boot_id"])
            )
        self.conn.execute(
            """
            INSERT INTO host_boot(boot_id, boot_anchor_epoch, first_seen_utc, last_seen_utc, seen_count)
            VALUES (?,?,?,?,1)
            ON CONFLICT(boot_id) DO UPDATE SET
                boot_anchor_epoch=excluded.boot_anchor_epoch,
                last_seen_utc=excluded.last_seen_utc,
                superseded_utc=NULL
            """,
            (boot_id, float(anchor_epoch), now_utc, now_utc),
        )
        return {
            "boot_id": boot_id,
            "boot_anchor_epoch": float(anchor_epoch),
            "first_seen_utc": now_utc,
        }

    def monotonic_deadline_is_comparable(self, anchor_epoch: float | None) -> bool:
        """Is a persisted monotonic deadline usable by this process?

        Only if this process's boot anchor matches the active persisted boot.
        After a host reboot the monotonic clock restarted, so a stale deadline
        must not be treated as already-elapsed or still-pending by comparison.
        """

        if anchor_epoch is None:
            return False
        active = self.active_host_boot()
        if active is None:
            return False
        from runner.contracts.boot import classify_boot_anchor

        return (
            classify_boot_anchor(float(active["boot_anchor_epoch"]), float(anchor_epoch)) == "same_boot"
        )

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
