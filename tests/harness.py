"""Shared test harness: a fully deterministic mock Runner environment."""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runner.adapters.build_backend import MockBuildBackend
from runner.adapters.collectors_mock import build_mock_collector_set
from runner.adapters.device_mock import MockDeviceAdapter
from runner.adapters.oracle import MockOracle
from runner.adapters.projection import DiagnosticOnlyProjector
from runner.config import SessionConfig, mock_config
from runner.contracts.clock import FixedClock
from runner.core.service import RunnerService
from runner.core.supervisor import Supervisor
from runner.store.artifacts import ArtifactStore
from runner.store.db import Store
from runner.store.events import EventLog
from runner.transports.http_api import ControllerApi, Request

JAVA_ROOT = "src/main/java/org/androidpoc/and000008/candidate/"


def make_source_files(
    body: str = "public final class CandidateBehaviour {}",
    class_name: str = "CandidateBehaviour",
    extra: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Build an `and000008-java-v1`-shaped submission payload."""

    files = {
        JAVA_ROOT + f"{class_name}.java": (
            "package org.androidpoc.and000008.candidate;\n\n" + body + "\n"
        ),
        JAVA_ROOT + "CandidateBehavior.java": (
            "package org.androidpoc.and000008.candidate;\n\n"
            "public final class CandidateBehavior {\n"
            "    public void run() {\n"
            "    }\n"
            "}\n"
        ),
    }
    if extra:
        files.update(extra)
    return [
        {"relative_path": path, "content_base64": base64.b64encode(data.encode("utf-8")).decode("ascii")}
        for path, data in sorted(files.items())
    ]


def mock_submission(files_payload: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    if files_payload is not None:
        return files_payload
    return [
        {
            "relative_path": "candidate/behaviour.txt",
            "content_base64": base64.b64encode(b"candidate behaviour source\n").decode("ascii"),
        }
    ]


class Harness:
    """One deterministic Runner instance plus a dispatcher for the HTTP layer."""

    def __init__(
        self,
        *,
        config: SessionConfig | None = None,
        build_fails: bool = False,
        build_failure_file: str | None = None,
        device: MockDeviceAdapter | None = None,
        oracle_scenario: str = "present_present",
        oracle_unavailable: bool = False,
        include_private_markers: bool = True,
        root: str | None = None,
    ) -> None:
        self.root = root or tempfile.mkdtemp(prefix="runner-test-")
        self.clock = FixedClock()
        self.config = config or mock_config()
        self.device = device or MockDeviceAdapter()
        self.build_backend = MockBuildBackend(fail=build_fails, failure_file=build_failure_file)
        self.oracle = MockOracle(scenario=oracle_scenario, unavailable=oracle_unavailable)
        self.projector = DiagnosticOnlyProjector()
        self._include_private_markers = include_private_markers

        factory = self._collector_factory
        self.service = RunnerService(
            root=self.root,
            config=self.config,
            store=Store(os.path.join(self.root, "control.db"), clock=self.clock),
            clock=self.clock,
            device=self.device,
            build_backend=self.build_backend,
            oracle=self.oracle,
            projector=self.projector,
            collector_factory=factory,
        )
        self.api = ControllerApi(self.service, self.config)
        self.session_id, self.token = self.service.create_session(self.config)
        self.service.activate_session(self.session_id, self.config)

    def _collector_factory(self, **kwargs: Any) -> dict[str, Any]:
        return build_mock_collector_set(include_private_markers=self._include_private_markers, **kwargs)

    # -- dispatch helpers ----------------------------------------------------
    def call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        token: Any = ...,
        session_id: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        payload = dict(body or {})
        if method == "POST" and "session_id" not in payload:
            payload["session_id"] = session_id or self.session_id
        request = Request(
            method=method,
            path=path,
            query=query or {},
            body=payload,
            raw_body=json.dumps(payload).encode("utf-8"),
            token=self.token if token is ... else token,
            implicit_session_id=session_id or self.session_id,
        )
        response = self.api.dispatch(request)
        return response.status, response.body

    def get(self, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        return self.call("GET", path, **kwargs)

    def post(self, path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        return self.call("POST", path, body=body, **kwargs)

    # -- convenience flows ---------------------------------------------------
    def submit(self, key: str, files: list[dict[str, str]] | None = None) -> dict[str, Any]:
        _status, body = self.post("/v1/candidates", {"idempotency_key": key, "files": files or mock_submission()})
        return body

    def build(self, key: str, candidate_id: str) -> dict[str, Any]:
        _status, body = self.post(
            "/v1/builds", {"idempotency_key": key, "candidate_id": candidate_id}
        )
        return body

    def begin(self, key: str, build_id: str) -> dict[str, Any]:
        _status, body = self.post("/v1/attempts", {"idempotency_key": key, "build_id": build_id})
        return body

    def launch(self, key: str, attempt_id: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        _status, body = self.post(
            f"/v1/attempts/{attempt_id}/actions",
            {"idempotency_key": key, "action_name": "launch_candidate", "params": params or {}},
        )
        return body

    def make_supervisor(self) -> Supervisor:
        return Supervisor(
            store=self.service.store,
            artifacts=self.service.artifacts,
            events=self.service.events,
            clock=self.clock,
            config=self.config,
            device=self.device,
            oracle=self.service.oracle,
        )

    def close(self) -> None:
        try:
            self.service.store.close()
        finally:
            if os.path.isdir(self.root) and self.root.startswith(tempfile.gettempdir()):
                shutil.rmtree(self.root, ignore_errors=True)


def jbytes(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))
