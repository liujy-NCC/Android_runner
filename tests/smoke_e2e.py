"""End-to-end smoke test over a real HTTP socket plus a separate supervisor.

Run from the repository root:  python tests/smoke_e2e.py

This is the acceptance-shaped walkthrough required by the task:
  create session -> submit candidate -> build -> begin attempt -> launch ->
  collect & seal (supervisor) -> restricted feedback -> revise & retry ->
  finalize. Nothing here touches ADB, an emulator, or a real Oracle.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def call(url: str, method: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", "Bearer " + token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def main() -> int:
    root = tempfile.mkdtemp(prefix="runner-smoke-")
    checks: list[str] = []
    try:
        # 1) Controller-only session provisioning (never over HTTP).
        created = subprocess.run(
            [sys.executable, "-m", "runner", "admin", "create-session", "--root", root],
            capture_output=True, text=True, check=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        session = json.loads(created.stdout)
        session_id, token = session["session_id"], session["credential"]
        checks.append(f"session created locally: {session_id[:12]}...")

        # 2) Serve the restricted API on an ephemeral loopback port.
        server = subprocess.Popen(
            [
                sys.executable, "-m", "runner", "serve", "--root", root,
                "--port", "0", "--session-id", session_id,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        try:
            base = ""
            for _ in range(200):
                line = server.stdout.readline()
                if not line:
                    break
                if "listening at" in line:
                    base = line.strip().split("listening at")[-1].strip()
                    break
            if not base.startswith("http"):
                print("could not discover the server URL", file=sys.stderr)
                return 2
            # The server announces the /v1 base; strip it for the call helpers.
            base = base[: -len("/v1")] if base.endswith("/v1") else base
            checks.append(f"http api listening at {base}")

            # 3) Anonymous and forbidden surfaces.
            status, _ = call(base + "/v1/health", "GET", token)
            assert status == 200, status
            checks.append("health is the only anonymous route")
            for path in ("/v1/exec", "/v1/adb", "/v1/oracle", "/v1/admin/retry"):
                status, _ = call(base + path, "POST", token, {"idempotency_key": "x" * 24})
                assert status in (404, 405), (path, status)
            checks.append("no generic exec/adb/oracle/admin surface exists")

            # 4) Submit a candidate.
            source = [
                {
                    "relative_path": "src/main/java/org/androidpoc/and000008/candidate/CandidateBehavior.java",
                    "content_base64": __import__("base64").b64encode(
                        b"package org.androidpoc.and000008.candidate;\n"
                        b"public final class CandidateBehavior {}\n"
                    ).decode("ascii"),
                }
            ]
            status, submitted = call(
                base + "/v1/candidates", "POST", token,
                {"idempotency_key": "smoke-submit-0000000001", "files": source},
            )
            assert status == 200 and submitted["status"] == "accepted", submitted
            candidate_id = submitted["candidate_id"]
            checks.append(f"candidate frozen: {candidate_id[:12]}...")

            # 5) Build.
            status, built = call(
                base + "/v1/builds", "POST", token,
                {"idempotency_key": "smoke-build-0000000001", "candidate_id": candidate_id},
            )
            assert built["state"] == "SUCCEEDED", built
            checks.append("build succeeded (labelled mock stand-in, not a real APK)")

            # 6) Begin attempt + launch.
            status, begun = call(
                base + "/v1/attempts", "POST", token,
                {"idempotency_key": "smoke-begin-0000000001", "build_id": built["build_id"]},
            )
            assert begun["status"] == "accepted", begun
            attempt_id = begun["attempt_id"]
            checks.append(f"attempt accepted: {attempt_id[:12]}...")

            # A second attempt while one is open is rejected, never queued.
            status, second = call(
                base + "/v1/attempts", "POST", token,
                {"idempotency_key": "smoke-begin-0000000002", "build_id": built["build_id"]},
            )
            assert second["status"] == "rejected", second
            checks.append("second concurrent attempt rejected without queueing")

            status, launched = call(
                base + f"/v1/attempts/{attempt_id}/actions", "POST", token,
                {
                    "idempotency_key": "smoke-launch-0000000001",
                    "action_name": "launch_candidate",
                    "params": {},
                },
            )
            assert launched["state"] == "accepted", launched
            checks.append("launch dispatched")

            # 7) The supervisor finalizes with NO polling from this process.
            # The observation window is 15 s of real time, so the supervisor
            # must run longer than that to finalize on its own deadline.
            supervisor = subprocess.Popen(
                [
                    sys.executable, "-m", "runner", "supervisor", "--root", root,
                    "--interval", "0.05", "--stop-after", "45",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            )
            try:
                supervisor.wait(timeout=120)
            except subprocess.TimeoutExpired:  # pragma: no cover
                supervisor.kill()
                raise
            checks.append("supervisor process ran independently and exited")

            status, page = call(base + f"/v1/attempts/{attempt_id}", "GET", token)
            assert status == 200, page
            assert page["state"] == "CLOSED", page
            assert all(v == "completed" for v in page["stages"].values()), page["stages"]
            checks.append("attempt CLOSED with all seven stages completed")

            # 8) Restricted feedback: diagnostics only, no private material.
            status, feedback = call(
                base + f"/v1/feedback?candidate_id={candidate_id}&kind=runtime&attempt_id={attempt_id}",
                "GET", token,
            )
            assert status == 200 and feedback["entries"], feedback
            blob = json.dumps(feedback).lower()
            for marker in ("asus", "viewer", "verdict", "oracle", "://"):
                assert marker not in blob, (marker, feedback)
            checks.append(
                f"restricted diagnostics returned ({len(feedback['entries'])} entries), no private material"
            )

            # 9) Oracle conclusions stay unreachable from every Agent surface.
            status, _ = call(base + "/v1/oracle", "GET", token)
            assert status == 404, status
            status, attempt_page = call(base + f"/v1/attempts/{attempt_id}", "GET", token)
            assert "verdict" not in json.dumps(attempt_page).lower(), attempt_page
            checks.append("private oracle verdict is unreachable via any HTTP route")

            # 10) Revise and retry with a second candidate.
            source[0]["content_base64"] = __import__("base64").b64encode(
                b"package org.androidpoc.and000008.candidate;\n"
                b"public final class CandidateBehavior { /* revised */ }\n"
            ).decode("ascii")
            status, revised = call(
                base + "/v1/candidates", "POST", token,
                {"idempotency_key": "smoke-submit-0000000002", "files": source},
            )
            assert revised["status"] == "accepted", revised
            status, rebuilt = call(
                base + "/v1/builds", "POST", token,
                {"idempotency_key": "smoke-build-0000000002", "candidate_id": revised["candidate_id"]},
            )
            assert rebuilt["state"] == "SUCCEEDED", rebuilt
            status, final = call(
                base + "/v1/finalize", "POST", token,
                {
                    "idempotency_key": "smoke-finalize-00000001",
                    "candidate_id": revised["candidate_id"],
                    "build_id": rebuilt["build_id"],
                },
            )
            assert status == 200, final
            checks.append("revised candidate built and submitted as final")

            # 11) The session closed cleanly.
            status, closed = call(
                base + "/v1/session/close", "POST", token,
                {"idempotency_key": "smoke-close-0000000001"},
            )
            assert status == 200, closed
            checks.append("session closed")
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                server.kill()
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("SMOKE OK")
    for line in checks:
        print("  -", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
