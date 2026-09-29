"""Scenario C: the API process exits; only the independent supervisor remains.

``test_supervisor`` already proves a supervisor finalizes without any *client
polling*. The gap this module closes is the harder case: the API **process
itself** is gone, the client stops polling, and the supervisor must still finish
the attempt from persisted state alone.

Every test here uses REAL operating-system processes (``runner serve`` and
``runner supervisor``) against a real temporary root and a real HTTP socket. The
API process is terminated abruptly (``Process.kill``), so nothing it held in
memory is available to the supervisor.

Guarantees asserted:

* the supervisor completes on the persisted deadline with the API dead;
* closing order is seal-then-stop, never the reverse;
* an API restart neither re-triggers the candidate nor extends the window;
* a supervisor restart neither re-seals, re-settles, nor re-evaluates.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# ---------------------------------------------------------------------------
# process helpers
# ---------------------------------------------------------------------------
def _admin_create_session(root: str) -> tuple[str, str]:
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "runner", "admin", "create-session", "--root", root],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    payload = json.loads(completed.stdout)
    return payload["session_id"], payload["credential"]


def _start_api(root: str, session_id: str) -> tuple[subprocess.Popen, str]:
    process = subprocess.Popen(
        [
            sys.executable, "-B", "-m", "runner", "serve",
            "--root", root, "--port", "0", "--session-id", session_id,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=ROOT,
    )
    base = ""
    for _ in range(300):
        line = process.stdout.readline()
        if not line:
            break
        if "listening at" in line:
            base = line.strip().split("listening at")[-1].strip()
            break
    if not base.startswith("http"):
        process.kill()
        raise AssertionError(f"API did not announce a URL: {base!r}")
    return process, base[: -len("/v1")] if base.endswith("/v1") else base


def _start_supervisor(root: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-B", "-m", "runner", "supervisor", "--root", root, "--interval", "0.05"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=ROOT,
    )


def _call(base: str, method: str, token: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _wait_until(predicate, *, timeout: float = 90.0, interval: float = 0.1, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")


def _open_state_db(root: str):
    """Read-only-ish handle to the control DB for assertions."""

    from runner.contracts.clock import SystemClock
    from runner.store.db import Store

    return Store(os.path.join(root, "control.db"), clock=SystemClock())


def _drive_to_observation(root: str, base: str, token: str) -> dict:
    """Submit -> build -> begin -> launch over real HTTP. Returns identifiers."""

    import base64

    files = [
        {
            "relative_path": "candidate/behaviour.txt",
            "content_base64": base64.b64encode(b"scenario c source\n").decode("ascii"),
        }
    ]
    status, sub = _call(
        base, "POST", token, "/v1/candidates",
        {"idempotency_key": "c-submit-000000000001", "files": files},
    )
    assert status == 200 and sub["status"] == "accepted", (status, sub)

    status, build = _call(
        base, "POST", token, "/v1/builds",
        {"idempotency_key": "c-build-000000000001", "candidate_id": sub["candidate_id"]},
    )
    assert status == 200 and build["state"] == "SUCCEEDED", (status, build)

    status, receipt = _call(
        base, "POST", token, "/v1/attempts",
        {"idempotency_key": "c-begin-000000000001", "build_id": build["build_id"]},
    )
    assert status == 200 and receipt["status"] == "accepted", (status, receipt)
    attempt_id = receipt["attempt_id"]

    status, launch = _call(
        base, "POST", token, f"/v1/attempts/{attempt_id}/actions",
        {"idempotency_key": "c-launch-00000000001", "action_name": "launch_candidate", "params": {}},
    )
    assert status == 200 and launch["state"] == "accepted", (status, launch)
    return {
        "attempt_id": attempt_id,
        "build_id": build["build_id"],
        "candidate_id": sub["candidate_id"],
        "package_name": build["package_name"],
    }


def _expect_connection_dead(base: str, token: str, *, timeout: float = 30.0) -> None:
    def dead() -> bool:
        try:
            _call(base, "GET", token, "/v1/health")
        except (urllib.error.URLError, ConnectionError, socket.timeout, OSError):
            return True
        return False

    _wait_until(dead, timeout=timeout, what="the API socket to stop answering")


# ---------------------------------------------------------------------------
# C.1 API killed after launch; supervisor completes alone
# ---------------------------------------------------------------------------
def test_supervisor_finalizes_after_the_api_process_is_killed() -> None:
    """The API dies mid-observation; the supervisor still closes the attempt."""

    root = tempfile.mkdtemp(prefix="runner-scenario-c-")
    supervisor: subprocess.Popen | None = None
    api: subprocess.Popen | None = None
    try:
        session_id, token = _admin_create_session(root)
        api, base = _start_api(root, session_id)
        supervisor = _start_supervisor(root)
        try:
            ids = _drive_to_observation(root, base, token)
            attempt_id = ids["attempt_id"]

            # The candidate really was launched while the API was alive.
            store = _open_state_db(root)
            try:
                row = store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
                assert row["state"] == "RUNNING", dict(row)
                assert row["launch_confirmed_monotonic"] is not None, dict(row)
            finally:
                store.close()

            # Kill the API process outright, then stop polling entirely.
            api.kill()
            api.wait(timeout=30)
            api = None
            _expect_connection_dead(base, token)

            # From here on NOTHING talks to the API. Only the supervisor runs.
            _wait_until(
                lambda: _attempt_settled(root, attempt_id),
                timeout=120,
                what="the supervisor to close and evaluate the attempt with the API dead",
            )

            store = _open_state_db(root)
            try:
                row = store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
                assert row["state"] == "CLOSED", dict(row)
                assert row["observation_window_complete"] == 1, dict(row)
                assert row["execution_status"] == "completed", dict(row)
                assert row["evidence_sealed"] == 1, dict(row)
                assert row["cleanup_sealed"] == 1, dict(row)
                assert row["closed_utc"], dict(row)

                # Evidence groups are sealed and the Oracle ran exactly once.
                groups = store.query_all(
                    "SELECT kind, state FROM seal_groups WHERE attempt_id=?", (attempt_id,)
                )
                kinds = {g["kind"]: g["state"] for g in groups}
                assert kinds.get("execution_evidence") == "sealed", kinds
                assert kinds.get("cleanup_evidence") == "sealed", kinds
                assert (
                    store.query_one(
                        "SELECT COUNT(*) AS n FROM oracle_evaluations WHERE attempt_id=?", (attempt_id,)
                    )["n"]
                    == 1
                )
            finally:
                store.close()
        finally:
            if api is not None:
                api.kill()
                api.wait(timeout=30)
            if supervisor is not None:
                supervisor.kill()
                supervisor.wait(timeout=30)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _supervisor_sweep_marker(root: str) -> str | None:
    """The durable ``last_sweep_utc`` written at the end of every supervisor sweep."""

    store = _open_state_db(root)
    try:
        row = store.query_one("SELECT last_sweep_utc FROM supervisor_state LIMIT 1")
        return row["last_sweep_utc"] if row else None
    finally:
        store.close()


def _restart_supervisor(root: str, supervisor: subprocess.Popen) -> subprocess.Popen:
    """Kill and restart the supervisor, returning once the new one has swept.

    The marker is read *after* the old process is reaped, so its value is stable
    and any later change can only come from the new process. That makes this a
    deterministic readiness check rather than a blind sleep.
    """

    supervisor.kill()
    supervisor.wait(timeout=30)
    settled = _supervisor_sweep_marker(root)

    fresh = _start_supervisor(root)
    _wait_until(
        lambda: _supervisor_sweep_marker(root) != settled,
        timeout=60,
        what="the restarted supervisor to complete a sweep",
    )
    return fresh


def _wait_for_supervisor_sweeps(root: str, count: int, *, timeout: float = 60.0) -> None:
    """Block until the running supervisor completes ``count`` further sweeps.

    ``heartbeat()`` persists ``last_sweep_utc`` at the end of every sweep, so a
    change in that value proves a full sweep finished. This replaces fixed sleeps
    with a real observation of the work the test depends on.
    """

    initial = _supervisor_sweep_marker(root)
    seen: set[str] = set()
    deadline = time.monotonic() + timeout
    while len(seen) < count:
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {count} supervisor sweeps")
        current = _supervisor_sweep_marker(root)
        if current is not None and current != initial:
            seen.add(current)
        time.sleep(0.05)


def _attempt_settled(root: str, attempt_id: str) -> bool:
    """True once the attempt is CLOSED *and* its Oracle evaluation row exists.

    ``_sweep_attempts`` (which closes the attempt) and ``_sweep_evaluations``
    (which writes the evaluation) are separate transactions in the same sweep, so
    "CLOSED" alone is not a settled state to snapshot.
    """

    store = _open_state_db(root)
    try:
        attempt = store.query_one("SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,))
        if attempt is None or attempt["state"] != "CLOSED":
            return False
        evaluation = store.query_one(
            "SELECT COUNT(*) AS n FROM oracle_evaluations WHERE attempt_id=?", (attempt_id,)
        )
        return int(evaluation["n"]) == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# C.2 seal strictly before stop
# ---------------------------------------------------------------------------
def test_seal_precedes_stop_in_the_supervisor_only_path() -> None:
    """With the API dead, execution evidence is sealed before the stop command."""

    root = tempfile.mkdtemp(prefix="runner-scenario-c-order-")
    supervisor: subprocess.Popen | None = None
    api: subprocess.Popen | None = None
    try:
        session_id, token = _admin_create_session(root)
        api, base = _start_api(root, session_id)
        supervisor = _start_supervisor(root)
        try:
            ids = _drive_to_observation(root, base, token)
            attempt_id = ids["attempt_id"]
            api.kill()
            api.wait(timeout=30)
            api = None

            _wait_until(
                lambda: _attempt_settled(root, attempt_id),
                timeout=120,
                what="the supervisor to close and evaluate the attempt",
            )

            store = _open_state_db(root)
            try:
                # Order is proven by durable timestamps, not by in-process traces:
                # the execution group must be sealed no later than the moment the
                # attempt records its stop/cleanup stage as done.
                execution = store.query_one(
                    "SELECT sealed_utc FROM seal_groups WHERE attempt_id=? AND kind='execution_evidence'",
                    (attempt_id,),
                )
                cleanup = store.query_one(
                    "SELECT sealed_utc FROM seal_groups WHERE attempt_id=? AND kind='cleanup_evidence'",
                    (attempt_id,),
                )
                assert execution is not None and execution["sealed_utc"], dict(execution or {})
                assert cleanup is not None and cleanup["sealed_utc"], dict(cleanup or {})
                assert execution["sealed_utc"] <= cleanup["sealed_utc"], (
                    execution["sealed_utc"],
                    cleanup["sealed_utc"],
                )

                # The stop and restore steps are recorded as completed, which can
                # only happen after sealing in the finalizer's ordering.
                stages = store.loads(
                    store.query_one("SELECT stages_json FROM attempts WHERE attempt_id=?", (attempt_id,))[
                        "stages_json"
                    ],
                    {},
                )
                assert stages.get("cleanup") == "completed", stages
            finally:
                store.close()
        finally:
            if api is not None:
                api.kill()
                api.wait(timeout=30)
            if supervisor is not None:
                supervisor.kill()
                supervisor.wait(timeout=30)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# C.3 restarting the API / supervisor changes nothing
# ---------------------------------------------------------------------------
def test_api_restart_after_close_does_not_retrigger_or_extend() -> None:
    """A restarted API must not re-launch, re-settle or re-open the attempt."""

    root = tempfile.mkdtemp(prefix="runner-scenario-c-restart-")
    supervisor: subprocess.Popen | None = None
    api: subprocess.Popen | None = None
    try:
        session_id, token = _admin_create_session(root)
        api, base = _start_api(root, session_id)
        supervisor = _start_supervisor(root)
        try:
            ids = _drive_to_observation(root, base, token)
            attempt_id = ids["attempt_id"]
            api.kill()
            api.wait(timeout=30)
            api = None

            _wait_until(
                lambda: _attempt_settled(root, attempt_id),
                timeout=120,
                what="the supervisor to close and evaluate the attempt",
            )

            store = _open_state_db(root)
            try:
                before = {
                    "attempt": dict(store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))),
                    "seal_groups": store.query_one("SELECT COUNT(*) AS n FROM seal_groups")["n"],
                    "evaluations": store.query_one("SELECT COUNT(*) AS n FROM oracle_evaluations")["n"],
                    "actions": store.query_one("SELECT COUNT(*) AS n FROM attempt_actions")["n"],
                    "ledger": store.query_one("SELECT COUNT(*) AS n FROM counter_ledger")["n"],
                    "builds": store.query_one("SELECT COUNT(*) AS n FROM builds")["n"],
                }
                observations = store.query_one(
                    "SELECT launch_count, observation_deadline_monotonic FROM attempts WHERE attempt_id=?",
                    (attempt_id,),
                )
            finally:
                store.close()

            # Bring the API back on a fresh port and keep the supervisor running.
            api, base2 = _start_api(root, session_id)
            # Let the still-running supervisor complete several full sweeps with
            # the API back, observed through its durable sweep marker rather than
            # a fixed sleep. This is the window in which a buggy supervisor would
            # re-trigger or extend the closed attempt.
            _wait_for_supervisor_sweeps(root, 3)

            # A fresh launch attempt must be refused: the attempt is closed.
            status, relaunch = _call(
                base2, "POST", token, f"/v1/attempts/{attempt_id}/actions",
                {"idempotency_key": "c-relaunch-0000000001", "action_name": "launch_candidate", "params": {}},
            )
            assert status in (403, 200), (status, relaunch)
            if status == 200:
                assert relaunch["state"] == "rejected", relaunch

            store = _open_state_db(root)
            try:
                after = {
                    "attempt": dict(store.query_one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))),
                    "seal_groups": store.query_one("SELECT COUNT(*) AS n FROM seal_groups")["n"],
                    "evaluations": store.query_one("SELECT COUNT(*) AS n FROM oracle_evaluations")["n"],
                    "actions": store.query_one("SELECT COUNT(*) AS n FROM attempt_actions")["n"],
                    "ledger": store.query_one("SELECT COUNT(*) AS n FROM counter_ledger")["n"],
                    "builds": store.query_one("SELECT COUNT(*) AS n FROM builds")["n"],
                }
                after_observations = store.query_one(
                    "SELECT launch_count, observation_deadline_monotonic FROM attempts WHERE attempt_id=?",
                    (attempt_id,),
                )
            finally:
                store.close()

            # No re-trigger: the launch counter and the window deadline are fixed.
            assert after_observations["launch_count"] == observations["launch_count"], (
                observations["launch_count"],
                after_observations["launch_count"],
            )
            assert (
                after_observations["observation_deadline_monotonic"]
                == observations["observation_deadline_monotonic"]
            ), "the observation window was extended"
            assert after["attempt"]["state"] == "CLOSED"
            assert after["attempt"]["closed_utc"] == before["attempt"]["closed_utc"], "attempt was re-closed"

            # No duplicate settlement of any kind.
            for key in ("seal_groups", "evaluations", "actions", "ledger", "builds"):
                assert after[key] == before[key], (key, before[key], after[key])
        finally:
            if api is not None:
                api.kill()
                api.wait(timeout=30)
            if supervisor is not None:
                supervisor.kill()
                supervisor.wait(timeout=30)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_supervisor_restart_does_not_reseal_resettle_or_reevaluate() -> None:
    """Killing and restarting the supervisor must not duplicate any outcome."""

    root = tempfile.mkdtemp(prefix="runner-scenario-c-suprestart-")
    supervisor: subprocess.Popen | None = None
    api: subprocess.Popen | None = None
    try:
        session_id, token = _admin_create_session(root)
        api, base = _start_api(root, session_id)
        supervisor = _start_supervisor(root)
        try:
            ids = _drive_to_observation(root, base, token)
            attempt_id = ids["attempt_id"]
            api.kill()
            api.wait(timeout=30)
            api = None

            # Wait for the attempt's *fully* settled state, not merely CLOSED.
            # Finalization (seal -> stop -> restore -> CLOSED) and the private
            # Oracle evaluation are separate transactions inside one sweep, so an
            # attempt can be observed CLOSED a moment before its evaluation row
            # lands. Waiting only for CLOSED made this test read a snapshot in
            # that window and then fail on "evaluations 0 -> 1" when the restarted
            # supervisor completed the same sweep.
            _wait_until(
                lambda: _attempt_settled(root, attempt_id),
                timeout=120,
                what="the supervisor to close the attempt and evaluate it",
            )

            def snapshot() -> dict:
                store = _open_state_db(root)
                try:
                    return {
                        "artifacts": store.query_one("SELECT COUNT(*) AS n FROM artifacts")["n"],
                        "seal_groups": store.query_one("SELECT COUNT(*) AS n FROM seal_groups")["n"],
                        "evaluations": store.query_one("SELECT COUNT(*) AS n FROM oracle_evaluations")["n"],
                        "ledger": store.query_one("SELECT COUNT(*) AS n FROM counter_ledger")["n"],
                        "closed_utc": store.query_one(
                            "SELECT closed_utc FROM attempts WHERE attempt_id=?", (attempt_id,)
                        )["closed_utc"],
                        "window": store.query_one(
                            "SELECT observation_window_complete FROM attempts WHERE attempt_id=?",
                            (attempt_id,),
                        )["observation_window_complete"],
                    }
                finally:
                    store.close()

            before = snapshot()

            # Restart the supervisor twice; it must be a no-op every time. Waiting
            # on the new process's durable register marker makes each restart
            # deterministic instead of a fixed sleep.
            for _ in range(2):
                supervisor = _restart_supervisor(root, supervisor)

            after = snapshot()
            assert after == before, (before, after)
            assert after["window"] == 1, after
        finally:
            if api is not None:
                api.kill()
                api.wait(timeout=30)
            if supervisor is not None:
                supervisor.kill()
                supervisor.wait(timeout=30)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_supervisor_alone_closes_the_session_after_the_api_dies() -> None:
    """The session-level wall-clock close must not depend on the API process."""

    root = tempfile.mkdtemp(prefix="runner-scenario-c-session-")
    supervisor: subprocess.Popen | None = None
    api: subprocess.Popen | None = None
    try:
        session_id, _token = _admin_create_session(root)

        # Shorten the session wall clock so the close path is reachable quickly.
        from runner.contracts.clock import iso_from_epoch
        from runner.store.db import Store

        store = Store(os.path.join(root, "control.db"))
        try:
            with store.txn():
                store.execute(
                    "UPDATE sessions SET wall_deadline_utc=? WHERE session_id=?",
                    (iso_from_epoch(__import__("time").time() + 2.0), session_id),
                )
        finally:
            store.close()

        supervisor = _start_supervisor(root)
        try:
            _wait_until(
                lambda: _session_state(root, session_id) == "CLOSED",
                timeout=120,
                what="the supervisor to close the expired session without any API",
            )
            store = _open_state_db(root)
            try:
                row = store.query_one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
                assert row["state"] == "CLOSED", dict(row)
                assert row["closed_utc"], dict(row)
            finally:
                store.close()
        finally:
            supervisor.kill()
            supervisor.wait(timeout=30)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _session_state(root: str, session_id: str) -> str:
    store = _open_state_db(root)
    try:
        row = store.query_one("SELECT state FROM sessions WHERE session_id=?", (session_id,))
        return row["state"] if row else "MISSING"
    finally:
        store.close()
