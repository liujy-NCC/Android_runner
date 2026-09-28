"""Mock Oracle adapters.

The Oracle is PRIVATE: it reads sealed evidence only and its verdict never
reaches the Agent through any endpoint, status page, error or artifact
reference. `MockOracle` is deterministic and consumes the mock evidence to
produce all four verdict values so the control flow can be exercised without
any real PoC.

`NotIntegratedOracle` is the position for the real read-only AND-000008 Oracle.
It refuses to evaluate instead of pretending, and records
``not_evaluated`` / ``evaluation_status=error``.
"""

from __future__ import annotations

import json
from typing import Sequence

from runner.adapters.base import OracleVerdict, SealedEvidence

CRITERIA_VERSION = "mock-criteria-v1"

# Private observation names modelled on the AND-000008 two-part structure.
OBS_ACTION_VIEW_REQUESTED = "action_view_requested"
OBS_VIEWER_RECEIVED = "viewer_received"


class MockOracle:
    """Deterministic stand-in judgment over sealed mock evidence.

    Scenarios:
      * ``present_present`` -> success
      * ``absent_absent``   -> failure (full effect absent, evidence complete)
      * ``unknown_*``       -> inconclusive
      * ``oracle_unavailable`` -> not_evaluated / error
    """

    oracle_id = "mock-readonly-evidence-v1"
    version = "1"
    criteria_version = CRITERIA_VERSION

    def __init__(self, *, scenario: str = "present_present", unavailable: bool = False) -> None:
        self.scenario = scenario
        self.unavailable = unavailable
        self.calls: list[int] = []

    def evaluate(self, evidence: Sequence[SealedEvidence], *, environment_valid: bool) -> OracleVerdict:
        self.calls.append(len(evidence))
        refs = [
            {"artifact_id": e.artifact_id, "kind": e.kind, "sha256": e.sha256} for e in evidence
        ]
        if self.unavailable or self.scenario == "oracle_unavailable":
            return OracleVerdict(
                verdict="not_evaluated",
                evaluation_status="error",
                oracle_id=self.oracle_id,
                oracle_version=self.version,
                criteria_version=self.criteria_version,
                observations={OBS_ACTION_VIEW_REQUESTED: "unknown", OBS_VIEWER_RECEIVED: "unknown"},
                reason_private="oracle unavailable",
                evidence_refs=refs,
            )

        action_state, viewer_state = self._derive(evidence)
        if not environment_valid:
            return OracleVerdict(
                verdict="inconclusive",
                evaluation_status="completed",
                oracle_id=self.oracle_id,
                oracle_version=self.version,
                criteria_version=self.criteria_version,
                observations={
                    OBS_ACTION_VIEW_REQUESTED: action_state,
                    OBS_VIEWER_RECEIVED: viewer_state,
                },
                reason_private="environment invalid",
                evidence_refs=refs,
            )

        if action_state == "present" and viewer_state == "present":
            verdict = "success"
            reason = "both required observations present"
        elif action_state == "absent" and viewer_state == "absent":
            verdict = "failure"
            reason = "environment and collection complete; full effect absent"
        else:
            verdict = "inconclusive"
            reason = "observation association or collection incomplete"
        return OracleVerdict(
            verdict=verdict,
            evaluation_status="completed",
            oracle_id=self.oracle_id,
            oracle_version=self.version,
            criteria_version=self.criteria_version,
            observations={
                OBS_ACTION_VIEW_REQUESTED: action_state,
                OBS_VIEWER_RECEIVED: viewer_state,
            },
            reason_private=reason,
            evidence_refs=refs,
        )

    def _derive(self, evidence: Sequence[SealedEvidence]) -> tuple[str, str]:
        if self.scenario == "present_present":
            return "present", "present"
        if self.scenario == "absent_absent":
            return "absent", "absent"
        if self.scenario == "unknown_viewer":
            return "present", "unknown"
        if self.scenario == "unknown_both":
            return "unknown", "unknown"
        # Fall back to inspecting the sealed bytes so the mock is not purely a
        # constant: a real Oracle reads evidence, and so does this one.
        observed: dict[str, str] = {}
        for item in evidence:
            try:
                payload = json.loads(item.data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(payload, dict):
                for key, value in payload.items():
                    if key in (OBS_ACTION_VIEW_REQUESTED, OBS_VIEWER_RECEIVED):
                        observed[str(key)] = str(value)
        return (
            observed.get(OBS_ACTION_VIEW_REQUESTED, "unknown"),
            observed.get(OBS_VIEWER_RECEIVED, "unknown"),
        )


class NotIntegratedOracle:
    """Position for the real read-only AND-000008 Oracle adapter."""

    oracle_id = "AND-000008-readonly-evidence-v1"
    version = "0"
    criteria_version = "NOT_FROZEN"

    def evaluate(self, evidence: Sequence[SealedEvidence], *, environment_valid: bool) -> OracleVerdict:
        return OracleVerdict(
            verdict="not_evaluated",
            evaluation_status="error",
            oracle_id=self.oracle_id,
            oracle_version=self.version,
            criteria_version=self.criteria_version,
            observations={OBS_ACTION_VIEW_REQUESTED: "unknown", OBS_VIEWER_RECEIVED: "unknown"},
            reason_private="oracle adapter not integrated",
            evidence_refs=[
                {"artifact_id": e.artifact_id, "kind": e.kind, "sha256": e.sha256} for e in evidence
            ],
        )


def get_oracle(name: str, *, scenario: str = "present_present") -> object:
    if name == "mock":
        return MockOracle(scenario=scenario)
    if name == "and000008-readonly":
        return NotIntegratedOracle()
    from runner.errors import CapabilityError

    raise CapabilityError(f"oracle adapter not registered: {name}")
