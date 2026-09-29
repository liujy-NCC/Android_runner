"""HTTP socket binding for the controller API.

DESIGN.md §10.2 requires the controller API to be reachable only on a controlled
interface and explicitly rejects binding ``0.0.0.0`` while relying on an API key.
The policy enforced here is therefore:

* the default is loopback only;
* a wildcard address (``0.0.0.0``, ``::``, an empty host or a ``*``) is ALWAYS
  refused, even when the operator opts in -- a wildcard bind exposes the
  controller on every interface, which no flag may authorise;
* opting in only permits one explicitly named private interface address
  (RFC1918 / RFC4193 unique-local / link-local). Public addresses are refused so
  the controller can never be exposed to the internet by a flag toggle.

Network isolation itself (host firewall rules, Docker internal gateway, ADB
forwarding) is NOT implemented or verified in this mock stage; see
IMPLEMENTATION-CONTRACT.md §12.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import socketserver
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from runner import API_PREFIX, SCHEMA_VERSION
from runner.config import SessionConfig
from runner.core.service import RunnerService
from runner.errors import ProtocolError
from runner.transports.http_api import ControllerApi, Request, Response

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

# Always refused, with or without an operator opt-in: these bind every interface.
WILDCARD_HOSTS = ("0.0.0.0", "::", "*", "0", "")

# Upper bound on how much of a *rejected* request body is drained before the
# connection is closed. Bounds the work one oversized request can cost.
MAX_DRAIN_BYTES = 8 * 1024 * 1024

# Total wall-clock budget for draining a rejected request body. A byte cap alone
# is not enough: it bounds how much is read but never how long, so a peer that
# simply stops sending leaves the worker blocked in read() forever. This is a
# TOTAL deadline for the whole drain -- not a per-read timeout -- because a
# peer that drips one byte at a time would otherwise reset a per-read timeout
# indefinitely and hold the worker just as effectively.
MAX_DRAIN_SECONDS = 2.0

# Per-read inactivity timeout for the connection. This bounds the *ordinary*
# read paths too (request line, headers, and an under-cap body): without it a
# peer that announces a body and never sends it blocks a worker thread
# indefinitely, which is the same defect as an unbounded drain. Generous
# relative to the 256 KiB body cap, so no legitimate upload is affected.
REQUEST_READ_TIMEOUT_SECONDS = 30.0


class BindPolicyError(ValueError):
    """Raised at startup when a bind address would violate the interface policy."""


def _is_private_address(host: str) -> bool:
    """True for a single private/unique-local/link-local interface address."""

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    # ``is_private`` covers RFC1918 and RFC4193 unique-local; link-local covers
    # the 169.254/fe80 ranges. Loopback is handled separately before this call.
    return bool(address.is_private or address.is_link_local)


def validate_bind_address(host: str, config: SessionConfig) -> None:
    """Enforce the controller bind policy, raising :class:`BindPolicyError`.

    Kept separate from :func:`make_server` so the API process can refuse to start
    *before* any socket exists, and so the policy is directly testable.
    """

    if host is None or host in WILDCARD_HOSTS:
        raise BindPolicyError(
            f"refusing to bind the wildcard address {host!r}: a wildcard bind exposes the "
            "controller on every interface, which no configuration may authorise "
            "(DESIGN.md §10.2)"
        )
    if host in LOOPBACK_HOSTS or host.lower() == "localhost":
        return
    if not config.limits.get("allow_non_loopback_bind"):
        raise BindPolicyError(
            f"refusing a non-loopback bind ({host!r}) without an explicit operator opt-in"
        )
    if not _is_private_address(host):
        raise BindPolicyError(
            f"refusing a non-loopback bind on {host!r}: only a single private interface "
            "address may be used, never a public address or a hostname"
        )


class _Handler(BaseHTTPRequestHandler):
    server_version = "RunnerController/1"
    protocol_version = "HTTP/1.1"

    # Applied by socketserver.StreamRequestHandler.setup() to every accepted
    # connection, so a peer that stalls at ANY point (request line, headers, or
    # a declared-but-never-sent body) cannot pin a worker thread forever. The
    # stdlib already discards the connection on TimeoutError.
    timeout = REQUEST_READ_TIMEOUT_SECONDS

    # Effective per-connection read bound; ``setup`` may lower it from config.
    read_timeout_seconds: float = REQUEST_READ_TIMEOUT_SECONDS

    def setup(self) -> None:
        super().setup()
        # Effective read bound for this connection. It is the idle timeout for a
        # single read AND the total budget for receiving a request body, so a
        # peer that keeps dribbling cannot hold the worker past it (see
        # ``_read_body``).
        self.read_timeout_seconds = REQUEST_READ_TIMEOUT_SECONDS
        # Honour an operator/test override without adding a default key, so the
        # canonical config hash of existing sessions is unchanged. A non-finite
        # or non-positive value is ignored rather than disabling the bound: this
        # timeout is a liveness guard, so it must not be switched off by a typo.
        override = self.config.limits.get("request_read_timeout_seconds")
        if override:
            try:
                seconds = float(override)
            except (TypeError, ValueError):
                return
            if seconds > 0 and seconds != float("inf"):
                self.read_timeout_seconds = seconds
                self.connection.settimeout(seconds)

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
            # Reject WITHOUT reading the body, but do not simply drop the socket:
            # the peer is usually still writing, and closing with unread data in
            # the receive buffer makes the OS send an RST, so the client sees a
            # connection abort instead of this 413. Discard the announced body
            # (bounded) so its write can finish, and close afterwards so the
            # abandoned bytes are never parsed as a second request.
            self.close_connection = True
            self._discard_body(length)
            self._send(
                Response(
                    413,
                    {"error": {"code": "payload_too_large",
                               "message": "request payload exceeds the allowed size"}},
                )
            )
            return
        raw = self._read_body(length) if length else b""
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

    def _read_body(self, length: int) -> bytes:
        """Read an accepted (under-cap) request body under a total deadline.

        The connection's idle timeout is NOT sufficient here, for the same reason
        the drain needed a deadline: ``BufferedReader.read(n)`` loops inside the
        buffer until ``n`` bytes arrive, so a peer that keeps dribbling bytes
        makes every low-level read succeed. The idle timeout never fires and
        ``read(n)`` never returns, pinning the worker well past that timeout
        (measured: still blocked after 12s with a 2s idle timeout).

        Reading with ``read1`` re-checks the deadline every round and bounds each
        blocking call by the remaining budget. A peer that dribbles is therefore
        cut off at ``REQUEST_READ_TIMEOUT_SECONDS`` of *total* body time rather
        than holding the thread for as long as it cares to keep sending.
        """

        deadline = time.monotonic() + self.read_timeout_seconds
        previous_timeout = self.connection.gettimeout()
        read_once = getattr(self.rfile, "read1", None) or self.rfile.read
        chunks: list[bytes] = []
        remaining = length
        try:
            while remaining > 0:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    raise TimeoutError("request body was not received within the read deadline")
                self.connection.settimeout(budget)
                chunk = read_once(min(remaining, 65536))
                if not chunk:
                    raise TimeoutError("request body ended before Content-Length was satisfied")
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            try:
                self.connection.settimeout(previous_timeout)
            except OSError:  # pragma: no cover - socket already gone
                pass
        return b"".join(chunks)

    def _discard_body(self, length: int) -> None:
        """Drain a rejected request body so the peer can finish writing.

        Both a byte cap AND a total wall-clock deadline are required:

        * ``length`` is attacker-controlled, so the byte cap stops one request
          from draining unbounded data;
        * the byte cap alone does not bound *time*: a peer may announce a large
          body and then send nothing, so the deadline stops it holding a worker
          thread. The deadline is for the whole drain, not each read, so a peer
          that keeps dribbling bytes cannot extend it.

        The drain reads with ``read1``, which performs AT MOST ONE underlying
        socket read per call. Plain ``read(n)`` loops inside the buffer object
        until ``n`` bytes arrive, so a peer dribbling one byte at a time keeps
        every low-level read succeeding: the socket never times out, ``read``
        never returns, and the deadline below is never re-checked. That made the
        "total" budget bypassable by exactly the drip it was meant to stop.

        Timeouts are always converted into "stop draining"; the caller sends its
        413 either way and closes the connection.
        """

        if length <= 0:
            return

        deadline = time.monotonic() + MAX_DRAIN_SECONDS
        previous_timeout = self.connection.gettimeout()
        remaining = min(length, MAX_DRAIN_BYTES)
        # ``read1`` is on BufferedReader; fall back to ``read`` only for a raw
        # stream, whose read() also performs a single underlying read.
        read_once = getattr(self.rfile, "read1", None) or self.rfile.read
        try:
            while remaining > 0:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    break
                # Never block past the remaining total budget.
                self.connection.settimeout(budget)
                try:
                    chunk = read_once(min(remaining, 65536))
                except (TimeoutError, socket.timeout):
                    break
                except OSError:
                    return
                if not chunk:
                    return
                remaining -= len(chunk)
        finally:
            try:
                self.connection.settimeout(previous_timeout)
            except OSError:  # pragma: no cover - socket already gone
                pass

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
    # Refuse at startup, before a socket exists: a policy violation is a
    # deployment error, not an HTTP response.
    validate_bind_address(host, config)
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
