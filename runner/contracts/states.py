"""State machines, stage vocabulary and health enumerations.

Values here are fixed by DESIGN.md v0.6 §3 / §4.6 / §7 and by
IMPLEMENTATION-CONTRACT.md §6. Transitions are declared once and used by the
core service so that no code path can invent a transition.
"""

from __future__ import annotations

# --- session ---------------------------------------------------------------
SESSION_CREATED = "CREATED"
SESSION_ACTIVE = "ACTIVE"
SESSION_CLOSING = "CLOSING"
SESSION_CLOSED = "CLOSED"

SESSION_STATES = (SESSION_CREATED, SESSION_ACTIVE, SESSION_CLOSING, SESSION_CLOSED)
SESSION_TRANSITIONS: dict[str, frozenset[str]] = {
    SESSION_CREATED: frozenset({SESSION_ACTIVE, SESSION_CLOSING}),
    SESSION_ACTIVE: frozenset({SESSION_CLOSING}),
    SESSION_CLOSING: frozenset({SESSION_CLOSED}),
    SESSION_CLOSED: frozenset(),
}
SESSION_TERMINAL = frozenset({SESSION_CLOSED})

# Close reasons are recorded separately from the state: a closed session never
# passes through a bare STOPPED terminal state.
SESSION_CLOSE_REASONS = (
    "budget_exhausted",
    "agent_requested",
    "agent_lost",
    "wall_clock_expired",
    "device_quarantined",
    "supervisor_recovery",
    "finalized",
)

# --- attempt ---------------------------------------------------------------
ATTEMPT_ACCEPTED = "ACCEPTED"
ATTEMPT_PREPARING = "PREPARING"
ATTEMPT_RUNNING = "RUNNING"
ATTEMPT_FINALIZING = "FINALIZING"
ATTEMPT_CLOSED = "CLOSED"

ATTEMPT_STATES = (
    ATTEMPT_ACCEPTED,
    ATTEMPT_PREPARING,
    ATTEMPT_RUNNING,
    ATTEMPT_FINALIZING,
    ATTEMPT_CLOSED,
)
ATTEMPT_TRANSITIONS: dict[str, frozenset[str]] = {
    ATTEMPT_ACCEPTED: frozenset({ATTEMPT_PREPARING, ATTEMPT_FINALIZING}),
    ATTEMPT_PREPARING: frozenset({ATTEMPT_RUNNING, ATTEMPT_FINALIZING}),
    ATTEMPT_RUNNING: frozenset({ATTEMPT_FINALIZING}),
    ATTEMPT_FINALIZING: frozenset({ATTEMPT_CLOSED}),
    ATTEMPT_CLOSED: frozenset(),
}
ATTEMPT_TERMINAL = frozenset({ATTEMPT_CLOSED})

# Seven Agent-visible stages. "observe" describes observation window and
# collection progress ONLY; it never means the vulnerability triggered.
ATTEMPT_STAGES: tuple[str, ...] = (
    "intake",
    "build",
    "install",
    "permission_check",
    "launch",
    "observe",
    "cleanup",
)
STAGE_STATUSES = ("pending", "running", "completed", "failed", "skipped", "unknown")

# --- build -----------------------------------------------------------------
BUILD_PENDING = "PENDING"
BUILD_RUNNING = "RUNNING"
BUILD_SUCCEEDED = "SUCCEEDED"
BUILD_FAILED = "FAILED"
BUILD_CANCELLED = "CANCELLED"

BUILD_STATES = (BUILD_PENDING, BUILD_RUNNING, BUILD_SUCCEEDED, BUILD_FAILED, BUILD_CANCELLED)
BUILD_TRANSITIONS: dict[str, frozenset[str]] = {
    BUILD_PENDING: frozenset({BUILD_RUNNING, BUILD_CANCELLED}),
    BUILD_RUNNING: frozenset({BUILD_SUCCEEDED, BUILD_FAILED, BUILD_CANCELLED}),
    BUILD_SUCCEEDED: frozenset(),
    BUILD_FAILED: frozenset(),
    BUILD_CANCELLED: frozenset(),
}

# --- operation -------------------------------------------------------------
OP_PENDING = "PENDING"
OP_RUNNING = "RUNNING"
OP_SUCCEEDED = "SUCCEEDED"
OP_FAILED = "FAILED"
OP_CANCELLED = "CANCELLED"
OP_OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"

