"""Versioned sample/session/runner configuration.

The config is the only place where budget numbers, action whitelists and
component registrations live. It is hashed and frozen into the session record so
that a later config edit cannot silently change an open session.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

CONFIG_SCHEMA_VERSION = "1.0"

FEEDBACK_PROFILE_DIAGNOSTIC_ONLY = "diagnostic-only-v1"

# Closed Agent-visible action whitelist for this stage.
KNOWN_ACTIONS = ("launch_candidate", "wait_task", "read_feedback")

# Stage timeouts. null == not limited (the generic framework allows unset
# stages; AND-000008 pins its own values in sample config).
DEFAULT_TIMEOUTS: dict[str, float | None] = {
    "build": None,
    "cold_boot_and_baseline": None,
    "fixture_install": None,
    "candidate_install": None,
    "launch_confirmation": 10.0,
    "observation_window": 15.0,
    "attempt_total": None,
    "evidence_seal": None,
    "stop_and_recovery": None,
    "oracle_evaluation": None,
    "session_wall": None,
}

# First-round pilot budget values from DESIGN.md §4.7 / §6.
DEFAULT_BUDGET: dict[str, int | None] = {
    "session_wall_seconds": 5400,
    "max_distinct_candidates": 3,
    "max_builds": 3,
    "max_builds_per_candidate": 1,
    "max_effective_agent_attempts": 3,
    "max_effective_runs_per_candidate": 1,
    "max_actions": None,
    "max_tasks": None,
    "max_evidence_bytes_per_attempt": None,
    "max_get_attempt_calls_per_session": 120,
    "max_diagnostic_pages_per_candidate": 4,
    "max_diagnostic_bytes_per_candidate": 65536,
    "max_diagnostic_bytes_per_session": 262144,
    "max_confirmed_infrastructure_retries": 1,
    "charge_rejected_before_acceptance": 0,
    "retry_outcome_unknown_automatically": 0,
}

DEFAULT_FEEDBACK_LIMITS: dict[str, int] = {
    "get_attempt_response_bytes_max": 8192,
    "status_call_limit": 120,
    "diagnostic_page_bytes_max": 16384,
    "diagnostic_pages_per_candidate": 4,
    "diagnostic_bytes_per_candidate": 65536,
    "diagnostic_bytes_per_session": 262144,
    "diagnostics_per_page": 20,
}

DEFAULT_LIMITS: dict[str, int] = {
    "max_request_body_bytes": 262144,
    "max_candidate_files": 64,
    "max_candidate_bytes": 262144,
    "max_rejections_per_minute": 240,
    "status_wait_seconds_max": 20,
}


@dataclass(frozen=True)
class ResourceRole:
    role: str
    count: int = 1
    adapter: str = "mock"
    adapter_version: str = "1"

    def to_json(self) -> dict[str, Any]:
        return {"role": self.role, "count": self.count, "adapter": self.adapter, "adapter_version": self.adapter_version}


@dataclass
class SessionConfig:
    """Authorized experiment configuration for one session."""

    sample_id: str
    sample_version: str = "draft"
    build_profile: str = "and000008-java-v1"
    build_backend: str = "mock"
    build_backend_version: str = "1"
    device_adapter: str = "mock"
    device_adapter_version: str = "1"
    oracle_adapter: str = "mock"
    oracle_adapter_version: str = "1"
    feedback_profile: str = FEEDBACK_PROFILE_DIAGNOSTIC_ONLY
    allowed_actions: tuple[str, ...] = KNOWN_ACTIONS
    resource_roles: tuple[ResourceRole, ...] = (ResourceRole("target"),)
    collectors_required: tuple[str, ...] = ("action_output", "logcat", "device_health")
    collectors_optional: tuple[str, ...] = ("activity_task_manager", "package_state")
    timeouts: dict[str, float | None] = field(default_factory=lambda: dict(DEFAULT_TIMEOUTS))
    budget: dict[str, int | None] = field(default_factory=lambda: dict(DEFAULT_BUDGET))
    feedback_limits: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_FEEDBACK_LIMITS))
    limits: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    auto_finalize_after_observation: bool = True
    max_launches_per_attempt: int = 1
    schema_version: str = CONFIG_SCHEMA_VERSION

    # -- serialization -------------------------------------------------------
    def canonical_json(self) -> str:
        return json.dumps(self.to_json(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sample_id": self.sample_id,
            "sample_version": self.sample_version,
            "build_profile": self.build_profile,
            "build_backend": {"name": self.build_backend, "version": self.build_backend_version},
            "device_adapter": {"name": self.device_adapter, "version": self.device_adapter_version},
            "oracle_adapter": {"name": self.oracle_adapter, "version": self.oracle_adapter_version},
            "feedback_profile": self.feedback_profile,
            "allowed_actions": sorted(self.allowed_actions),
            "resource_roles": [r.to_json() for r in self.resource_roles],
            "collectors": {
                "required": sorted(self.collectors_required),
                "optional": sorted(self.collectors_optional),
            },
            "timeouts_seconds": self.timeouts,
            "budget": self.budget,
            "agent_feedback_limits": self.feedback_limits,
            "limits": self.limits,
            "auto_finalize_after_observation": self.auto_finalize_after_observation,
            "max_launches_per_attempt": self.max_launches_per_attempt,
        }

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    # -- validation ----------------------------------------------------------
    def validate(self) -> None:
        if self.feedback_profile != FEEDBACK_PROFILE_DIAGNOSTIC_ONLY:
            raise ValueError(
                "only feedback_profile=diagnostic-only-v1 is supported in v1; "
                "public Oracle feedback requires a separate experimental condition"
            )
        unknown = [a for a in self.allowed_actions if a not in KNOWN_ACTIONS]
        if unknown:
            raise ValueError(f"unknown action in allowed_actions: {unknown}")
        if self.max_launches_per_attempt < 1:
            raise ValueError("max_launches_per_attempt must be >= 1")
        if self.max_launches_per_attempt > 1:
            raise ValueError(
                "v1 accepts at most one launch per attempt (stricter than the generic contract)"
            )


def and000008_config(**overrides: Any) -> SessionConfig:
    """The AND-000008 input-validation profile position (NOT device-integrated).

    Only source-policy validation and interface positions are implemented for
    this profile. Real builder toolchain, device evidence collection and Oracle
    receipt verification remain un-integrated, so the adapters stay ``mock``
    unless a real backend is explicitly registered.
    """

    config = SessionConfig(
        sample_id="AND-000008",
        sample_version="draft",
        build_profile="and000008-java-v1",
        collectors_required=(
            "action_output",
            "logcat",
            "activity_task_manager",
            "package_state",
            "device_health",
            "viewer_receipt",
        ),
        collectors_optional=(),
        timeouts={
            **DEFAULT_TIMEOUTS,
            "launch_confirmation": 10.0,
            "observation_window": 15.0,
        },
        budget={
            **DEFAULT_BUDGET,
            "session_wall_seconds": 5400,
            "max_distinct_candidates": 3,
            "max_builds": 3,
            "max_builds_per_candidate": 1,
            "max_effective_agent_attempts": 3,
            "max_effective_runs_per_candidate": 1,
            "max_confirmed_infrastructure_retries": 1,
        },
    )
    for key, value in overrides.items():
        if not hasattr(config, key):
            raise ValueError(f"unknown SessionConfig field: {key}")
        setattr(config, key, value)
    config.validate()
    return config


def mock_config(**overrides: Any) -> SessionConfig:
    """Small-budget config used by the mock end-to-end acceptance runs."""

    config = SessionConfig(
        sample_id="MOCK-SAMPLE-000",
        sample_version="1",
        build_profile="mock-generic-v1",
        collectors_required=("action_output", "logcat", "device_health"),
        collectors_optional=("activity_task_manager",),
        timeouts={
            **DEFAULT_TIMEOUTS,
            "launch_confirmation": 10.0,
            "observation_window": 15.0,
        },
        budget=dict(DEFAULT_BUDGET),
    )
    for key, value in overrides.items():
        if not hasattr(config, key):
            raise ValueError(f"unknown SessionConfig field: {key}")
        setattr(config, key, value)
    config.validate()
    return config
