"""The Runner core service: the single place where state, budget and evidence meet.

Every mutating method follows the same discipline required by DESIGN.md §4.7
and §5:

    parse -> authenticate -> schema -> ownership -> state machine -> budget
      -> reserve (persisted with the idempotency claim, before any side effect)
      -> side effect
      -> settle -> store the idempotent response

`begin_attempt` is the one method whose Agent-visible body is restricted to
``status`` / ``attempt_id`` / ``reason_code``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from typing import Any, Callable, Iterable, Sequence

from runner.adapters.base import BuildError, SourceFile
from runner.adapters.profiles import get_profile
from runner.config import SessionConfig
from runner.contracts.clock import Clock, epoch_from_iso, iso_from_epoch
from runner.contracts.ids import new_id
from runner.contracts.states import (
    ATTEMPT_ACCEPTED,
    ATTEMPT_CLOSED,
    ATTEMPT_FINALIZING,
    ATTEMPT_PREPARING,
    ATTEMPT_RUNNING,
    BUILD_FAILED,
    BUILD_PENDING,
    BUILD_RUNNING,
    BUILD_SUCCEEDED,
    CANDIDATE_FROZEN,
    DEVICE_HEALTH,
    OP_FAILED,
    OP_RUNNING,
    OP_SUCCEEDED,
    SESSION_ACTIVE,
    SESSION_CLOSING,
    SOURCE_BUILT,
    TASK_EXITED,
    TASK_REGISTERED,
    TASK_RUNNING,
)
from runner.core.attempt import (
    AttemptFinalizer,
    AttemptRuntime,
    collector_task_handles,
    empty_stages,
)
from runner.core.budget import (
    C_ACTIONS,
    C_BUILDS,
    C_DISTINCT_CANDIDATES,
    C_EFFECTIVE_ATTEMPTS,
    C_GET_ATTEMPT_CALLS,
    C_INFRA_RETRIES,
    C_TASKS,
    tree_builds,
    tree_counter_limits,
    tree_runs,
)
from runner.core.idempotency import DuplicateInFlight, IdempotencyGuard, request_hash
from runner.core.sessions import SessionRecord, SessionService
from runner.errors import (
    BudgetExhausted,
    BusinessRejection,
    CapabilityError,
    ProtocolError,
    StorageError,
)
from runner.store.artifacts import ArtifactStore
from runner.store.db import Store
from runner.store.events import EventLog

# Directory name for private per-attempt scratch (collector streams).
SCRATCH = "scratch"


class RunnerService:
    """Facade over sessions, candidates, builds and attempts."""

    def __init__(
        self,
        *,
        root: str,
        config: SessionConfig,
        store: Store | None = None,
        clock: Clock | None = None,
        device: Any | None = None,
        build_backend: Any | None = None,
        oracle: Any | None = None,
        projector: Any | None = None,
        collector_factory: Callable[..., dict[str, Any]] | None = None,
        oracle_scenario: str = "present_present",
    ) -> None:
        from runner.adapters.build_backend import MockBuildBackend
        from runner.adapters.device_mock import MockDeviceAdapter
        from runner.adapters.oracle import MockOracle
        from runner.adapters.projection import DiagnosticOnlyProjector
        from runner.contracts.clock import SystemClock

        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self.config = config
        self.clock = clock or SystemClock()
        self.store = store or Store(os.path.join(self.root, "control.db"), clock=self.clock)
        self.events = EventLog(self.store, clock=self.clock)
        self.artifacts = ArtifactStore(os.path.join(self.root, "artifacts"), self.store, self.events, clock=self.clock)
        self.sessions = SessionService(self.store, self.events, self.clock)
        self.idempotency = IdempotencyGuard(self.store)
        self.runtime = AttemptRuntime(self.store)

        self.device = device or MockDeviceAdapter()
        self.build_backend = build_backend or MockBuildBackend()
        self.oracle = oracle or MockOracle(scenario=oracle_scenario)
        self.projector = projector or DiagnosticOnlyProjector()
        self.collector_factory = collector_factory
        self._collector_instances: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _scratch_dir(self, attempt_id: str) -> str:
        path = os.path.join(self.root, SCRATCH, attempt_id)
        os.makedirs(path, exist_ok=True)
        return path

    def _counter_limits(self, session_id: str) -> dict[str, int | None]:
        from runner.core.budget import session_counter_limits

        limits = session_counter_limits(self.config)
        # Tree counters are allocated lazily when a candidate is frozen.
        for row in self.store.query_all(
            "SELECT counter_key, limit_value FROM counters WHERE session_id=?", (session_id,)
        ):
            limits.setdefault(row["counter_key"], row["limit_value"])
        return limits

    def _reserve(self, session_id: str, entries: Sequence[tuple[str, int]], *, reason: str, ref_type: str, ref_id: str | None) -> None:
        """Reserve all counters atomically, or fail before any side effect."""

        for key, amount in entries:
            if not self.store.counter_available(session_id, key, amount):
                raise BudgetExhausted(key)
        now = self.clock.utc()
        for key, amount in entries:
            self.store.counter_reserve(
                session_id,
                key,
                amount,
                reason=reason,
                ref_type=ref_type,
                ref_id=ref_id,
                ledger_id=new_id("event"),
                now=now,
            )

    def _settle(
        self,
        session_id: str,
        *,
        reserved: Sequence[tuple[str, int]],
        consumed: Sequence[tuple[str, int]],
        reason: str,
        ref_type: str,
        ref_id: str | None,
    ) -> None:
        consumed_map = dict(consumed)
        now = self.clock.utc()
        for key, amount in reserved:
            self.store.counter_settle(
                session_id,
                key,
                reserved=amount,
                consumed=consumed_map.get(key, 0),
                reason=reason,
                ref_type=ref_type,
                ref_id=ref_id,
                ledger_id=new_id("event"),
                now=now,
            )

    def _release(self, session_id: str, reserved: Sequence[tuple[str, int]], *, reason: str) -> None:
        """Release a reservation without charging it (pre-side-effect abort)."""

        self._settle(
            session_id,
            reserved=reserved,
            consumed=[],
            reason=reason,
            ref_type="release",
            ref_id=None,
        )

    def _record_rejection_in_txn(
        self,
        session_id: str | None,
        *,
        scope: str,
        reason_code: str | None = None,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> str:
        """Ledger a rejection inside the caller's transaction.

        Same effect as :meth:`_record_rejection` but composable, so a rejection
        and the idempotency receipt that replays it commit atomically. Rejections
        are never charged against execution budget.
        """

        rejection_id = new_id("rejection")
        self.store.execute(
            """
            INSERT INTO rejections(rejection_id, session_id, scope, reason_code, error_code,
                                   detail_sanitized, created_utc)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                rejection_id,
                session_id,
                scope,
                reason_code,
                error_code,
                (detail or "")[:400],
                self.clock.utc(),
            ),
        )
        if session_id:
            self.events.append(
                session_id,
                "request_rejected",
                payload={
                    "scope": scope,
                    "reason_code": reason_code,
                    "error_code": error_code,
                },
            )
        return rejection_id

    def _record_rejection(
        self,
        session_id: str | None,
        *,
        scope: str,
        reason_code: str | None = None,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Rejections are ledgered but never charged against execution budget."""

        with self.store.txn():
            self._record_rejection_in_txn(
                session_id,
                scope=scope,
                reason_code=reason_code,
                error_code=error_code,
                detail=detail,
            )

    def _check_rate(self, session_id: str) -> None:
        from runner.contracts.clock import iso_from_epoch

        window_start = iso_from_epoch(self.clock.epoch() - 60.0)
        limit = int(self.config.limits.get("max_rejections_per_minute", 240))
        if self.store.rejections_since(session_id, window_start) >= limit:
            raise ProtocolError("rate_limited")

    def _operation_create(
        self,
        session_id: str,
        method: str,
        *,
        state: str,
        attempt_id: str | None = None,
        candidate_id: str | None = None,
        build_id: str | None = None,
        task_id: str | None = None,
        deadline_utc: str | None = None,
    ) -> str:
        operation_id = new_id("operation")
        now = self.clock.utc()
        self.store.execute(
            """
            INSERT INTO operations(operation_id, session_id, method, state, attempt_id, candidate_id,
                                   build_id, task_id, created_utc, updated_utc, deadline_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                operation_id,
                session_id,
                method,
                state,
                attempt_id,
                candidate_id,
                build_id,
                task_id,
                now,
                now,
                deadline_utc,
            ),
        )
        return operation_id

    def _operation_finish(
        self,
        operation_id: str,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        reason_code: str | None = None,
        error_code: str | None = None,
    ) -> None:
        self.store.execute(
            """
            UPDATE operations SET state=?, result_json=?, reason_code=?, error_code=?, updated_utc=?
             WHERE operation_id=?
            """,
            (
                state,
                self.store.dumps(result) if result is not None else None,
                reason_code,
                error_code,
                self.clock.utc(),
                operation_id,
            ),
        )

    def _load_session(self, session_id: str) -> SessionRecord:
        row = self.store.query_one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        if row is None:
            raise ProtocolError("not_found", detail="session not found")
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

    # -- administrative (never on HTTP) -------------------------------------
    def create_session(
        self, config: SessionConfig | None = None, *, credential_ttl_seconds: float | None = None
    ) -> tuple[str, str]:
        cfg = config or self.config
        return self.sessions.create(cfg, credential_ttl_seconds=credential_ttl_seconds)

    def activate_session(self, session_id: str, config: SessionConfig | None = None) -> None:
        cfg = config or self.config
        self._allocate_tree_agnostic(cfg)
        self.sessions.activate(session_id, config=cfg)

    def _allocate_tree_agnostic(self, config: SessionConfig) -> None:
        return None

    def _create_attempt_row(
        self,
        *,
        session_id: str,
        candidate_id: str,
        build_id: str,
        replay_of: str | None = None,
        effective: bool = True,
        infrastructure_retry: bool = False,
    ) -> str:
        """Insert one attempt row. Caller owns the transaction and the budget.

        ``effective`` distinguishes an Agent attempt (counts against the
        per-candidate run allowance) from a controller-granted infrastructure
        replay (does not).
        """

        attempt_id = new_id("attempt")
        row = self.store.query_one(
            "SELECT COALESCE(MAX(attempt_no), 0) AS n FROM attempts WHERE session_id=?", (session_id,)
        )
        attempt_no = int(row["n"]) + 1 if row else 1
        stages = empty_stages()
        stages["intake"] = "running"
        self.store.execute(
            """
            INSERT INTO attempts(attempt_id, session_id, attempt_no, candidate_id, build_id,
                                 replay_of, state, stages_json, status_version, effective_agent_attempt,
                                 infrastructure_retry, prepare_deadline_utc, created_utc)
            VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?)
            """,
            (
                attempt_id,
                session_id,
                attempt_no,
                candidate_id,
                build_id,
                replay_of,
                ATTEMPT_ACCEPTED,
                self.store.dumps(stages),
                1 if effective else 0,
                1 if infrastructure_retry else 0,
                self._deadline_for("cold_boot_and_baseline"),
                self.clock.utc(),
            ),
        )
        self.store.execute(
            "UPDATE sessions SET active_attempt_id=?, status_version = status_version + 1 WHERE session_id=?",
            (attempt_id, session_id),
        )
        return attempt_id

    def read_private_oracle(self, attempt_id: str) -> dict[str, Any] | None:
        """PRIVATE researcher-only accessor. Deliberately not routed over HTTP."""

        row = self.store.query_one(
            "SELECT * FROM oracle_evaluations WHERE attempt_id=? ORDER BY created_utc DESC LIMIT 1",
            (attempt_id,),
        )
        return dict(row) if row else None

    def grant_infrastructure_retry(
        self, session_id: str, attempt_id: str, *, evidence_ref: str, reason: str
    ) -> str:
        """Controller-only attribution of a confirmed infrastructure fault.

        Only this private entry point may create a retry attempt reusing the
        original ``build_id``. Candidate-caused faults and ``outcome_unknown``
        are never replayed automatically, and there is no Agent-visible path to
        this call.
        """

        original = self.store.query_one(
            "SELECT * FROM attempts WHERE attempt_id=? AND session_id=?", (attempt_id, session_id)
        )
        if original is None:
            raise ProtocolError("not_found", detail="attempt not found")
        if not evidence_ref:
            raise ProtocolError("malformed_request", detail="independent evidence reference required")
        reserved = [(C_INFRA_RETRIES, 1)]
        with self.store.txn():
            self._reserve(
                session_id,
                reserved,
                reason="infrastructure_retry_attribution",
                ref_type="attempt",
                ref_id=attempt_id,
            )
            new_attempt_id = self._create_attempt_row(
                session_id=session_id,
                candidate_id=original["candidate_id"],
                build_id=original["build_id"],
                replay_of=attempt_id,
                effective=False,
                infrastructure_retry=True,
            )
            self._settle(
                session_id,
                reserved=reserved,
                consumed=reserved,
                reason="infrastructure_retry_attribution",
                ref_type="attempt",
                ref_id=new_attempt_id,
            )
            self.events.append(
                session_id,
                "infrastructure_retry_granted",
                payload={"replay_of": attempt_id, "reason": reason, "evidence_ref": evidence_ref},
                attempt_id=new_attempt_id,
            )
        return new_attempt_id

    # ------------------------------------------------------------------
    # capability discovery
    # ------------------------------------------------------------------
    def get_capabilities(self, session_id: str) -> dict[str, Any]:
        record = self._load_session(session_id)
        self.sessions.authorize(record, "get_capabilities")
        visible_actions = []
        unavailable = []
        for action in ("launch_candidate", "wait_task", "read_feedback"):
            if action in self.config.allowed_actions:
                visible_actions.append(
                    {
                        "name": action,
                        "version": "1",
                        "params": _ACTION_PARAMS[action],
                    }
                )
            else:
                unavailable.append({"name": action, "reason_code": "unsupported_capability"})
        return {
            "session_id": session_id,
            "sample_id": self.config.sample_id,
            "build_profile": self.config.build_profile,
            "feedback_profile": self.config.feedback_profile,
            "actions": visible_actions,
            "artifact_kinds": ["source_snapshot", "build_log", "candidate_apk"],
            "unavailable": unavailable,
            "read_limits": {
                "get_attempt_response_bytes_max": self.config.feedback_limits["get_attempt_response_bytes_max"],
                "get_attempt_calls_per_session": self.config.feedback_limits["status_call_limit"],
                "diagnostic_page_bytes_max": self.config.feedback_limits["diagnostic_page_bytes_max"],
                "diagnostic_pages_per_candidate": self.config.feedback_limits["diagnostic_pages_per_candidate"],
                "diagnostic_bytes_per_candidate": self.config.feedback_limits["diagnostic_bytes_per_candidate"],
                "diagnostic_bytes_per_session": self.config.feedback_limits["diagnostic_bytes_per_session"],
            },
        }

    # ------------------------------------------------------------------
    # session read
    # ------------------------------------------------------------------
    def get_session(self, session_id: str) -> dict[str, Any]:
        record = self._load_session(session_id)
        self.sessions.authorize(record, "get_session")
        row = self.store.query_one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        assert row is not None
        remaining = self._remaining_budget(session_id)
        return {
            "session_id": session_id,
            "state": row["state"],
            "can_continue": row["state"] == SESSION_ACTIVE and not self.sessions.wall_expired(session_id),
            "sample_id": row["sample_id"],
            "sample_version": row["sample_version"],
            "feedback_profile": row["feedback_profile"],
            "resource_roles": self.store.loads(row["resource_roles_json"], []),
            "budget_remaining": remaining,
            "active_attempt_id": row["active_attempt_id"],
            "finalized_candidate_id": row["finalized_candidate_id"],
            "finalized_build_id": row["finalized_build_id"],
            "status_version": int(row["status_version"]),
        }

    def _remaining_budget(self, session_id: str) -> dict[str, int | None]:
        snapshot = self.store.counter_snapshot(session_id)
        wall = self.sessions.wall_deadline_epoch(session_id)
        wall_remaining = None if wall is None else max(0, int(wall - self.clock.epoch()))

        def left(key: str) -> int | None:
            entry = snapshot.get(key)
            if entry is None or entry["limit"] is None:
                return None
            return max(0, int(entry["limit"]) - int(entry["consumed"]) - int(entry["reserved"]))

        return {
            "wall_seconds_remaining": wall_remaining,
            "distinct_candidates_remaining": left(C_DISTINCT_CANDIDATES),
            "builds_remaining": left(C_BUILDS),
            "effective_attempts_remaining": left(C_EFFECTIVE_ATTEMPTS),
            "infrastructure_retries_remaining": left(C_INFRA_RETRIES),
        }

    # ------------------------------------------------------------------
    # operations
    # ------------------------------------------------------------------
    def get_operation(self, session_id: str, operation_id: str) -> dict[str, Any]:
        record = self._load_session(session_id)
        self.sessions.authorize(record, "get_operation")
        row = self.store.query_one(
            "SELECT * FROM operations WHERE operation_id=? AND session_id=?", (operation_id, session_id)
        )
        if row is None:
            raise ProtocolError("not_found", detail="operation not found in this session")
        result = self.store.loads(row["result_json"], None)
        # Operation results are a visibility projection: no diagnostics body.
        return {
            "operation_id": row["operation_id"],
            "method": row["method"],
            "state": row["state"],
            "reason_code": row["reason_code"],
            "error": {"code": row["error_code"]} if row["error_code"] else None,
            "result": _project_operation_result(row["method"], result),
            "created_utc": row["created_utc"],
            "updated_utc": row["updated_utc"],
        }

    def cancel_operation(self, session_id: str, operation_id: str) -> dict[str, Any]:
        record = self._load_session(session_id)
        self.sessions.authorize(record, "cancel_operation")
        row = self.store.query_one(
            "SELECT * FROM operations WHERE operation_id=? AND session_id=?", (operation_id, session_id)
        )
        if row is None:
            raise ProtocolError("not_found", detail="operation not found in this session")
        if row["state"] in (OP_SUCCEEDED, OP_FAILED):
            return {"operation_id": operation_id, "cancel_requested": False, "state": row["state"]}
        with self.store.txn():
            self._operation_finish(operation_id, state="CANCELLED", reason_code="REQUEST_REJECTED")
            self.events.append(
                session_id,
                "operation_cancelled",
                payload={"operation_id": operation_id},
                attempt_id=row["attempt_id"],
            )
        return {"operation_id": operation_id, "cancel_requested": True, "state": "CANCELLED"}

    # ------------------------------------------------------------------
    # candidate submission
    # ------------------------------------------------------------------
    def submit_candidate(
        self,
        session_id: str,
        *,
        idempotency_key: str,
        files_payload: list[dict[str, Any]],
    ) -> dict[str, Any]:
        record = self._load_session(session_id)
        self.sessions.authorize(record, "submit_candidate")
        self._check_rate(session_id)
        if record.state != SESSION_ACTIVE:
            self._record_rejection(session_id, scope="submit_candidate", reason_code="REQUEST_REJECTED",
                                   error_code="permission_denied", detail="session not active")
            raise ProtocolError("permission_denied", detail="session not active")
        if record.finalized_candidate_id is not None:
            self._record_rejection(session_id, scope="submit_candidate", reason_code="REQUEST_REJECTED",
                                   error_code="permission_denied", detail="candidate already finalized")
            raise ProtocolError("permission_denied", detail="candidate already finalized")

        payload = {"session_id": session_id, "files": files_payload}
        rhash = request_hash("submit_candidate", payload)
        existing = self.idempotency.lookup(session_id, "submit_candidate", idempotency_key, rhash)
        if existing is not None:
            if existing["state"] != "completed":
                existing = self.idempotency.wait_for_completion(session_id, "submit_candidate", idempotency_key)
            return json.loads(existing["response_json"])

        files = _decode_files(files_payload, limits=self.config.limits)
        profile = get_profile(self.config.build_profile)
        policy = profile.validate_source(files, limits=self.config.limits)

        if not policy.accepted:
            # Execution-time (pre-acceptance) rejection: no budget is consumed.
            self._record_rejection(
                session_id,
                scope="submit_candidate",
                reason_code="SOURCE_POLICY_REJECTED",
                error_code="malformed_request",
                detail=f"{len(policy.errors)} source policy violation(s)",
            )
            self._store_policy_diagnostics(session_id, policy)
            response = {
                "operation_id": None,
                "candidate_id": None,
                "tree_hash": None,
                "file_count": 0,
                "source_bytes": 0,
                "status": "rejected",
                "reason_code": "SOURCE_POLICY_REJECTED",
                "diagnostics_available": True,
            }
            with self.store.txn():
                self.idempotency.claim(session_id, "submit_candidate", idempotency_key, rhash, now=self.clock.utc())
                self.idempotency.complete(
                    session_id,
                    "submit_candidate",
                    idempotency_key,
                    response=response,
                    http_status=200,
                    operation_id=None,
                    now=self.clock.utc(),
                )
            return response

        # Same frozen source tree hash: never a new candidate quota, and never a
        # fresh candidate_id that could be used to re-build the same source.
        duplicate = self.store.query_one(
            "SELECT candidate_id FROM tree_hashes WHERE session_id=? AND tree_hash=?",
            (session_id, policy.tree_hash),
        )
        if duplicate is not None:
            candidate_id = duplicate["candidate_id"]
            response = {
                "operation_id": None,
                "candidate_id": candidate_id,
                "tree_hash": policy.tree_hash,
                "file_count": len(policy.manifest),
                "source_bytes": policy.source_bytes,
                "status": "duplicate",
                "reason_code": None,
            }
            with self.store.txn():
                self.idempotency.claim(session_id, "submit_candidate", idempotency_key, rhash, now=self.clock.utc())
                self.idempotency.complete(
                    session_id, "submit_candidate", idempotency_key, response=response,
                    http_status=200, operation_id=None, now=self.clock.utc(),
                )
            return response

        reserved = [(C_DISTINCT_CANDIDATES, 1)]
        try:
            with self.store.txn():
                self.idempotency.claim_or_rollback(
                    session_id, "submit_candidate", idempotency_key, rhash, now=self.clock.utc()
                )
                self._reserve(
                    session_id, reserved, reason="submit_candidate", ref_type="tree_hash", ref_id=policy.tree_hash
                )
                candidate_id = new_id("candidate")
                self.store.execute(
                    """
                    INSERT INTO candidates(candidate_id, session_id, build_profile, tree_hash,
                                           file_manifest_json, manifest_sha256, path_policy_version,
                                           file_count, source_bytes, state, created_utc)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        candidate_id,
                        session_id,
                        self.config.build_profile,
                        policy.tree_hash,
                        self.store.dumps(policy.manifest),
                        policy.manifest_sha256,
                        profile.path_policy_version,
                        len(policy.manifest),
                        policy.source_bytes,
                        CANDIDATE_FROZEN,
                        self.clock.utc(),
                    ),
                )
                for ordinal, entry in enumerate(policy.manifest):
                    self.store.execute(
                        """
                        INSERT INTO candidate_files(candidate_id, relative_path, ordinal, size_bytes, sha256)
                        VALUES (?,?,?,?,?)
                        """,
                        (
                            candidate_id,
                            entry["relative_path"],
                            ordinal,
                            entry["size_bytes"],
                            entry["sha256"],
                        ),
                    )
                self.store.execute(
                    "INSERT INTO tree_hashes(session_id, tree_hash, candidate_id, first_seen_utc) VALUES (?,?,?,?)",
                    (session_id, policy.tree_hash, candidate_id, self.clock.utc()),
                )
                for key, limit in tree_counter_limits(self.config, policy.tree_hash).items():
                    self.store.counter_ensure(session_id, key, limit)

                snapshot = json.dumps(
                    {
                        "tree_hash": policy.tree_hash,
                        "manifest": policy.manifest,
                        "files": [
                            {
                                "relative_path": f.relative_path,
                                "content_b64": base64.b64encode(f.data).decode("ascii"),
                            }
                            for f in sorted(files, key=lambda x: x.relative_path)
                        ],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                snapshot_artifact = self.artifacts.put_in_txn(
                    session_id=session_id,
                    kind="source_snapshot",
                    media_type="application/json",
                    data=snapshot,
                    source="agent_submission",
                    provenance=SOURCE_BUILT,
                    candidate_id=candidate_id,
                )
                self._settle(
                    session_id,
                    reserved=reserved,
                    consumed=reserved,
                    reason="submit_candidate",
                    ref_type="candidate",
                    ref_id=candidate_id,
                )
                self.events.append(
                    session_id,
                    "candidate_frozen",
                    payload={
                        "tree_hash": policy.tree_hash,
                        "file_count": len(policy.manifest),
                        "source_bytes": policy.source_bytes,
                        "snapshot_artifact_id": snapshot_artifact,
                    },
                    candidate_id=candidate_id,
                )
                response = {
                    "operation_id": None,
                    "candidate_id": candidate_id,
                    "tree_hash": policy.tree_hash,
                    "file_count": len(policy.manifest),
                    "source_bytes": policy.source_bytes,
                    "status": "accepted",
                    "reason_code": None,
                    "snapshot_artifact_id": snapshot_artifact,
                }
                self.idempotency.complete(
                    session_id, "submit_candidate", idempotency_key, response=response,
                    http_status=200, operation_id=None, now=self.clock.utc(),
                )
        except DuplicateInFlight:
            existing = self.idempotency.wait_for_completion(session_id, "submit_candidate", idempotency_key)
            return json.loads(existing["response_json"])
        except BudgetExhausted as exc:
            # Exhausted candidate quota is a pre-execution rejection: no candidate
            # row, no budget consumed. It is still a *receipted* outcome, so it is
            # returned in this method's own response shape and committed with its
            # idempotency record. Letting it escape as a BusinessRejection would
            # hand the caller an ``attempt_id``-shaped body it never asked for and
            # leave the key replayable as an unreceipted rejection, which
            # duplicates a rejection-ledger row on every retry.
            return self._atomic_submit_rejection(
                session_id,
                idempotency_key=idempotency_key,
                rhash=rhash,
                detail=f"counter exhausted: {exc.counter_key}",
            )
        return response

    def _atomic_submit_rejection(
        self,
        session_id: str,
        *,
        idempotency_key: str,
        rhash: str,
        detail: str,
    ) -> dict[str, Any]:
        """Reject a candidate submission for exhausted budget, atomically.

        Same guarantee as :meth:`_atomic_build_rejection`: the rejection ledger
        entry and the replayable idempotency receipt commit together, no counter
        is charged, and a concurrent same-key caller replays the winner's receipt
        instead of adding a second ledger row.
        """

        response = {
            "operation_id": None,
            "candidate_id": None,
            "tree_hash": None,
            "file_count": 0,
            "source_bytes": 0,
            "status": "rejected",
            "reason_code": "BUDGET_EXHAUSTED",
            "diagnostics_available": False,
        }
        try:
            with self.store.txn():
                self.idempotency.claim_or_rollback(
                    session_id, "submit_candidate", idempotency_key, rhash, now=self.clock.utc()
                )
                self._record_rejection_in_txn(
                    session_id,
                    scope="submit_candidate",
                    reason_code="BUDGET_EXHAUSTED",
                    error_code="permission_denied",
                    detail=detail,
                )
                self.idempotency.complete(
                    session_id, "submit_candidate", idempotency_key, response=response,
                    http_status=200, operation_id=None, now=self.clock.utc(),
                )
        except DuplicateInFlight:
            existing = self.idempotency.wait_for_completion(
                session_id, "submit_candidate", idempotency_key
            )
            if existing["response_json"]:
                return json.loads(existing["response_json"])
        return response

    def _store_policy_diagnostics(self, session_id: str, policy: Any) -> None:
        if not policy.errors:
            return
        # Source-policy rejections have no frozen candidate yet, so they are
        # scoped to a synthetic per-session diagnostic bucket keyed by a
        # derivation of the rejected submission, never by absolute paths.
        pseudo_tree = hashlib.sha256(
            "\n".join(sorted(e.relative_path for e in policy.errors)).encode("utf-8")
        ).hexdigest()
        projection = self.projector.project_build_errors(
            candidate_id="",
            relative_root="",
            errors=policy.errors,
            limit=self.config.feedback_limits["diagnostics_per_page"],
        )
        with self.store.txn():
            for entry in projection.entries:
                self.store.execute(
                    """
                    INSERT INTO diagnostics(diagnostic_id, session_id, candidate_id, tree_hash, build_id,
                                            attempt_id, scope, kind, ordinal, payload_json, sanitized,
                                            incomplete, created_utc)
                    VALUES (?,?,?,?,NULL,NULL,'build',?,?,?,1,?,?)
                    """,
                    (
                        new_id("diagnostic"),
                        session_id,
                        "",
                        pseudo_tree,
                        entry.kind,
                        entry.ordinal,
                        self.store.dumps(entry.payload),
                        1 if projection.diagnostics_incomplete else 0,
                        self.clock.utc(),
                    ),
                )

    # ------------------------------------------------------------------
    # build
    # ------------------------------------------------------------------
    def build_candidate(
        self,
        session_id: str,
        *,
        idempotency_key: str,
        candidate_id: str,
    ) -> dict[str, Any]:
        record = self._load_session(session_id)
        self.sessions.authorize(record, "build_candidate")
        self._check_rate(session_id)
        if record.state != SESSION_ACTIVE:
            self._record_rejection(session_id, scope="build_candidate", reason_code="REQUEST_REJECTED",
                                   error_code="permission_denied", detail="session not active")
            raise ProtocolError("permission_denied", detail="session not active")
        if record.finalized_candidate_id is not None:
            raise ProtocolError("permission_denied", detail="candidate already finalized")

        candidate = self.store.query_one(
            "SELECT * FROM candidates WHERE candidate_id=? AND session_id=?", (candidate_id, session_id)
        )
        if candidate is None:
            raise ProtocolError("not_found", detail="candidate not found in this session")
        tree_hash = candidate["tree_hash"]

        payload = {"session_id": session_id, "candidate_id": candidate_id}
        rhash = request_hash("build_candidate", payload)
        existing = self.idempotency.lookup(session_id, "build_candidate", idempotency_key, rhash)
        if existing is not None:
            if existing["state"] != "completed":
                existing = self.idempotency.wait_for_completion(session_id, "build_candidate", idempotency_key)
            return json.loads(existing["response_json"])

        reserved = [(C_BUILDS, 1), (tree_builds(tree_hash), 1)]
        exhausted = next(
            (key for key, amount in reserved if not self.store.counter_available(session_id, key, amount)),
            None,
        )
        if exhausted is not None:
            # Budget-exhausted rejection. The rejection ledger entry, the
            # idempotency claim and the stored receipt are written in ONE
            # transaction: a crash cannot leave a rejection with no replayable
            # result (or vice versa), and concurrent calls with the same key each
            # get the same receipt without a second ledger row and without 500.
            return self._atomic_build_rejection(
                session_id,
                idempotency_key=idempotency_key,
                rhash=rhash,
                candidate_id=candidate_id,
                detail=f"counter exhausted: {exhausted}",
            )

        build_id = new_id("build")
        deadline = self._deadline_for("build")
        try:
            with self.store.txn():
                self.idempotency.claim_or_rollback(
                    session_id, "build_candidate", idempotency_key, rhash, now=self.clock.utc()
                )
                # Budget is reserved before any build side effect.
                self._reserve(
                    session_id, reserved, reason="build_candidate", ref_type="candidate", ref_id=candidate_id
                )
                operation_id = self._operation_create(
                    session_id,
                    "build_candidate",
                    state=OP_RUNNING,
                    candidate_id=candidate_id,
                    build_id=build_id,
                )
                self.store.execute(
                    """
                    INSERT INTO builds(build_id, session_id, candidate_id, tree_hash, state,
                                       application_id, build_provenance, attempt_deadline_utc, created_utc)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        build_id,
                        session_id,
                        candidate_id,
                        tree_hash,
                        BUILD_PENDING,
                        self._application_id(candidate_id),
                        SOURCE_BUILT,
                        deadline,
                        self.clock.utc(),
                    ),
                )
                self.events.append(
                    session_id,
                    "build_accepted",
                    payload={"build_id": build_id, "tree_hash": tree_hash},
                    candidate_id=candidate_id,
                    build_id=build_id,
                )
        except BudgetExhausted as exc:
            # The pre-check passed but the reserve lost a race with a concurrent
            # spender; the transaction rolled back, so nothing was charged.
            return self._build_rejection(
                session_id, idempotency_key, rhash, candidate_id, "BUDGET_EXHAUSTED",
                detail=str(exc),
            )
        except DuplicateInFlight:
            existing = self.idempotency.wait_for_completion(session_id, "build_candidate", idempotency_key)
            return json.loads(existing["response_json"])

        application_id = self._application_id(candidate_id)
        outcome = None
        try:
            files = self._load_source_files(session_id, candidate_id, tree_hash)
            from runner.adapters.base import BuildRequest

            request = BuildRequest(
                candidate_id=candidate_id,
                build_profile=self.config.build_profile,
                tree_hash=tree_hash,
                files=files,
                application_id=application_id,
                isolation_token=new_id("build"),
            )
            self.store.execute("UPDATE builds SET state=? WHERE build_id=?", (BUILD_RUNNING, build_id))
            outcome = self.build_backend.build(request, limits=self.config.limits)
        except StorageError as exc:
            outcome = None
            self._finalize_build_failure(
                session_id, build_id, candidate_id, tree_hash, reserved,
                reason_code="BUILD_FAILED", log_text="", errors=[],
                failure_json={"code": "source_snapshot_unavailable", "detail": str(exc)[:200]},
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                response_extra={"status": "failed"},
            )
            return self._build_response(build_id, session_id)
        except CapabilityError as exc:
            self._finalize_build_failure(
                session_id, build_id, candidate_id, tree_hash, reserved,
                reason_code="BUILD_FAILED", log_text="", errors=[],
                failure_json={"code": "build_backend_unavailable", "detail": str(exc)[:200]},
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                response_extra={"status": "failed"},
            )
            return self._build_response(build_id, session_id)
        except Exception as exc:
            # An unexpected backend/adapter fault is still an *accepted* build:
            # it keeps its row, its ledger entry and its consumed budget, and it
            # is recorded as a stable BUILD_FAILED rather than escaping as a bare
            # HTTP 500 with a dangling reservation that nothing would settle.
            self._finalize_build_failure(
                session_id, build_id, candidate_id, tree_hash, reserved,
                reason_code="BUILD_FAILED", log_text="", errors=[],
                failure_json={"code": "build_backend_error", "detail": type(exc).__name__},
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                response_extra={"status": "failed"},
            )
            return self._build_response(build_id, session_id)

        assert outcome is not None
        if not outcome.ok:
            self._finalize_build_failure(
                session_id, build_id, candidate_id, tree_hash, reserved,
                reason_code=outcome.failure_reason_code or "BUILD_FAILED",
                log_text=outcome.log_text,
                errors=outcome.errors,
                failure_json={
                    "code": "compile_failed",
                    "detail": (outcome.failure_reason_code or "BUILD_FAILED"),
                },
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                response_extra={"status": "failed"},
            )
            return self._build_response(build_id, session_id)

        with self.store.txn():
            log_artifact = self.artifacts.put_in_txn(
                session_id=session_id,
                kind="build_log",
                media_type="text/plain",
                data=(outcome.log_text or "").encode("utf-8"),
                source="build_backend",
                provenance="runner_generated",
                candidate_id=candidate_id,
                build_id=build_id,
            )
            apk_artifact = None
            if outcome.apk_bytes is not None:
                apk_artifact = self.artifacts.put_in_txn(
                    session_id=session_id,
                    kind="candidate_apk",
                    media_type="application/vnd.android.package-archive",
                    data=outcome.apk_bytes,
                    source="build_backend",
                    provenance="mock_stand_in_not_a_real_apk",
                    candidate_id=candidate_id,
                    build_id=build_id,
                    expected_sha256=outcome.apk_sha256,
                )
            self.store.execute(
                """
                UPDATE builds
                   SET state=?, template_id=?, template_sha256=?, toolchain_id=?, toolchain_fingerprint=?,
                       dependency_lock_sha256=?, signing_profile=?, environment_fingerprint=?, exit_code=?,
                       log_artifact_id=?, apk_artifact_id=?, apk_sha256=?, apk_size_bytes=?, package_name=?,
                       version_code=?, signer_fingerprint=?, manifest_sha256=?, permissions_json=?,
                       components_json=?, queries_json=?, entry_component=?, application_id=?,
                       completed_utc=?
                 WHERE build_id=?
                """,
                (
                    BUILD_SUCCEEDED,
                    outcome.template_id,
                    outcome.template_sha256,
                    outcome.toolchain_id,
                    outcome.toolchain_fingerprint,
                    outcome.dependency_lock_sha256,
                    outcome.signing_profile,
                    outcome.environment_fingerprint,
                    outcome.exit_code,
                    log_artifact,
                    apk_artifact,
                    outcome.apk_sha256,
                    len(outcome.apk_bytes or b""),
                    outcome.package_name,
                    outcome.version_code,
                    outcome.signer_fingerprint,
                    outcome.manifest_sha256,
                    self.store.dumps(outcome.permissions),
                    self.store.dumps(outcome.components),
                    self.store.dumps(outcome.queries),
                    outcome.entry_component,
                    outcome.application_id,
                    self.clock.utc(),
                    build_id,
                ),
            )
            self._settle(
                session_id,
                reserved=reserved,
                consumed=reserved,  # accepted build counts even if it later fails
                reason="build_candidate_succeeded",
                ref_type="build",
                ref_id=build_id,
            )
            self.events.append(
                session_id,
                "build_succeeded",
                payload={
                    "build_id": build_id,
                    "apk_sha256": outcome.apk_sha256,
                    "package_name": outcome.package_name,
                },
                candidate_id=candidate_id,
                build_id=build_id,
            )
            self._operation_finish(
                operation_id,
                state=OP_SUCCEEDED,
                result={"build_id": build_id, "state": BUILD_SUCCEEDED},
            )
            response = self._build_response(build_id, session_id)
            self.idempotency.complete(
                session_id, "build_candidate", idempotency_key, response=response,
                http_status=200, operation_id=operation_id, now=self.clock.utc(),
            )
        return response

    def _finalize_build_failure(
        self,
        session_id: str,
        build_id: str,
        candidate_id: str,
        tree_hash: str,
        reserved: Sequence[tuple[str, int]],
        *,
        reason_code: str,
        log_text: str,
        errors: Iterable[BuildError],
        failure_json: dict[str, Any],
        operation_id: str,
        idempotency_key: str,
        response_extra: dict[str, Any],
    ) -> None:
        """A build failure keeps its record and its consumed budget.

        No attempt row is created: a failed build must never be dressed up as an
        execution attempt.
        """

        projection = self.projector.project_build_errors(
            candidate_id=candidate_id,
            relative_root="",
            errors=list(errors),
            limit=self.config.feedback_limits["diagnostics_per_page"],
        )
        with self.store.txn():
            log_artifact = self.artifacts.put_in_txn(
                session_id=session_id,
                kind="build_log",
                media_type="text/plain",
                data=(log_text or "").encode("utf-8"),
                source="build_backend",
                provenance="runner_generated",
                candidate_id=candidate_id,
                build_id=build_id,
            )
            self.store.execute(
                "UPDATE builds SET state=?, exit_code=?, log_artifact_id=?, failure_json=?, completed_utc=? WHERE build_id=?",
                (BUILD_FAILED, 1, log_artifact, self.store.dumps(failure_json), self.clock.utc(), build_id),
            )
            for entry in projection.entries:
                self.store.execute(
                    """
                    INSERT INTO diagnostics(diagnostic_id, session_id, candidate_id, tree_hash, build_id,
                                            attempt_id, scope, kind, ordinal, payload_json, sanitized,
                                            incomplete, created_utc)
                    VALUES (?,?,?,?,?,NULL,'build',?,?,?,1,?,?)
                    """,
                    (
                        new_id("diagnostic"),
                        session_id,
                        candidate_id,
                        tree_hash,
                        build_id,
                        entry.kind,
                        entry.ordinal,
                        self.store.dumps(entry.payload),
                        1 if projection.diagnostics_incomplete else 0,
                        self.clock.utc(),
                    ),
                )
            # An accepted build that fails still settles its budget.
            self._settle(
                session_id,
                reserved=reserved,
                consumed=reserved,
                reason=f"build_candidate_failed:{reason_code}",
                ref_type="build",
                ref_id=build_id,
            )
            self.events.append(
                session_id,
                "build_failed",
                payload={"build_id": build_id, "reason_code": reason_code,
                         "diagnostic_count": len(projection.entries)},
                candidate_id=candidate_id,
                build_id=build_id,
            )
            self._operation_finish(
                operation_id,
                state=OP_FAILED,
                result={"build_id": build_id, "state": BUILD_FAILED, "reason_code": reason_code},
                reason_code=reason_code,
            )
            response = self._build_response(build_id, session_id)
            self.idempotency.complete(
                session_id, "build_candidate", idempotency_key, response=response,
                http_status=200, operation_id=operation_id, now=self.clock.utc(),
            )

    def _atomic_build_rejection(
        self,
        session_id: str,
        *,
        idempotency_key: str,
        rhash: str,
        candidate_id: str,
        detail: str,
    ) -> dict[str, Any]:
        """Reject a build for exhausted budget, atomically and idempotently.

        Guarantees, all inside one transaction:
          * no ``builds`` row is created and no counter is charged;
          * the rejection ledger entry and the replayable idempotency receipt
            are committed together, so there is no window in which one exists
            without the other;
          * a concurrent caller with the same key observes the winner's stored
            receipt (never a 500, never a second ledger row).

        Losing the claim race is resolved by waiting for the winner and
        replaying its response, which is the same receipt this call would have
        produced.
        """

        response = {
            "build_id": None,
            "candidate_id": candidate_id,
            "state": "REJECTED",
            "reason_code": "BUDGET_EXHAUSTED",
            "apk_artifact_id": None,
            "apk_sha256": None,
            "package_name": None,
            "entry_component": None,
            "build_provenance": None,
        }
        try:
            with self.store.txn():
                self.idempotency.claim_or_rollback(
                    session_id, "build_candidate", idempotency_key, rhash, now=self.clock.utc()
                )
                self._record_rejection_in_txn(
                    session_id,
                    scope="build_candidate",
                    reason_code="BUDGET_EXHAUSTED",
                    error_code="permission_denied",
                    detail=detail,
                )
                self.idempotency.complete(
                    session_id, "build_candidate", idempotency_key, response=response,
                    http_status=200, operation_id=None, now=self.clock.utc(),
                )
        except DuplicateInFlight:
            # Another caller owns this key and stores the same receipt.
            existing = self.idempotency.wait_for_completion(
                session_id, "build_candidate", idempotency_key
            )
            if existing["response_json"]:
                return json.loads(existing["response_json"])
            return response
        return response

    def _build_rejection(
        self,
        session_id: str,
        idempotency_key: str,
        rhash: str,
        candidate_id: str,
        reason_code: str,
        *,
        detail: str = "build not accepted",
    ) -> dict[str, Any]:
        """A pre-side-effect build rejection: no build row, no budget consumed.

        The rejection entry and the idempotency receipt are committed together so
        a crash cannot leave one without the other.
        """

        response = {
            "build_id": None,
            "candidate_id": candidate_id,
            "state": "REJECTED",
            "reason_code": reason_code,
            "apk_artifact_id": None,
            "apk_sha256": None,
            "package_name": None,
            "entry_component": None,
            "build_provenance": None,
        }
        try:
            with self.store.txn():
                self.idempotency.claim_or_rollback(
                    session_id, "build_candidate", idempotency_key, rhash, now=self.clock.utc()
                )
                self._record_rejection_in_txn(
                    session_id,
                    scope="build_candidate",
                    reason_code=reason_code,
                    error_code="permission_denied",
                    detail=detail,
                )
                self.idempotency.complete(
                    session_id, "build_candidate", idempotency_key, response=response,
                    http_status=200, operation_id=None, now=self.clock.utc(),
                )
        except DuplicateInFlight:
            existing = self.idempotency.wait_for_completion(
                session_id, "build_candidate", idempotency_key
            )
            if existing["response_json"]:
                return json.loads(existing["response_json"])
        return response

    def _build_response(self, build_id: str, session_id: str) -> dict[str, Any]:
        row = self.store.query_one("SELECT * FROM builds WHERE build_id=?", (build_id,))
        assert row is not None
        return {
            "build_id": build_id,
            "candidate_id": row["candidate_id"],
            "state": row["state"],
            "reason_code": _build_reason_code(row["state"], row["failure_json"]),
            "apk_artifact_id": row["apk_artifact_id"],
            "apk_sha256": row["apk_sha256"],
            "apk_size_bytes": row["apk_size_bytes"],
            "package_name": row["package_name"],
            "version_code": row["version_code"],
            "signer_fingerprint": row["signer_fingerprint"],
            "manifest_sha256": row["manifest_sha256"],
            "permissions": self.store.loads(row["permissions_json"], []),
            "components": self.store.loads(row["components_json"], []),
            "queries": self.store.loads(row["queries_json"], []),
            "entry_component": row["entry_component"],
            "build_provenance": row["build_provenance"],
            "template_id": row["template_id"],
            "toolchain_id": row["toolchain_id"],
            "log_artifact_id": row["log_artifact_id"],
        }

    def _application_id(self, candidate_id: str) -> str:
        """Controller-assigned, non-fixture namespace. Never Agent-controlled."""

        digest = hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()[:12]
        return f"org.androidpoc.candidate.c{digest}"

    def _load_source_files(self, session_id: str, candidate_id: str, tree_hash: str) -> list[SourceFile]:
        row = self.store.query_one(
            "SELECT artifact_id, content_sha256 FROM artifacts "
            "WHERE candidate_id=? AND kind='source_snapshot' AND state='sealed' "
            "ORDER BY created_utc DESC LIMIT 1",
            (candidate_id,),
        )
        if row is None:
            raise StorageError("source snapshot artifact is unavailable")
        raw = self.artifacts.read_object(row["artifact_id"])
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("tree_hash") != tree_hash:
            raise StorageError("source snapshot tree hash does not match the frozen candidate")
        files = [
            SourceFile(
                relative_path=entry["relative_path"],
                data=base64.b64decode(entry["content_b64"]),
            )
            for entry in payload.get("files", [])
        ]
        # Re-verify per-file digests against the frozen manifest.
        manifest = {m["relative_path"]: m["sha256"] for m in payload.get("manifest", [])}
        for source in files:
            if hashlib.sha256(source.data).hexdigest() != manifest.get(source.relative_path):
                raise StorageError("frozen source file failed hash verification")
        return files

    def _deadline_for(self, stage: str) -> str | None:
        seconds = self.config.timeouts.get(stage)
        if seconds is None:
            total = self.config.timeouts.get("attempt_total")
            if total is None:
                return None
            seconds = total
        return iso_from_epoch(self.clock.epoch() + float(seconds))

    # ------------------------------------------------------------------
    # attempt lifecycle
    # ------------------------------------------------------------------
    def begin_attempt(
        self,
        session_id: str,
        *,
        idempotency_key: str,
        build_id: str,
    ) -> dict[str, Any]:
        record = self._load_session(session_id)
        self.sessions.authorize(record, "begin_attempt")
        self._check_rate(session_id)

        payload = {"session_id": session_id, "build_id": build_id}
        rhash = request_hash("begin_attempt", payload)
        existing = self.idempotency.lookup(session_id, "begin_attempt", idempotency_key, rhash)
        if existing is not None:
            if existing["state"] != "completed":
                existing = self.idempotency.wait_for_completion(session_id, "begin_attempt", idempotency_key)
            return json.loads(existing["response_json"])

        if record.state != SESSION_ACTIVE:
            return self._begin_receipt(
                session_id, idempotency_key, rhash, status="rejected",
                attempt_id=None, reason_code="REQUEST_REJECTED",
                detail="session not active",
            )

        build = self.store.query_one(
            "SELECT * FROM builds WHERE build_id=? AND session_id=?", (build_id, session_id)
        )
        if build is None or build["state"] != BUILD_SUCCEEDED:
            return self._begin_receipt(
                session_id, idempotency_key, rhash, status="rejected",
                attempt_id=None, reason_code="BUILD_FAILED",
                detail="build is not a successful build of this session",
            )
        if build["build_provenance"] != SOURCE_BUILT:
            return self._begin_receipt(
                session_id, idempotency_key, rhash, status="rejected",
                attempt_id=None, reason_code="IDENTITY_CHECK_FAILED",
                detail="build provenance is not a verified source build",
            )

        open_attempt = self.store.query_one(
            "SELECT attempt_id FROM attempts WHERE session_id=? AND state<>'CLOSED'", (session_id,)
        )
        if open_attempt is not None:
            # Exactly one open attempt per session: a new key is rejected with a
            # stable reason code and never queued behind the active attempt.
            return self._begin_receipt(
                session_id, idempotency_key, rhash, status="rejected",
                attempt_id=None, reason_code="REQUEST_REJECTED",
                detail="an attempt is already open for this session",
            )

        tree_hash = build["tree_hash"]
        reserved = [(C_EFFECTIVE_ATTEMPTS, 1), (tree_runs(tree_hash), 1)]

        # Claim, reserve, create the attempt row and settle in ONE transaction.
        # A concurrent duplicate aborts this whole block via DuplicateInFlight,
        # so the attempt side effect happens exactly once.
        try:
            with self.store.txn():
                self.idempotency.claim_or_rollback(
                    session_id, "begin_attempt", idempotency_key, rhash, now=self.clock.utc()
                )
                self._reserve(
                    session_id, reserved, reason="begin_attempt", ref_type="build", ref_id=build_id
                )
                attempt_id = self._create_attempt_row(
                    session_id=session_id,
                    candidate_id=build["candidate_id"],
                    build_id=build_id,
                    effective=True,
                )
                self._settle(
                    session_id,
                    reserved=reserved,
                    consumed=reserved,
                    reason="begin_attempt",
                    ref_type="attempt",
                    ref_id=attempt_id,
                )
                self.events.append(
                    session_id,
                    "attempt_accepted",
                    payload={
                        "attempt_no": self.store.query_one(
                            "SELECT attempt_no FROM attempts WHERE attempt_id=?", (attempt_id,)
                        )["attempt_no"],
                        "build_id": build_id,
                    },
                    candidate_id=build["candidate_id"],
                    build_id=build_id,
                    attempt_id=attempt_id,
                )
                response = {"status": "accepted", "attempt_id": attempt_id, "reason_code": None}
                self.idempotency.complete(
                    session_id, "begin_attempt", idempotency_key, response=response,
                    http_status=200, operation_id=None, now=self.clock.utc(),
                )
        except BudgetExhausted as exc:
            self._record_rejection(
                session_id, scope="begin_attempt", reason_code="BUDGET_EXHAUSTED",
                error_code="permission_denied", detail=str(exc),
            )
            response = {
                "status": "rejected",
                "attempt_id": None,
                "reason_code": "BUDGET_EXHAUSTED",
            }
            try:
                with self.store.txn():
                    # ``claim`` must be honoured, not ignored: by the time this
                    # branch runs another caller with the same key may already
                    # have stored a winning "accepted" receipt. Overwriting it
                    # would report a rejection for an attempt that really exists.
                    won, existing = self.idempotency.claim(
                        session_id, "begin_attempt", idempotency_key, rhash, now=self.clock.utc()
                    )
                    if not won and existing is not None and existing["response_json"]:
                        return json.loads(existing["response_json"])
                    self.idempotency.complete(
                        session_id, "begin_attempt", idempotency_key, response=response,
                        http_status=200, operation_id=None, now=self.clock.utc(),
                    )
            except DuplicateInFlight:  # pragma: no cover - claim does not raise it
                existing = self.idempotency.wait_for_completion(
                    session_id, "begin_attempt", idempotency_key
                )
                return json.loads(existing["response_json"])
            return response
        except DuplicateInFlight:
            existing = self.idempotency.wait_for_completion(session_id, "begin_attempt", idempotency_key)
            return json.loads(existing["response_json"])

        self._prepare_attempt(attempt_id)
        return response

    def _begin_receipt(
        self,
        session_id: str,
        idempotency_key: str,
        rhash: str,
        *,
        status: str,
        attempt_id: str | None,
        reason_code: str | None,
        detail: str,
    ) -> dict[str, Any]:
        """Store and return the restricted receipt.

        Agent-visible business fields are only status / attempt_id /
        reason_code; nothing else is added for rejected or duplicate receipts.
        """

        self._record_rejection(
            session_id, scope="begin_attempt", reason_code=reason_code,
            error_code="permission_denied", detail=detail,
        )
        response = {"status": status, "attempt_id": attempt_id, "reason_code": reason_code}
        try:
            with self.store.txn():
                self.idempotency.claim_or_rollback(
                    session_id, "begin_attempt", idempotency_key, rhash, now=self.clock.utc()
                )
                self.idempotency.complete(
                    session_id, "begin_attempt", idempotency_key, response=response,
                    http_status=200, operation_id=None, now=self.clock.utc(),
                )
        except DuplicateInFlight:
            existing = self.idempotency.wait_for_completion(session_id, "begin_attempt", idempotency_key)
            return json.loads(existing["response_json"])
        return response

    def _prepare_attempt(self, attempt_id: str) -> None:
        """Baseline verify -> fixture install -> candidate install -> verify -> collectors."""

        attempt = self.runtime.load(attempt_id)
        session_id = attempt["session_id"]
        candidate_id = attempt["candidate_id"]
        self.runtime.transition(attempt_id, ATTEMPT_PREPARING)
        build = self.store.query_one("SELECT * FROM builds WHERE build_id=?", (attempt["build_id"],))
        assert build is not None

        def fail(stage: str, reason_code: str, execution_status: str) -> None:
            self.runtime.set_stage(attempt_id, stage, "failed")
            self.runtime.update(
                attempt_id, reason_code=reason_code, execution_status=execution_status,
                recovery_status="not_required",
            )
            self.runtime.set_stage(attempt_id, "cleanup", "skipped")
            self._request_finalize(attempt_id, reason_code=reason_code)

        # intake / build summary stages
        self.runtime.set_stage(attempt_id, "intake", "completed")
        self.runtime.set_stage(attempt_id, "build", "completed")

        # 1) fixed-image cold boot + baseline verification
        try:
            info = self.device.reset_baseline("target")
        except Exception:
            fail("install", "DEVICE_UNAVAILABLE", "setup_failed")
            return
        if info.health != "ready":
            self.runtime.update(attempt_id, device_health=info.health)
            self._quarantine()
            fail("install", "DEVICE_UNAVAILABLE", "setup_failed")
            return
        self.runtime.update(
            attempt_id, device_id=info.device_id, device_boot_id=info.boot_id,
            device_health=info.health, status_version=1,
        )
        self.runtime.set_stage(attempt_id, "install", "running")

        # 2) install fixed fixtures, then the candidate
        try:
            self.device.install_fixture(
                "target", "fixed_fixture", self._fixture_package(), b"mock-fixture-apk"
            )
        except Exception:
            fail("install", "INSTALL_FAILED", "install_failed")
            return
        apk_row = self.store.query_one(
            "SELECT artifact_id, content_sha256 FROM artifacts WHERE artifact_id=?",
            (build["apk_artifact_id"],),
        )
        if apk_row is None:
            fail("install", "IDENTITY_CHECK_FAILED", "install_failed")
            return
        apk_bytes = self.artifacts.read_object(apk_row["artifact_id"])
        try:
            install = self.device.install_candidate("target", build["package_name"], apk_bytes)
        except Exception:
            fail("install", "INSTALL_FAILED", "install_failed")
            return
        if not install.ok:
            fail("install", install.reason_code or "INSTALL_FAILED", "install_failed")
            return
        self.runtime.set_stage(attempt_id, "install", "completed")

        # 3) PackageManager identity re-check against the build manifest
        self.runtime.set_stage(attempt_id, "permission_check", "running")
        try:
            verified = self.device.verify_install(
                "target",
                build["package_name"],
                {
                    "apk_sha256": build["apk_sha256"],
                    "version_code": build["version_code"],
                    "signer_fingerprint": build["signer_fingerprint"],
                    "permissions": self.store.loads(build["permissions_json"], []),
                },
            )
        except Exception:
            fail("permission_check", "IDENTITY_CHECK_FAILED", "setup_failed")
            return
        if not verified.ok:
            # Installed identity or permission set differs from the build
            # manifest: the candidate must not be launched.
            self.runtime.set_stage(attempt_id, "permission_check", "failed")
            self.runtime.update(
                attempt_id,
                reason_code=verified.reason_code or "IDENTITY_CHECK_FAILED",
                execution_status="setup_failed",
            )
            self.runtime.set_stage(attempt_id, "cleanup", "skipped")
            self._request_finalize(attempt_id, reason_code=verified.reason_code or "IDENTITY_CHECK_FAILED")
            return
        self.runtime.update(attempt_id, status_version=1)
        self.runtime.set_stage(attempt_id, "permission_check", "completed")

        # 4) collectors must be ready BEFORE any trigger is possible
        required = self.config.collectors_required
        collectors = self._make_collectors(attempt_id, build["package_name"], verified.actual_uid or 0)
        ready = self.device.start_collectors("target", required)
        if not ready.get("ready"):
            self.runtime.set_stage(attempt_id, "launch", "failed")
            fail("observe", "EVIDENCE_COLLECTION_INCOMPLETE", "setup_failed")
            return
        scratch = self._scratch_dir(attempt_id)
        for name, collector in collectors.items():
            collector.bind_stream(os.path.join(scratch, f"{name}.ndjson"))
            handle = collector.start(attempt_id=attempt_id, device=info, session_id=session_id)
            if not handle.ready and collector.required_by_default:
                self.runtime.set_stage(attempt_id, "observe", "failed")
                fail("observe", "EVIDENCE_COLLECTION_INCOMPLETE", "setup_failed")
                return
            with self.store.txn():
                self.store.execute(
                    """
                    INSERT INTO tasks(task_id, attempt_id, session_id, role, name, state, readiness,
                                      required, dependencies_json, deadline_utc, handle, evidence_refs_json,
                                      adapter_version, created_utc, updated_utc)
                    VALUES (?,?,?, 'collector', ?, ?, ?, ?, '[]', ?, ?, '[]', ?, ?, ?)
                    """,
                    (
                        handle.task_id or f"{name}:{attempt_id}",
                        attempt_id,
                        session_id,
                        name,
                        TASK_RUNNING if handle.ready else TASK_EXITED,
                        handle.readiness,
                        1 if collector.required_by_default else 0,
                        self._deadline_for("attempt_total"),
                        self.store.dumps(collector.state_json()),
                        collector.version,
                        self.clock.utc(),
                        self.clock.utc(),
                    ),
                )
            self._collector_instances.setdefault(attempt_id, {})[name] = collector

        self.runtime.set_stage(attempt_id, "observe", "pending")
        self.runtime.set_stage(attempt_id, "launch", "pending")
        self.runtime.transition(attempt_id, ATTEMPT_RUNNING)
        self.runtime.update(attempt_id, launch_deadline_utc=self._deadline_for("attempt_total"))
        with self.store.txn():
            self.events.append(
                session_id,
                "attempt_prepared",
                payload={"device_boot_id": info.boot_id, "candidate_uid": verified.actual_uid},
                candidate_id=candidate_id,
                attempt_id=attempt_id,
            )

    def _fixture_package(self) -> str:
        return "org.androidpoc.fixture.baseline"

    def _make_collectors(self, attempt_id: str, package_name: str, uid: int) -> dict[str, Any]:
        existing = self._collector_instances.get(attempt_id)
        if existing:
            return existing
        if self.collector_factory is not None:
            return self.collector_factory(
                required=self.config.collectors_required,
                optional=self.config.collectors_optional,
                candidate_package=package_name,
                candidate_uid=uid,
            )
        from runner.adapters.collectors_mock import build_mock_collector_set

        return build_mock_collector_set(
            required=self.config.collectors_required,
            optional=self.config.collectors_optional,
            candidate_package=package_name,
            candidate_uid=uid,
        )

    def _quarantine(self) -> None:
        try:
            self.device.quarantine()
        except Exception:
            pass

    def _request_finalize(self, attempt_id: str, *, reason_code: str | None = None) -> None:
        """Mark the attempt as ready for finalization; the supervisor drains it."""

        attempt = self.runtime.load(attempt_id)
        if attempt["state"] == ATTEMPT_CLOSED:
            return
        if reason_code and not attempt["reason_code"]:
            self.runtime.update(attempt_id, reason_code=reason_code, bump_version=False)
        self.runtime.transition(attempt_id, ATTEMPT_FINALIZING)

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def perform_action(
        self,
        session_id: str,
        attempt_id: str,
        *,
        idempotency_key: str,
        action_name: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = self._load_session(session_id)
        self.sessions.authorize(record, "perform_action")
        self._check_rate(session_id)

        if action_name not in self.config.allowed_actions:
            self._record_rejection(
                session_id, scope="perform_action", reason_code="REQUEST_REJECTED",
                error_code="unsupported_capability", detail="action not in sample whitelist",
            )
            raise ProtocolError("unsupported_capability", detail="action not authorized for this sample")
        if action_name != "launch_candidate":
            # There is no generic launch/install/intent/service entry point.
            raise ProtocolError("unsupported_capability", detail="only launch_candidate is implemented")
        _validate_launch_params(params or {})

        attempt = self.runtime.load(attempt_id)
        if attempt["session_id"] != session_id:
            raise ProtocolError("not_found", detail="attempt not found in this session")

        payload = {"session_id": session_id, "attempt_id": attempt_id, "action_name": action_name,
                   "params": params or {}}
        rhash = request_hash("perform_action", payload)
        existing = self.idempotency.lookup(session_id, f"perform_action:{action_name}", idempotency_key, rhash)
        if existing is not None:
            if existing["state"] != "completed":
                existing = self.idempotency.wait_for_completion(
                    session_id, f"perform_action:{action_name}", idempotency_key
                )
            return json.loads(existing["response_json"])

        if attempt["state"] != ATTEMPT_RUNNING:
            return self._action_receipt(
                session_id, attempt_id, action_name, idempotency_key, rhash,
                status="rejected", reason_code="REQUEST_REJECTED",
                detail="attempt is not in RUNNING state",
            )
        max_launches = int(self.config.max_launches_per_attempt)
        if attempt["launch_count"] >= max_launches:
            # A second launch with a new key is rejected; the same key returns
            # the original result above. Unknown launch results are never
            # re-triggered automatically.
            return self._action_receipt(
                session_id, attempt_id, action_name, idempotency_key, rhash,
                status="rejected", reason_code="REQUEST_REJECTED",
                detail="this attempt has already used its launch allowance",
            )

        action_id = new_id("action")
        reserved = [(C_ACTIONS, 1)]
        scope = f"perform_action:{action_name}"
        try:
            with self.store.txn():
                self.idempotency.claim_or_rollback(
                    session_id, scope, idempotency_key, rhash, now=self.clock.utc()
                )
                self._reserve(
                    session_id, reserved, reason="perform_action", ref_type="attempt", ref_id=attempt_id
                )
                operation_id = self._operation_create(
                    session_id, "perform_action", state=OP_RUNNING, attempt_id=attempt_id
                )
                self.store.execute(
                    """
                    INSERT INTO attempt_actions(action_id, attempt_id, session_id, action_name, params_json,
                                                params_sha256, state, operation_id, created_utc, updated_utc)
                    VALUES (?,?,?,?,?,?, 'RUNNING', ?, ?, ?)
                    """,
                    (
                        action_id,
                        attempt_id,
                        session_id,
                        action_name,
                        self.store.dumps(params or {}),
                        hashlib.sha256(
                            json.dumps(params or {}, sort_keys=True).encode("utf-8")
                        ).hexdigest(),
                        operation_id,
                        self.clock.utc(),
                        self.clock.utc(),
                    ),
                )
                # The launch allowance is consumed in the same transaction that
                # claims the key, so two keys cannot both launch.
                self.store.execute(
                    "UPDATE attempts SET launch_count = launch_count + 1, "
                    "status_version = status_version + 1 WHERE attempt_id=?",
                    (attempt_id,),
                )
                self._settle(
                    session_id, reserved=reserved, consumed=reserved,
                    reason="perform_action", ref_type="action", ref_id=action_id,
                )
                self.events.append(
                    session_id, "action_accepted",
                    payload={"action_name": action_name},
                    attempt_id=attempt_id, action_id=action_id,
                )
        except DuplicateInFlight:
            existing = self.idempotency.wait_for_completion(session_id, scope, idempotency_key)
            return json.loads(existing["response_json"])

        result = self._execute_launch(session_id, attempt_id, action_id, operation_id)
        response = {
            "action_id": action_id,
            "action_name": action_name,
            "state": result["state"],
            "operation_id": operation_id,
            "reason_code": result["reason_code"],
        }
        with self.store.txn():
            self._operation_finish(
                operation_id,
                state=OP_SUCCEEDED if result["state"] == "accepted" else OP_FAILED,
                result={"action_id": action_id, "state": result["state"]},
                reason_code=result["reason_code"],
            )
            self.idempotency.complete(
                session_id, f"perform_action:{action_name}", idempotency_key, response=response,
                http_status=200, operation_id=operation_id, now=self.clock.utc(),
            )
        return response

    def _action_receipt(
        self,
        session_id: str,
        attempt_id: str,
        action_name: str,
        idempotency_key: str,
        rhash: str,
        *,
        status: str,
        reason_code: str,
        detail: str,
    ) -> dict[str, Any]:
        self._record_rejection(
            session_id, scope=f"perform_action:{action_name}", reason_code=reason_code,
            error_code="permission_denied", detail=detail,
        )
        response = {
            "action_id": None,
            "action_name": action_name,
            "state": "rejected",
            "operation_id": None,
            "reason_code": reason_code,
        }
        scope = f"perform_action:{action_name}"
        try:
            with self.store.txn():
                self.idempotency.claim_or_rollback(
                    session_id, scope, idempotency_key, rhash, now=self.clock.utc()
                )
                self.idempotency.complete(
                    session_id, scope, idempotency_key, response=response,
                    http_status=200, operation_id=None, now=self.clock.utc(),
                )
        except DuplicateInFlight:
            existing = self.idempotency.wait_for_completion(session_id, scope, idempotency_key)
            return json.loads(existing["response_json"])
        return response

    def _execute_launch(
        self, session_id: str, attempt_id: str, action_id: str, operation_id: str
    ) -> dict[str, Any]:
        """Dispatch, then require trusted confirmation within the limit.

        The observation window opens only on trusted confirmation of an actual
        entry-Activity start; it is never opened from the command's exit code.
        """

        attempt = self.runtime.load(attempt_id)
        build = self.store.query_one("SELECT * FROM builds WHERE build_id=?", (attempt["build_id"],))
        assert build is not None
        window_seconds = float(self.config.timeouts.get("observation_window") or 15.0)
        confirm_limit = float(self.config.timeouts.get("launch_confirmation") or 10.0)

        self.runtime.set_stage(attempt_id, "launch", "running")
        # launch-phase evidence is emitted before the command is dispatched.
        self._emit_phase(attempt_id, "launch")

        dispatch = self.device.launch_candidate(
            "target", build["package_name"], build["entry_component"]
        )
        dispatch_monotonic = self.clock.monotonic()
        dispatch_utc = self.clock.utc()
        self.runtime.update(
            attempt_id,
            launch_dispatch_utc=dispatch_utc,
            launch_dispatch_monotonic=dispatch_monotonic,
            launch_deadline_utc=iso_from_epoch(self.clock.epoch() + confirm_limit),
        )
        with self.store.txn():
            self.events.append(
                session_id, "launch_dispatched",
                payload={"entry_component_class": "template_launcher", "command_token": dispatch.command_token},
                candidate_id=attempt["candidate_id"], attempt_id=attempt_id, action_id=action_id,
            )

        confirmation = self.device.confirm_launch(
            "target",
            dispatch,
            deadline_monotonic=dispatch_monotonic + confirm_limit,
            now_monotonic=self.clock.monotonic(),
        )
        if not confirmation.confirmed:
            self.runtime.set_stage(attempt_id, "launch", "failed")
            self.runtime.set_stage(attempt_id, "observe", "skipped")
            self.runtime.update(
                attempt_id,
                reason_code="LAUNCH_TIMEOUT",
                execution_status="setup_failed",
                observation_window_complete=0,
            )
            with self.store.txn():
                self.events.append(
                    session_id, "launch_unconfirmed",
                    payload={"reason_code": "LAUNCH_TIMEOUT"}, attempt_id=attempt_id,
                )
            self._request_finalize(attempt_id, reason_code="LAUNCH_TIMEOUT")
            return {"state": "accepted", "reason_code": "LAUNCH_TIMEOUT"}

        confirmed_monotonic = confirmation.confirmed_monotonic or dispatch_monotonic
        deadline_monotonic = confirmed_monotonic + window_seconds
        boot_anchor = self.store.boot_anchor()
        self.runtime.update(
            attempt_id,
            launch_confirmed_monotonic=confirmed_monotonic,
            launch_confirmed_utc=self.clock.utc(),
            observation_deadline_monotonic=deadline_monotonic,
            observation_deadline_utc=iso_from_epoch(
                self.clock.epoch() + max(0.0, deadline_monotonic - self.clock.monotonic())
            ),
            # Record which host boot produced this monotonic deadline so a later
            # process can tell whether it is still comparable.
            monotonic_boot_id=self.store.host_boot.get("boot_id"),
            monotonic_boot_anchor=boot_anchor,
            finalize_deadline_utc=self._deadline_for("evidence_seal"),
        )
        self.runtime.set_stage(attempt_id, "launch", "completed")
        self.runtime.set_stage(attempt_id, "observe", "running")
        # Observation-window evidence begins only after confirmation.
        self._emit_phase(attempt_id, "observe")
        with self.store.txn():
            self.events.append(
                session_id, "launch_confirmed",
                payload={"window_seconds": window_seconds},
                candidate_id=attempt["candidate_id"], attempt_id=attempt_id, action_id=action_id,
            )
        return {"state": "accepted", "reason_code": None}

    def _emit_phase(self, attempt_id: str, phase: str) -> None:
        collectors = self._collector_instances.get(attempt_id, {})
        for collector in collectors.values():
            try:
                end = collector.emit_phase(phase)
            except Exception:
                continue
            handle = collector.state_json()
            self.store.execute(
                "UPDATE tasks SET handle=?, updated_utc=? WHERE task_id=?",
                (self.store.dumps(handle), self.clock.utc(), collector.handle_id),
            )

    # ------------------------------------------------------------------
    # attempt read / finalize / close
    # ------------------------------------------------------------------
    def get_attempt(self, session_id: str, attempt_id: str) -> dict[str, Any]:
        record = self._load_session(session_id)
        self.sessions.authorize(record, "get_attempt")
        self._check_rate(session_id)
        attempt = self.runtime.find(attempt_id)
        if attempt is None or attempt["session_id"] != session_id:
            raise ProtocolError("not_found", detail="attempt not found in this session")
        return self._attempt_status_page(session_id, attempt)

    def _attempt_status_page(self, session_id: str, attempt: dict[str, Any]) -> dict[str, Any]:
        """Compact status page: seven stages, health, remaining budget, metadata.

        Contains no diagnostics, no log text and never any Oracle field.
        """

        health = attempt["device_health"]
        if health not in DEVICE_HEALTH:
            health = "unknown"
        return {
            "attempt_id": attempt["attempt_id"],
            "attempt_no": attempt["attempt_no"],
            "state": attempt["state"],
            "status_version": attempt["status_version"],
            "stages": {name: attempt["stages"].get(name, "pending") for name in (
                "intake", "build", "install", "permission_check", "launch", "observe", "cleanup"
            )},
            "device_health": health,
            "remaining_budget": self._remaining_budget(session_id),
            "reason_code": attempt["reason_code"],
            "truncated": False,
        }

    def list_attempts(self, session_id: str) -> dict[str, Any]:
        record = self._load_session(session_id)
        self.sessions.authorize(record, "get_attempt")
        rows = self.store.query_all(
            "SELECT attempt_id, attempt_no, state, status_version, device_health, created_utc "
            "FROM attempts WHERE session_id=? ORDER BY attempt_no",
            (session_id,),
        )
        return {
            "attempts": [
                {
                    "attempt_id": row["attempt_id"],
                    "attempt_no": row["attempt_no"],
                    "state": row["state"],
                    "status_version": row["status_version"],
                    "device_health": row["device_health"],
                }
                for row in rows
            ]
        }

    def finish_attempt(self, session_id: str, attempt_id: str) -> dict[str, Any]:
        record = self._load_session(session_id)
        self.sessions.authorize(record, "finish_attempt")
        attempt = self.runtime.load(attempt_id)
        if attempt["session_id"] != session_id:
            raise ProtocolError("not_found", detail="attempt not found in this session")
        if self.config.auto_finalize_after_observation and attempt["observation_deadline_monotonic"]:
            if self.clock.monotonic() < float(attempt["observation_deadline_monotonic"]):
                # The Agent cannot shorten a normal observation window.
                self._record_rejection(
                    session_id, scope="finish_attempt", reason_code="REQUEST_REJECTED",
                    error_code="permission_denied", detail="observation window still active",
                )
                return {"attempt_id": attempt_id, "state": attempt["state"], "accepted": False,
                        "reason_code": "REQUEST_REJECTED"}
        self._request_finalize(attempt_id)
        return {"attempt_id": attempt_id, "state": self.runtime.load(attempt_id)["state"], "accepted": True,
                "reason_code": None}

    def abort_attempt(self, session_id: str, attempt_id: str) -> dict[str, Any]:
        """Abort path: finalize the attempt and record an incomplete window.

        Aborting never claims a completed observation window, and any prefix of
        execution evidence collected so far is still sealed before the candidate
        is stopped.
        """

        record = self._load_session(session_id)
        self.sessions.authorize(record, "abort_attempt")
        attempt = self.runtime.find(attempt_id)
        if attempt is None or attempt["session_id"] != session_id:
            raise ProtocolError("not_found", detail="attempt not found in this session")
        if attempt["state"] == ATTEMPT_CLOSED:
            return {
                "attempt_id": attempt_id,
                "state": ATTEMPT_CLOSED,
                "observation_window_complete": bool(attempt["observation_window_complete"]),
            }
        with self.store.txn():
            self.events.append(
                session_id,
                "attempt_aborted",
                payload={"observation_window_complete": False},
                attempt_id=attempt_id,
            )
            self.runtime.update(attempt_id, observation_window_complete=0, bump_version=False)
        self._request_finalize(attempt_id, reason_code=attempt["reason_code"] or "REQUEST_REJECTED")
        return {
            "attempt_id": attempt_id,
            "state": self.runtime.load(attempt_id)["state"],
            "observation_window_complete": False,
        }

    def finalize_candidate(
        self, session_id: str, *, idempotency_key: str, candidate_id: str, build_id: str
    ) -> dict[str, Any]:
        """The Agent's explicit single final choice. Never auto-selected."""

        record = self._load_session(session_id)
        self.sessions.authorize(record, "finalize_candidate")
        self._check_rate(session_id)
        payload = {"session_id": session_id, "candidate_id": candidate_id, "build_id": build_id}
        rhash = request_hash("finalize_candidate", payload)
        existing = self.idempotency.lookup(session_id, "finalize_candidate", idempotency_key, rhash)
        if existing is not None:
            if existing["state"] != "completed":
                existing = self.idempotency.wait_for_completion(
                    session_id, "finalize_candidate", idempotency_key
                )
            return json.loads(existing["response_json"])

        if record.finalized_candidate_id is not None:
            if record.finalized_candidate_id == candidate_id:
                response = {
                    "finalized_candidate_id": candidate_id,
                    "build_id": build_id,
                    "recipe_hash": None,
                    "state": "already_finalized",
                }
                with self.store.txn():
                    self.idempotency.claim(
                        session_id, "finalize_candidate", idempotency_key, rhash, now=self.clock.utc()
                    )
                    self.idempotency.complete(
                        session_id, "finalize_candidate", idempotency_key, response=response,
                        http_status=200, operation_id=None, now=self.clock.utc(),
                    )
                return response
            self._record_rejection(
                session_id, scope="finalize_candidate", reason_code="REQUEST_REJECTED",
                error_code="permission_denied", detail="a different candidate is already final",
            )
            raise ProtocolError("permission_denied", detail="a different candidate is already final")

        build = self.store.query_one(
            "SELECT * FROM builds WHERE build_id=? AND session_id=? AND state=?",
            (build_id, session_id, BUILD_SUCCEEDED),
        )
        if build is None or build["candidate_id"] != candidate_id:
            self._record_rejection(
                session_id, scope="finalize_candidate", reason_code="BUILD_FAILED",
                error_code="malformed_request", detail="build is not a successful build of this candidate",
            )
            raise ProtocolError("malformed_request", detail="build is not a successful build of this candidate")

        recipe_hash = _recipe_hash(self.store, session_id, build_id, candidate_id, self.config)
        with self.store.txn():
            self.idempotency.claim(
                session_id, "finalize_candidate", idempotency_key, rhash, now=self.clock.utc()
            )
            self.sessions.finalize_candidate(
                session_id, candidate_id=candidate_id, build_id=build_id, recipe_hash=recipe_hash
            )
            self.events.append(
                session_id, "candidate_finalized",
                payload={"build_id": build_id, "recipe_hash": recipe_hash},
                candidate_id=candidate_id, build_id=build_id,
            )
            response = {
                "finalized_candidate_id": candidate_id,
                "build_id": build_id,
                "recipe_hash": recipe_hash,
                "state": "finalized",
            }
            self.idempotency.complete(
                session_id, "finalize_candidate", idempotency_key, response=response,
                http_status=200, operation_id=None, now=self.clock.utc(),
            )
        return response

    def close_session(self, session_id: str, *, idempotency_key: str, reason: str = "agent_requested") -> dict[str, Any]:
        record = self._load_session(session_id)
        self.sessions.authorize(record, "close_session")
        payload = {"session_id": session_id, "reason": reason}
        rhash = request_hash("close_session", payload)
        existing = self.idempotency.lookup(session_id, "close_session", idempotency_key, rhash)
        if existing is not None:
            if existing["state"] != "completed":
                existing = self.idempotency.wait_for_completion(session_id, "close_session", idempotency_key)
            return json.loads(existing["response_json"])
        self.sessions.set_closing(session_id, reason)
        response = {"session_id": session_id, "state": SESSION_CLOSING, "close_reason": reason}
        with self.store.txn():
            self.idempotency.claim(session_id, "close_session", idempotency_key, rhash, now=self.clock.utc())
            self.idempotency.complete(
                session_id, "close_session", idempotency_key, response=response,
                http_status=200, operation_id=None, now=self.clock.utc(),
            )
        return response

    # ------------------------------------------------------------------
    # tasks
    # ------------------------------------------------------------------
    def list_tasks(self, session_id: str, attempt_id: str) -> dict[str, Any]:
        record = self._load_session(session_id)
        self.sessions.authorize(record, "list_tasks")
        attempt = self.runtime.load(attempt_id)
        if attempt["session_id"] != session_id:
            raise ProtocolError("not_found", detail="attempt not found in this session")
        rows = self.store.query_all(
            "SELECT * FROM tasks WHERE attempt_id=? ORDER BY created_utc, task_id", (attempt_id,)
        )
        return {"tasks": [self._task_projection(row) for row in rows]}

    def get_task(self, session_id: str, task_id: str) -> dict[str, Any]:
        record = self._load_session(session_id)
        self.sessions.authorize(record, "get_task")
        row = self.store.query_one("SELECT * FROM tasks WHERE task_id=? AND session_id=?", (task_id, session_id))
        if row is None:
            raise ProtocolError("not_found", detail="task not found in this session")
        return self._task_projection(row)

    def _task_projection(self, row: Any) -> dict[str, Any]:
        """Task view: lifecycle, readiness and evidence *references* only.

        Evidence references are artifact IDs with non-content metadata; the
        diagnostic body itself is only reachable through read_feedback.
        """

        return {
            "task_id": row["task_id"],
            "attempt_id": row["attempt_id"],
            "role": row["role"],
            "name": row["name"],
            "state": row["state"],
            "readiness": row["readiness"],
            "exit_reason": row["exit_reason"],
            "required": bool(row["required"]),
            "dependencies": self.store.loads(row["dependencies_json"], []),
            "deadline_utc": row["deadline_utc"],
            "evidence_refs": self.store.loads(row["evidence_refs_json"], []),
        }

    def wait_task(self, session_id: str, task_id: str, *, condition: str, timeout_seconds: float) -> dict[str, Any]:
        record = self._load_session(session_id)
        self.sessions.authorize(record, "wait_task")
        row = self.store.query_one("SELECT * FROM tasks WHERE task_id=? AND session_id=?", (task_id, session_id))
        if row is None:
            raise ProtocolError("not_found", detail="task not found in this session")
        if condition not in ("ready", "exited"):
            raise ProtocolError("malformed_request", detail="unsupported wait condition")
        deadline = min(self.clock.monotonic() + max(0.0, timeout_seconds), 30.0 + self.clock.monotonic())
        satisfied = False
        while True:
            current = self.store.query_one("SELECT state, readiness FROM tasks WHERE task_id=?", (task_id,))
            if current is None:
                raise ProtocolError("not_found", detail="task disappeared")
            if condition == "ready" and current["readiness"] == "READY":
                satisfied = True
                break
            if condition == "exited" and current["state"] in (TASK_EXITED,):
                satisfied = True
                break
            if self.clock.monotonic() >= deadline:
                break
            self.clock_advance_hook()
        current = self.store.query_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        return {
            "task_id": task_id,
            "condition": condition,
            "satisfied": satisfied,
            "state": current["state"],
            "reason_code": None if satisfied else "REQUEST_REJECTED",
        }

    def clock_advance_hook(self) -> None:
        """Hook for deterministic clocks in tests; a no-op for real clocks."""

        return None

    def stop_task(self, session_id: str, task_id: str) -> dict[str, Any]:
        record = self._load_session(session_id)
        self.sessions.authorize(record, "stop_task")
        row = self.store.query_one("SELECT * FROM tasks WHERE task_id=? AND session_id=?", (task_id, session_id))
        if row is None:
            raise ProtocolError("not_found", detail="task not found in this session")
        if row["role"] == "collector" and row["required"]:
            # Required collectors stay running until cleanup is complete.
            self._record_rejection(
                session_id, scope="stop_task", reason_code="REQUEST_REJECTED",
                error_code="permission_denied", detail="required collector cannot be stopped by the agent",
            )
            raise ProtocolError("permission_denied", detail="required collector cannot be stopped")
        dependents = self.store.query_all(
            "SELECT task_id FROM tasks WHERE attempt_id=? AND dependencies_json LIKE ?",
            (row["attempt_id"], f'%"{task_id}"%'),
        )
        active = [d["task_id"] for d in dependents]
        if active:
            self._record_rejection(
                session_id, scope="stop_task", reason_code="REQUEST_REJECTED",
                error_code="permission_denied", detail="dependent tasks must stop first",
            )
            raise ProtocolError("permission_denied", detail="stop dependent tasks first")
        with self.store.txn():
            self.store.execute(
                "UPDATE tasks SET state=?, exit_reason=?, updated_utc=? WHERE task_id=?",
                (TASK_EXITED, "cancelled", self.clock.utc(), task_id),
            )
            self.events.append(
                session_id, "task_stopped", payload={"task_id": task_id}, task_id=task_id,
                attempt_id=row["attempt_id"],
            )
        return {"task_id": task_id, "stop_requested": True, "exit_reason": "cancelled"}

    # ------------------------------------------------------------------
    # feedback
    # ------------------------------------------------------------------
    def read_feedback(
        self,
        session_id: str,
        *,
        candidate_id: str,
        kind: str,
        build_id: str | None = None,
        attempt_id: str | None = None,
        cursor: int | None = None,
    ) -> dict[str, Any]:
        """Restricted paged diagnostics. Billed on delivered UTF-8 JSON bytes."""

        record = self._load_session(session_id)
        self.sessions.authorize(record, "read_feedback")
        self._check_rate(session_id)

        candidate = self.store.query_one(
            "SELECT * FROM candidates WHERE candidate_id=? AND session_id=?", (candidate_id, session_id)
        )
        if candidate is not None:
            tree_hash = candidate["tree_hash"]
        else:
            # Source-policy rejections are bucketed by a derived hash and are
            # still visible to the submitting session.
            row = self.store.query_one(
                "SELECT tree_hash FROM diagnostics WHERE session_id=? AND candidate_id='' LIMIT 1",
                (session_id,),
            )
            tree_hash = row["tree_hash"] if row is not None else None
        if tree_hash is None:
            raise ProtocolError("not_found", detail="candidate not found in this session")

        # Build diagnostics may be read without an attempt_id (a failed build has
        # none); runtime diagnostics require the candidate's own attempt_id.
        where = ["session_id=?", "tree_hash=?", "scope=?"]
        params: list[Any] = [session_id, tree_hash, kind]
        if kind == "build":
            if build_id is not None:
                build = self.store.query_one(
                    "SELECT build_id FROM builds WHERE build_id=? AND session_id=? AND candidate_id=?",
                    (build_id, session_id, candidate_id),
                )
                if build is None:
                    raise ProtocolError("not_found", detail="build not found for this candidate")
                where.append("build_id=?")
                params.append(build_id)
            else:
                where.append("build_id IS NOT NULL")
        elif kind == "runtime":
            # A runtime read names an attempt. An attempt from another candidate
            # or another session is a not-found, never a malformed request.
            if not attempt_id:
                raise ProtocolError(
                    "malformed_request",
                    detail="runtime diagnostics require the candidate's own attempt_id",
                )
            attempt = self.store.query_one(
                "SELECT attempt_id, candidate_id FROM attempts WHERE attempt_id=? AND session_id=?",
                (attempt_id, session_id),
            )
            if attempt is None or attempt["candidate_id"] != candidate_id:
                raise ProtocolError("not_found", detail="attempt does not belong to this candidate")
            where.append("attempt_id=?")
            params.append(attempt_id)
        else:
            raise ProtocolError("malformed_request", detail="kind must be build or runtime")

        max_page = int(self.config.feedback_limits["diagnostic_page_bytes_max"])
        max_pages = int(self.config.feedback_limits["diagnostic_pages_per_candidate"])
        max_cand_bytes = int(self.config.feedback_limits["diagnostic_bytes_per_candidate"])
        max_sess_bytes = int(self.config.feedback_limits["diagnostic_bytes_per_session"])

        base_params: list[Any] = list(params)

        with self.store.txn():
            # Quota is examined and charged inside one transaction so that
            # concurrent readers cannot jointly exceed the limits.
            remaining = self.store.feedback_remaining(
                session_id, tree_hash, max_pages=max_pages, max_bytes=max_cand_bytes
            )
            session_row = self.store.read_ledger_row(session_id, "session:diag_bytes")
            sess_left = max_sess_bytes - session_row["bytes_used"]
            if remaining["pages_left"] <= 0:
                return self._feedback_quota_error("DIAGNOSTIC_CANDIDATE_PAGE_LIMIT")
            if remaining["bytes_left"] <= 0 or sess_left <= 0:
                return self._feedback_quota_error("DIAGNOSTIC_CANDIDATE_BYTES_LIMIT")

            sql = (
                "SELECT * FROM diagnostics WHERE " + " AND ".join(where)
                + " ORDER BY created_utc, ordinal, diagnostic_id"
            )
            rows = self.store.query_all(sql, base_params)
            start = int(cursor or 0)
            selected = rows[start:]
            budget_for_page = min(max_page, max(0, remaining["bytes_left"]), max(0, sess_left))

            entries: list[dict[str, Any]] = []
            truncated = False
            truncation_reason: str | None = None
            incomplete = any(int(r["incomplete"]) for r in selected)
            page_bytes = 0
            for row in selected:
                payload = self.store.loads(row["payload_json"], {})
                entry = {"kind": row["kind"], "ordinal": row["ordinal"], **payload}
                encoded = len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                # Reserve space for the truncation metadata itself.
                if page_bytes + encoded > budget_for_page - 256:
                    truncated = True
                    truncation_reason = "DIAGNOSTIC_PAGE_BYTES_LIMIT"
                    break
                entries.append(entry)
                page_bytes += encoded
                if len(entries) >= int(self.config.feedback_limits["diagnostics_per_page"]):
                    break

            response: dict[str, Any] = {
                "candidate_id": candidate_id,
                "kind": kind,
                "page_index": start,
                "entries": entries,
                "next_cursor": start + len(entries) if (start + len(entries)) < len(rows) else None,
                "diagnostics_incomplete": bool(incomplete),
                "truncated": truncated or (start + len(entries)) < len(rows),
            }
            if response["truncated"]:
                response["truncation_reason"] = (
                    truncation_reason or "DIAGNOSTIC_PAGE_BYTES_LIMIT"
                )

            delivered = len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if delivered > max_page:
                # Shrink the safe body so the delivered page still fits.
                while entries and delivered > max_page:
                    entries.pop()
                    response["entries"] = entries
                    response["truncated"] = True
                    response["truncation_reason"] = "DIAGNOSTIC_PAGE_BYTES_LIMIT"
                    delivered = len(
                        json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    )
            if delivered > budget_for_page:
                response["truncated"] = True
                response["truncation_reason"] = "DIAGNOSTIC_PAGE_BYTES_LIMIT"

            charge_reason = self.store.feedback_charge(
                session_id,
                tree_hash,
                pages=1,
                bytes_=delivered,
                max_pages=max_pages,
                max_bytes=max_cand_bytes,
            )
            if charge_reason is not None:
                return self._feedback_quota_error(charge_reason)
            self.store.consume_read(
                session_id,
                "session:diag_bytes",
                count=0,
                bytes_=delivered,
                max_count=None,
                max_bytes=max_sess_bytes,
            )
            self.events.append(
                session_id, "feedback_read",
                payload={
                    "kind": kind,
                    "page_index": start,
                    "delivered_bytes": delivered,
                    "entry_count": len(entries),
                    "truncated": bool(response["truncated"]),
                },
                candidate_id=candidate_id or None,
                attempt_id=attempt_id,
            )
        return response

    @staticmethod
    def _feedback_quota_error(reason: str) -> dict[str, Any]:
        """Quota exhaustion response: no status or diagnostic content at all."""

        return {
            "truncated": True,
            "truncation_reason": reason,
            "entries": [],
            "error": {"code": "permission_denied"},
        }

    # ------------------------------------------------------------------
    # status-page quota
    # ------------------------------------------------------------------
    def charge_status_read(self, session_id: str, *, delivered_bytes: int) -> str | None:
        """Charge one status-page read against the session ledger."""

        from runner.contracts.clock import iso_from_epoch

        limit = int(self.config.feedback_limits["status_call_limit"])
        max_bytes = int(self.config.feedback_limits["get_attempt_response_bytes_max"])
        if self.store.counter_available(session_id, C_GET_ATTEMPT_CALLS, 1) is False:
            return "STATUS_CALL_LIMIT"
        with self.store.txn():
            reason = self.store.consume_read(
                session_id,
                "session:get_attempt",
                count=1,
                bytes_=delivered_bytes,
                max_count=limit,
                max_bytes=None,
            )
            if reason is not None:
                return reason
            self._settle(
                session_id,
                reserved=[],
                consumed=[(C_GET_ATTEMPT_CALLS, 1)],
                reason="get_attempt_read",
                ref_type="read",
                ref_id=None,
            )
        return None


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------
_ACTION_PARAMS: dict[str, dict[str, Any]] = {
    "launch_candidate": {
        "type": "object",
        "required": [],
        "additionalProperties": False,
        "description": "Launches the approved single entry Activity of the current candidate build.",
    },
    "wait_task": {
        "type": "object",
        "properties": {"task_id": {"type": "string"}, "condition": {"enum": ["ready", "exited"]}},
    },
    "read_feedback": {
        "type": "object",
        "properties": {"candidate_id": {"type": "string"}, "kind": {"enum": ["build", "runtime"]}},
    },
}


def _validate_launch_params(params: dict[str, Any]) -> None:
    """`launch_candidate` accepts NO intent, extras or component parameters.

    Any attempt to pass a component, action, URI, extras or flags is refused so
    the action can never degrade into a generic launch or ADB entry point.
    """

    if not isinstance(params, dict):
        raise ProtocolError("malformed_request", detail="params must be an object")
    forbidden = {
        "component", "activity", "intent", "action", "uri", "data", "extras", "flags",
        "package", "serial", "command", "shell", "target", "service",
    }
    for key in params:
        if key in forbidden:
            raise ProtocolError(
                "malformed_request",
                detail="launch_candidate does not accept component, intent or extras parameters",
            )
    if params:
        raise ProtocolError("malformed_request", detail="launch_candidate accepts no parameters")


def _decode_files(files_payload: Any, *, limits: dict[str, int]) -> list[SourceFile]:
    """Decode the submitted file list.

    Only ``relative_path`` and ``content_base64`` are accepted. Content is
    materialized in memory with a hard size cap before any path policy check,
    and no absolute or traversal path survives decoding.
    """

    if not isinstance(files_payload, list):
        raise ProtocolError("malformed_request", detail="files must be a list")
    max_files = int(limits.get("max_candidate_files", 64))
    if len(files_payload) > max_files:
        raise ProtocolError("payload_too_large", detail=f"too many files: limit {max_files}")
    max_bytes = int(limits.get("max_candidate_bytes", 262144))
    out: list[SourceFile] = []
    total = 0
    for entry in files_payload:
        if not isinstance(entry, dict):
            raise ProtocolError("malformed_request", detail="each file entry must be an object")
        extra = set(entry) - {"relative_path", "content_base64"}
        if extra:
            raise ProtocolError("malformed_request", detail="unexpected field in file entry")
        rel = entry.get("relative_path")
        content = entry.get("content_base64")
        if not isinstance(rel, str) or not isinstance(content, str):
            raise ProtocolError("malformed_request", detail="file entry needs relative_path and content_base64")
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            raise ProtocolError("malformed_request", detail="content_base64 is not valid base64")
        total += len(data)
        if total > max_bytes:
            raise ProtocolError("payload_too_large", detail=f"submission exceeds {max_bytes} bytes")
        out.append(SourceFile(relative_path=rel, data=data))
    return out


def _build_reason_code(state: str, failure_json: str | None) -> str | None:
    if state == BUILD_SUCCEEDED:
        return None
    if state == BUILD_FAILED:
        return "BUILD_FAILED"
    return None


def _project_operation_result(method: str, result: Any) -> Any:
    """Whitelist operation results so no diagnostic body can ride along."""

    if not isinstance(result, dict):
        return result
    allowed = {
        "build_candidate": {
            "build_id", "candidate_id", "state", "reason_code", "apk_sha256", "apk_size_bytes",
            "package_name", "version_code", "signer_fingerprint", "manifest_sha256", "permissions",
            "components", "queries", "entry_component", "build_provenance", "template_id", "toolchain_id",
            "apk_artifact_id", "log_artifact_id",
        },
        "submit_candidate": {
            "candidate_id", "tree_hash", "file_count", "source_bytes", "status", "reason_code",
        },
        "perform_action": {"action_id", "action_name", "state", "reason_code"},
    }
    keep = allowed.get(method)
    if keep is None:
        return {k: v for k, v in result.items() if k in {"state", "reason_code"}}
    return {k: v for k, v in result.items() if k in keep}


def _recipe_hash(
    store: Store, session_id: str, build_id: str, candidate_id: str, config: SessionConfig
) -> str:
    """Stable hash of the core replay recipe (build + action/window parameters).

    The repaired-state replay reuses this hash while varying only the fixture.
    """

    payload = {
        "build_id": build_id,
        "candidate_id": candidate_id,
        "allowed_actions": sorted(config.allowed_actions),
        "max_launches_per_attempt": config.max_launches_per_attempt,
        "timeouts": {k: config.timeouts.get(k) for k in ("launch_confirmation", "observation_window")},
        "collectors_required": sorted(config.collectors_required),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
