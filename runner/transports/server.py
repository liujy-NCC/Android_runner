"""HTTP socket binding for the controller API.

Bound to the loopback interface by default. DESIGN.md §10.2 requires the
controller API to be reachable only on a controlled interface; binding
``0.0.0.0`` and relying on a default API key is explicitly rejected, so a
non-loopback bind must be requested explicitly by the operator.
"""

from __future__ import annotations

import json
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from runner import API_PREFIX, SCHEMA_VERSION
from runner.config import SessionConfig
from runner.core.service import RunnerService
from runner.errors import ProtocolError
from runner.transports.http_api import ControllerApi, Request, Response

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


class _Handler(BaseHTTPRequestHandler):
    server_version = "RunnerController/1"
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------------
    @property
    def api(self) -> ControllerApi:
        return self.server.api  # type: ignore[attr-defined]

    @property
    def config(self) -> SessionConfig:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Never let the default logger leak request paths or credentials into a
        # shared log; the structured event ledger is the audit trail.
        return

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    # -- request handling ----------------------------------------------------
    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        token = self._bearer()
        implicit_session = self.server.session_id  # type: ignore[attr-defined]

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        max_body = int(self.config.limits.get("max_request_body_bytes", 262144))
        if length > max_body:
            self._send(
                Response(
                    413,
                    {"error": {"code": "payload_too_large",
                               "message": "request payload exceeds the allowed size"}},
                )
            )
            return
        raw = self.rfile.read(length) if length else b""
        body: dict[str, Any]
        if raw:
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send(
                    Response(
                        400,
                        {"error": {"code": "malformed_request",
                                   "message": "request body or query is not well formed"}},
                    )
                )
                return
            if not isinstance(decoded, dict):
                self._send(
                    Response(
                        400,
                        {"error": {"code": "malformed_request",
                                   "message": "request body or query is not well formed"}},
                    )
                )
                return
            body = decoded
        else:
            body = {}

        request = Request(
            method=method,
            path=parsed.path,
            query=query,
            body=body,
            raw_body=raw,
            token=token,
            implicit_session_id=implicit_session,
        )
        response = self.api.dispatch(request)
        self._send(response)

    def _bearer(self) -> str | None:
        header = self.headers.get("Authorization")
        if not header:
            return None
        parts = header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        return parts[1].strip() or None

    def _send(self, response: Response) -> None:
        blob = response.serialize()
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("X-Runner-Schema-Version", SCHEMA_VERSION)
        self.send_header("X-Runner-Response-Bytes", str(len(blob)))
        self.send_header("X-Runner-Truncated", "true" if response.truncated else "false")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, api: ControllerApi, config: SessionConfig,
                 session_id: str | None) -> None:
        super().__init__(address, handler)
        self.api = api
        self.config = config
        self.session_id = session_id


def make_server(
    api: ControllerApi,
    config: SessionConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8642,
    session_id: str | None = None,
) -> ThreadingHTTPServer:
    if host not in LOOPBACK_HOSTS and not config.limits.get("allow_non_loopback_bind"):
        # An operator/deployment misconfiguration, not an API response: DESIGN
        # §10.2 forbids binding a wildcard address and relying on a shared key.
        raise ValueError(
            f"refusing a non-loopback bind ({host!r}) without an explicit operator opt-in"
        )
    return _Server((host, port), _Handler, api=api, config=config, session_id=session_id)


def serve_forever(
    service: RunnerService,
    config: SessionConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8642,
    session_id: str | None = None,
    announce: bool = True,
) -> None:
    api = ControllerApi(service, config)
    server = make_server(api, config, host=host, port=port, session_id=session_id)
    if announce:
        # Printed to stdout (not stderr) so an operator or launcher can discover
        # the actual port when ``--port 0`` was requested.
        bound_host, bound_port = server.server_address[0], server.server_address[1]
        print(f"runner controller API listening at http://{bound_host}:{bound_port}/v1", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
