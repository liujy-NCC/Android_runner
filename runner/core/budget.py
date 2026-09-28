"""Budget counter keys and their session-level limits.

Keeping the mapping in one place means every request path asks the same
question: "which counters does this side effect consume?"
"""

from __future__ import annotations

from runner.config import SessionConfig

C_DISTINCT_CANDIDATES = "session:distinct_candidates"
C_BUILDS = "session:builds"
C_EFFECTIVE_ATTEMPTS = "session:effective_attempts"
C_INFRA_RETRIES = "session:infra_retry_attempts"
C_ACTIONS = "session:actions"
C_TASKS = "session:tasks"
C_GET_ATTEMPT_CALLS = "session:get_attempt_calls"
C_DIAG_BYTES = "session:diag_bytes"

TREE_BUILDS = "tree:{tree}:builds"
TREE_RUNS = "tree:{tree}:effective_runs"
TREE_DIAG_PAGES = "tree:{tree}:diag_pages"
TREE_DIAG_BYTES = "tree:{tree}:diag_bytes"


def tree_builds(tree_hash: str) -> str:
    return TREE_BUILDS.format(tree=tree_hash)


def tree_runs(tree_hash: str) -> str:
    return TREE_RUNS.format(tree=tree_hash)


def tree_diag_pages(tree_hash: str) -> str:
    return TREE_DIAG_PAGES.format(tree=tree_hash)


def tree_diag_bytes(tree_hash: str) -> str:
    return TREE_DIAG_BYTES.format(tree=tree_hash)


def session_counter_limits(config: SessionConfig) -> dict[str, int | None]:
    """Map configuration budget fields onto session counter keys."""

    budget = config.budget
    return {
        C_DISTINCT_CANDIDATES: budget.get("max_distinct_candidates"),
        C_BUILDS: budget.get("max_builds"),
        C_EFFECTIVE_ATTEMPTS: budget.get("max_effective_agent_attempts"),
        C_INFRA_RETRIES: budget.get("max_confirmed_infrastructure_retries"),
        C_ACTIONS: budget.get("max_actions"),
        C_TASKS: budget.get("max_tasks"),
        C_GET_ATTEMPT_CALLS: budget.get("max_get_attempt_calls_per_session"),
        C_DIAG_BYTES: budget.get("max_diagnostic_bytes_per_session"),
    }


def tree_counter_limits(config: SessionConfig, tree_hash: str) -> dict[str, int | None]:
    budget = config.budget
    return {
        tree_builds(tree_hash): budget.get("max_builds_per_candidate"),
        tree_runs(tree_hash): budget.get("max_effective_runs_per_candidate"),
    }
