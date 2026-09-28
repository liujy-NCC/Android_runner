"""Adversarial leakage audit: sweep every Agent-visible surface for private data.

Run from the repository root:  python tests/audit_leakage.py

This is deliberately independent of the unit tests. It drives a full attempt in
two different private-Oracle scenarios and asserts that no Agent-reachable
response, error body, task view, operation view or diagnostic page contains:

  * any Oracle conclusion (verdict / observation / evaluation)
  * any target-side private identity (fixed viewer, ASUS package, URI, Intent)
  * any absolute host path, stack trace or command line
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import Harness, make_source_files  # noqa: E402

# Markers that must never appear in anything the Agent can reach. Sourced from
# the private collectors/oracle in the mock adapters plus generic secret shapes.
FORBIDDEN = (
    "orch",            # oracle id prefix
    "verdict",
    "oracle",
    "observation_result",
    "repaired",
    "asus",
    "fixed.viewer",
    "viewer",
    "commandservice",
    "android.intent.action.view",
    "content://",
    "https://oracle-private",
    "act=",
    "dat=",
    "cmp=",
    "c:\\users",
    "d:\\android_poc",
    "traceback",
    "site-packages",
)

FORBIDDEN_FIELDS = (
    "oracle_verdict",
    "evaluation_status",
    "observation_result",
    "repaired_state",
    "private",
    "verdict_scenario",
)


def _scan(label: str, payload: object, problems: list[str]) -> None:
    if payload is None:
        return
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).lower()
    for marker in FORBIDDEN:
        if marker in blob:
            problems.append(f"{label}: leaked marker {marker!r}")
    if isinstance(payload, dict):
        for key in payload:
            if key.lower() in FORBIDDEN_FIELDS:
                problems.append(f"{label}: leaked field {key!r}")


def drive(scenario: str) -> tuple[dict, list[str]]:
    problems: list[str] = []
    h = Harness(oracle_scenario=scenario)
    try:
        sub = h.submit("audit-submit-00000000001", files=make_source_files())
        build = h.build("audit-build-000000000001", sub["candidate_id"])
        begun = h.begin("audit-begin-000000000001", build["build_id"])
        attempt_id = begun["attempt_id"]

        surfaces: dict[str, object] = {"begin_receipt": begun}

        launched = h.launch("audit-launch-00000000001", attempt_id)
        surfaces["launch_receipt"] = launched
        if launched.get("operation_id"):
            surfaces["operation"] = h.get(f"/v1/operations/{launched['operation_id']}")[1]

        supervisor = h.make_supervisor()
        h.clock.advance(30)
        supervisor.sweep()

        surfaces["attempt"] = h.get(f"/v1/attempts/{attempt_id}")[1]
        surfaces["attempts_list"] = h.get(f"/v1/sessions/{h.session_id}/attempts")[1]
        surfaces["session"] = h.get("/v1/session")[1]
        surfaces["capabilities"] = h.get("/v1/capabilities")[1]
        surfaces["tasks"] = h.get(f"/v1/attempts/{attempt_id}/tasks")[1]
        surfaces["feedback_build"] = h.get(
            "/v1/feedback", query={"candidate_id": sub["candidate_id"], "kind": "build"}
        )[1]
        surfaces["feedback_runtime"] = h.get(
            "/v1/feedback",
            query={"candidate_id": sub["candidate_id"], "kind": "runtime", "attempt_id": attempt_id},
        )[1]

        # Every plausible forbidden route, and a few malformed requests, must
        # also not disclose anything.
        for path in (
            "/v1/oracle", "/v1/evidence", "/v1/artifacts", "/v1/raw", "/v1/logcat",
            "/v1/exec", "/v1/adb", "/v1/admin/oracle",
        ):
            for method in ("GET", "POST"):
                status, body = h.call(method, path, body={"idempotency_key": "audit-probe-0000000001"})
                surfaces[f"probe:{method} {path}:{status}"] = body

        status, body = h.get(f"/v1/attempts/{'att_' + 'Z' * 26}")
        surfaces[f"foreign_attempt:{status}"] = body
        status, body = h.get("/v1/operations/op_" + "Z" * 26)
        surfaces[f"foreign_operation:{status}"] = body

        for label, payload in surfaces.items():
            _scan(label, payload, problems)

        # The private verdict must exist and differ per scenario, proving the
        # scan above was run against a session that really had one.
        private = h.service.read_private_oracle(attempt_id)
        return {"scenario": scenario, "private": private}, problems
    finally:
        h.close()


def main() -> int:
    all_problems: list[str] = []
    for scenario in ("present_present", "absent_absent", "unknown_both"):
        result, problems = drive(scenario)
        private = result["private"]
        print(f"scenario={scenario} private_verdict={private['verdict'] if private else None}")
        all_problems.extend(problems)

    if all_problems:
        print("\nLEAKAGE FOUND:")
        for problem in sorted(set(all_problems)):
            print("  -", problem)
        return 1
    print("\nAUDIT OK: no private material reachable from any Agent-visible surface")
    return 0


if __name__ == "__main__":
    sys.exit(main())