OPERATION_STATES = (OP_PENDING, OP_RUNNING, OP_SUCCEEDED, OP_FAILED, OP_CANCELLED, OP_OUTCOME_UNKNOWN)
OPERATION_TRANSITIONS: dict[str, frozenset[str]] = {
    OP_PENDING: frozenset({OP_RUNNING, OP_SUCCEEDED, OP_FAILED, OP_CANCELLED, OP_OUTCOME_UNKNOWN}),
    OP_RUNNING: frozenset({OP_SUCCEEDED, OP_FAILED, OP_CANCELLED, OP_OUTCOME_UNKNOWN}),
    OP_SUCCEEDED: frozenset(),
    OP_FAILED: frozenset(),
    OP_CANCELLED: frozenset(),
    OP_OUTCOME_UNKNOWN: frozenset(),
}

# --- task ------------------------------------------------------------------
TASK_REGISTERED = "REGISTERED"
TASK_STARTING = "STARTING"
TASK_RUNNING = "RUNNING"
TASK_STOPPING = "STOPPING"
TASK_EXITED = "EXITED"
TASK_UNKNOWN = "UNKNOWN"

TASK_STATES = (
    TASK_REGISTERED,
    TASK_STARTING,
    TASK_RUNNING,
    TASK_STOPPING,
    TASK_EXITED,
    TASK_UNKNOWN,
)
TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    TASK_REGISTERED: frozenset({TASK_STARTING, TASK_STOPPING, TASK_EXITED, TASK_UNKNOWN}),
    TASK_STARTING: frozenset({TASK_RUNNING, TASK_STOPPING, TASK_EXITED, TASK_UNKNOWN}),
    TASK_RUNNING: frozenset({TASK_STOPPING, TASK_EXITED, TASK_UNKNOWN}),
    TASK_STOPPING: frozenset({TASK_EXITED, TASK_UNKNOWN}),
    TASK_EXITED: frozenset(),
    TASK_UNKNOWN: frozenset({TASK_EXITED, TASK_STOPPING}),
}
READINESS = ("PENDING", "READY", "FAILED", "UNKNOWN")
EXIT_REASONS = ("completed", "crashed", "cancelled", "timed_out", "startup_failed")
TASK_ROLES = ("collector", "candidate", "auxiliary")

# --- artifact / seal -------------------------------------------------------
ARTIFACT_STAGED = "staged"
ARTIFACT_SEALED = "sealed"
ARTIFACT_CORRUPT = "corrupt"
ARTIFACT_QUARANTINED = "quarantined"

SEAL_OPEN = "open"
SEAL_SEALING = "sealing"
SEAL_SEALED = "sealed"
SEAL_CORRUPT = "corrupt"

SEAL_KINDS = (
    "execution_evidence",
    "cleanup_evidence",
    "build_log",
    "source_snapshot",
    "metadata",
)

# --- device / execution / recovery (kept as separate columns by design) ----
DEVICE_HEALTH = ("ready", "degraded", "unavailable", "unknown")
DEVICE_QUARANTINED = "QUARANTINED"

EXECUTION_STATUSES = (
    "completed",
    "setup_failed",
    "install_failed",
    "cancelled",
    "timed_out",
    "device_lost",
    "outcome_unknown",
    "infrastructure_error",
)
RECOVERY_STATUSES = ("succeeded", "failed", "not_required")

# Verdict values are PRIVATE. They are never projected to the Agent.
ORACLE_VERDICTS = ("success", "failure", "inconclusive", "not_evaluated")
EVALUATION_STATUSES = ("pending", "completed", "error")

# --- candidate -------------------------------------------------------------
CANDIDATE_FROZEN = "FROZEN"
CANDIDATE_REJECTED = "REJECTED"

SOURCE_BUILT = "source_built"
PROVENANCE_UNVERIFIED = "unverified"


def can_transition(table: dict[str, frozenset[str]], current: str, target: str) -> bool:
    return target in table.get(current, frozenset())


def assert_transition(table: dict[str, frozenset[str]], current: str, target: str, what: str) -> None:
    if current == target:
        return
    if not can_transition(table, current, target):
        from runner.errors import StateError

        raise StateError(f"illegal {what} transition: {current} -> {target}")
