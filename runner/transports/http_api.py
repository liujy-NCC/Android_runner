"""Restricted HTTP/JSON controller API.

The transport only converts requests to core calls and projects responses. All
authentication, state transitions, budget handling and evidence recording stay
in the core service.

There is deliberately NO endpoint for: generic shell, PowerShell, ADB, emulator
console, arbitrary file access, arbitrary Intent/component launch, device
enumeration, or plugin loading. Session provisioning is a local administrative
action and has no route here.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from runner import API_PREFIX, SCHEMA_VERSION, SERVICE_VERSION
from runner.config import SessionConfig
from runner.contracts.validation import Field, IDEMPOTENCY_FIELD, is_valid_id, validate_payload
from runner.core.service import RunnerService
from runner.errors import PROTOCOL_MESSAGES, BusinessRejection, ProtocolError, RunnerError

# Endpoint table. Each entry declares whether the method is payload-describing
# (needs an idempotency key) and which credential scope it requires.
ROUTES: tuple[dict[str, Any], ...] = (
    {"method": "GET", "pattern": r"^/health$", "scope": None, "handler": "_health"},
    {"method": "GET", "pattern": r"^/capabilities$", "scope": "get_capabilities", "handler": "_capabilities"},
    {"method": "GET", "pattern": r"^/session$", "scope": "get_session", "handler": "_session"},
    {"method": "GET", "pattern": r"^/operations/(?P<operation_id>[A-Za-z0-9_]+)$", "scope": "get_operation",
     "handler": "_operation"},
    {"method": "POST", "pattern": r"^/operations/(?P<operation_id>[A-Za-z0-9_]+)/cancel$",
     "scope": "cancel_operation", "handler": "_cancel_operation", "idempotent": True},
    {"method": "POST", "pattern": r"^/candidates$", "scope": "submit_candidate",
     "handler": "_submit_candidate", "idempotent": True},
    {"method": "POST", "pattern": r"^/builds$", "scope": "build_candidate",
     "handler": "_build_candidate", "idempotent": True},
    {"method": "POST", "pattern": r"^/attempts$", "scope": "begin_attempt",
     "handler": "_begin_attempt", "idempotent": True},
    {"method": "GET", "pattern": r"^/attempts/(?P<attempt_id>[A-Za-z0-9_]+)$", "scope": "get_attempt",
     "handler": "_get_attempt"},
    {"method": "GET", "pattern": r"^/sessions/(?P<session_id>[A-Za-z0-9_]+)/attempts$",
     "scope": "get_attempt", "handler": "_list_attempts"},
    {"method": "POST", "pattern": r"^/attempts/(?P<attempt_id>[A-Za-z0-9_]+)/actions$",
     "scope": "perform_action", "handler": "_perform_action", "idempotent": True},
    {"method": "POST", "pattern": r"^/attempts/(?P<attempt_id>[A-Za-z0-9_]+)/finish$",
     "scope": "finish_attempt", "handler": "_finish_attempt", "idempotent": True},
    {"method": "POST", "pattern": r"^/attempts/(?P<attempt_id>[A-Za-z0-9_]+)/abort$",
     "scope": "abort_attempt", "handler": "_abort_attempt", "idempotent": True},
    {"method": "GET", "pattern": r"^/attempts/(?P<attempt_id>[A-Za-z0-9_]+)/tasks$", "scope": "list_tasks",
     "handler": "_list_tasks"},
    {"method": "GET", "pattern": r"^/tasks/(?P<task_id>[A-Za-z0-9_]+)$", "scope": "get_task",
     "handler": "_get_task"},
    {"method": "POST", "pattern": r"^/tasks/(?P<task_id>[A-Za-z0-9_]+)/wait$", "scope": "wait_task",
     "handler": "_wait_task", "idempotent": True},
    {"method": "POST", "pattern": r"^/tasks/(?P<task_id>[A-Za-z0-9_]+)/stop$", "scope": "stop_task",
     "handler": "_stop_task", "idempotent": True},
    {"method": "GET", "pattern": r"^/feedback$", "scope": "read_feedback", "handler": "_read_feedback"},
    {"method": "POST", "pattern": r"^/finalize$", "scope": "finalize_candidate",
     "handler": "_finalize_candidate", "idempotent": True},
    {"method": "POST", "pattern": r"^/session/close$", "scope": "close_session",
     "handler": "_close_session", "idempotent": True},
)

_COMPILED = tuple((r, re.compile(r["pattern"])) for r in ROUTES)

# Paths that must never exist, with the reason recorded in the rejection ledger.
FORBIDDEN_PREFIXES = (
    "exec", "shell", "powershell", "adb", "emulator", "devices", "files", "fs",
    "plugins", "config", "admin", "oracle", "evidence", "artifacts", "raw",
    "logcat", "sqlite", "db", "sessions", "install", "uninstall", "intent",
)


class Request:
    """A parsed request, ready to be dispatched to a core method."""

    __slots__ = ("method", "path", "query", "body", "raw_body", "token", "implicit_session_id")

    def __init__(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        body: dict[str, Any],
        raw_body: bytes,
        token: str | None,
        implicit_session_id: str | None = None,
    ) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self.raw_body = raw_body
        self.token = token
        self.implicit_session_id = implicit_session_id


class Response:
    __slots__ = ("status", "body", "truncated")

    def __init__(self, status: int, body: dict[str, Any], truncated: bool = False) -> None:
        self.status = status
        self.body = body
        self.truncated = truncated

    def serialize(self) -> bytes:
        return json.dumps(self.body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


class ControllerApi:
    """Dispatcher. Framework-agnostic so it can be unit tested without sockets."""

    def __init__(self, service: RunnerService, config: SessionConfig) -> None:
        self.service = service
        self.config = config

    # -- dispatch ------------------------------------------------------------
    def dispatch(self, request: Request) -> Response:
        try:
            return self._dispatch(request)
        except ProtocolError as exc:
            self._reject(request, error_code=exc.code, detail=exc.detail)
            return Response(exc.http_status, exc.to_body())
        except BusinessRejection as exc:
            self._reject(request, error_code="reason_code", detail=exc.detail)
            return Response(200, {"status": "rejected", "attempt_id": None, "reason_code": exc.reason_code})
        except RunnerError as exc:
            self._reject(request, error_code=exc.code, detail=None)
            return Response(
                PROTOCOL_ERROR_STATUS_FALLBACK,
                {"error": {"code": "internal_error", "message": PROTOCOL_MESSAGES["internal_error"]}},
            )
        except Exception:
            self._reject(request, error_code="internal_error", detail=None)
            return Response(
                PROTOCOL_ERROR_STATUS_FALLBACK,
                {"error": {"code": "internal_error", "message": PROTOCOL_MESSAGES["internal_error"]}},
            )

    def _dispatch(self, request: Request) -> Response:
        route, match = self._match(request)
        scope = route["scope"]

        # Unauthenticated liveness probe: no credential, no session, no body.
        if scope is None:
            return getattr(self, route["handler"])(request, "", "", match.groupdict())

        # Credential presence is checked before any body/session parsing so an
        # anonymous caller always receives the same stable 401.
        token = request.token
        if not token:
            raise ProtocolError("unauthenticated")

        if route.get("idempotent"):
            self._require_idempotency_key(request)
        session_id = self._session_id(request)
        # The credential is authoritative; the body's session_id (when present)
        # is only a consistency check. Inject it so handlers see one value.
        if isinstance(request.body, dict) and "session_id" not in request.body:
            request.body["session_id"] = session_id

        handler: Callable[..., Response] = getattr(self, route["handler"])
        return handler(request, session_id, token, match.groupdict())

    # -- routing -------------------------------------------------------------
    def _match(self, request: Request) -> tuple[dict[str, Any], re.Match[str]]:
        path = request.path
        if path.startswith(API_PREFIX):
            path = path[len(API_PREFIX):]
        if not path.startswith("/"):
            path = "/" + path

        first = path.strip("/").split("/")[0].lower()
        if first in FORBIDDEN_PREFIXES:
            # No generic execution, filesystem, device or plugin surface exists.
            raise ProtocolError("not_found", detail=f"no such endpoint class: {first}")

        method_mismatch = False
        for route, compiled in _COMPILED:
            match = compiled.match(path)
            if match is None:
                continue
            groupdict = match.groupdict()
            # A path segment that carries an object id must parse as one. This
            # keeps a verb-like suffix (e.g. /v1/attempts/retry) a 404 rather
            # than an object lookup that could later become a new verb.
            if any(not is_valid_id(value) for value in groupdict.values()):
                continue
            if route["method"] != request.method:
                method_mismatch = True
                continue
            return route, match
        if method_mismatch:
            raise ProtocolError("method_not_allowed")
        raise ProtocolError("not_found")

    def _require_idempotency_key(self, request: Request) -> None:
        body = request.body
        key = body.get("idempotency_key") if isinstance(body, dict) else None
        if not isinstance(key, str) or not key:
            raise ProtocolError("malformed_request", detail="idempotency_key is required")
        import re as _re

        if not _re.match(r"^[A-Za-z0-9._:-]{20,128}$", key):
            raise ProtocolError("malformed_request", detail="idempotency_key has invalid form")

    def _session_id(self, request: Request) -> str:
        session_id = request.implicit_session_id
        body_session = None
        if isinstance(request.body, dict):
            body_session = request.body.get("session_id")
        query_session = request.query.get("session_id")
        declared = body_session or query_session
        if session_id and declared and declared != session_id:
            raise ProtocolError("session_mismatch", detail="session_id does not match credential")
        resolved = session_id or declared
        if not resolved:
            raise ProtocolError("malformed_request", detail="session_id is required")
        return str(resolved)

    def _reject(self, request: Request, *, error_code: str, detail: str | None) -> None:
        session_id = request.implicit_session_id
        try:
            self.service._record_rejection(
                session_id, scope=f"{request.method} {request.path}", error_code=error_code, detail=detail
            )
        except Exception:
            pass

    # -- handlers ------------------------------------------------------------
    def _health(self, request, session_id, token, params) -> Response:
        return Response(200, {"status": "ok", "service_version": SERVICE_VERSION, "schema_version": SCHEMA_VERSION})

    def _capabilities(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "get_capabilities")
        return Response(200, self.service.get_capabilities(session_id))

    def _session(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "get_session")
        return Response(200, self.service.get_session(session_id))

    def _operation(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "get_operation")
        return Response(200, self.service.get_operation(session_id, params["operation_id"]))

    def _cancel_operation(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "cancel_operation")
        body = self._body(request, [])
        return Response(200, self.service.cancel_operation(session_id, params["operation_id"]))

    def _submit_candidate(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "submit_candidate")
        body = self._body(
            request,
            [
                IDEMPOTENCY_FIELD,
                Field("session_id", "string", required=True),
                Field("files", "list", required=True),
            ],
        )
        result = self.service.submit_candidate(
            session_id,
            idempotency_key=body["idempotency_key"],
            files_payload=body["files"],
        )
        return Response(200, result)

    def _build_candidate(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "build_candidate")
        body = self._body(
            request,
            [
                IDEMPOTENCY_FIELD,
                Field("session_id", "string", required=True),
                Field("candidate_id", "string", required=True),
            ],
        )
        result = self.service.build_candidate(
            session_id,
            idempotency_key=body["idempotency_key"],
            candidate_id=body["candidate_id"],
        )
        return Response(200, result)

    def _begin_attempt(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "begin_attempt")
        body = self._body(
            request,
            [
                IDEMPOTENCY_FIELD,
                Field("session_id", "string", required=True),
                Field("build_id", "string", required=True),
            ],
        )
        result = self.service.begin_attempt(
            session_id, idempotency_key=body["idempotency_key"], build_id=body["build_id"]
        )
        # The Agent-visible business body is exactly three fields.
        projected = {
            "status": result["status"],
            "attempt_id": result.get("attempt_id"),
            "reason_code": result.get("reason_code"),
        }
        return Response(200, projected)

    def _get_attempt(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "get_attempt")
        attempt = self.service.get_attempt(session_id, params["attempt_id"])
        wait_for = request.query.get("wait_for_status_version")
        if wait_for is not None:
            import time as _time

            try:
                target = int(wait_for)
            except ValueError:
                raise ProtocolError("malformed_request", detail="wait_for_status_version must be an integer")
            deadline = _time.monotonic() + min(
                float(self.config.limits.get("status_wait_seconds_max", 20)), 20.0
            )
            while attempt["status_version"] <= target and _time.monotonic() < deadline:
                _time.sleep(0.01)
                attempt = self.service.get_attempt(session_id, params["attempt_id"])

        page = self._fit_status_page(session_id, attempt)
        return Response(200, page, truncated=bool(page.get("truncated")))

    def _fit_status_page(self, session_id: str, attempt: dict[str, Any]) -> dict[str, Any]:
        """Serialize under 8 KiB and charge the read against the session quota."""

        max_bytes = int(self.config.feedback_limits["get_attempt_response_bytes_max"])
        page = dict(attempt)
        page["truncated"] = False
        raw = json.dumps(page, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

        if len(raw) > max_bytes:
            # Shrink optional control metadata first; the seven stages, health
            # and remaining budget are the mandatory content.
            page["truncated"] = True
            page["truncation_reason"] = "STATUS_RESPONSE_BYTES_LIMIT"
            raw = json.dumps(page, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
            while len(raw) > max_bytes and page.get("remaining_budget"):
                page["remaining_budget"] = {}
                raw = json.dumps(page, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

        reason = self.service.charge_status_read(session_id, delivered_bytes=len(raw))
        if reason is not None:
            return {
                "truncated": True,
                "truncation_reason": reason,
                "error": {"code": "permission_denied"},
            }
        return page

    def _list_attempts(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "get_attempt")
        return Response(200, self.service.list_attempts(session_id))

    def _perform_action(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "perform_action")
        body = self._body(
            request,
            [
                IDEMPOTENCY_FIELD,
                Field("session_id", "string", required=True),
                Field("action_name", "string", required=True),
                Field("params", "object", required=False),
            ],
        )
        result = self.service.perform_action(
            session_id,
            params["attempt_id"],
            idempotency_key=body["idempotency_key"],
            action_name=body["action_name"],
            params=body.get("params"),
        )
        return Response(200, result)

    def _finish_attempt(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "finish_attempt")
        self._body(request, [IDEMPOTENCY_FIELD, Field("session_id", "string", required=True)])
        return Response(200, self.service.finish_attempt(session_id, params["attempt_id"]))

    def _abort_attempt(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "abort_attempt")
        self._body(request, [IDEMPOTENCY_FIELD, Field("session_id", "string", required=True)])
        return Response(200, self.service.abort_attempt(session_id, params["attempt_id"]))

    def _list_tasks(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "list_tasks")
        return Response(200, self.service.list_tasks(session_id, params["attempt_id"]))

    def _get_task(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "get_task")
        return Response(200, self.service.get_task(session_id, params["task_id"]))

    def _wait_task(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "wait_task")
        body = self._body(
            request,
            [
                IDEMPOTENCY_FIELD,
                Field("session_id", "string", required=True),
                Field("condition", "string", required=True, enum=("ready", "exited")),
                Field("timeout_seconds", "int", required=False),
            ],
        )
        return Response(
            200,
            self.service.wait_task(
                session_id,
                params["task_id"],
                condition=body["condition"],
                timeout_seconds=float(body.get("timeout_seconds", 1)),
            ),
        )

    def _stop_task(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "stop_task")
        self._body(request, [IDEMPOTENCY_FIELD, Field("session_id", "string", required=True)])
        return Response(200, self.service.stop_task(session_id, params["task_id"]))

    def _read_feedback(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "read_feedback")
        candidate_id = request.query.get("candidate_id")
        kind = request.query.get("kind", "runtime")
        if not candidate_id:
            raise ProtocolError("malformed_request", detail="candidate_id is required")
        cursor_raw = request.query.get("cursor")
        cursor = None
        if cursor_raw:
            try:
                cursor = int(cursor_raw)
            except ValueError:
                raise ProtocolError("malformed_request", detail="cursor must be an integer")
        result = self.service.read_feedback(
            session_id,
            candidate_id=candidate_id,
            kind=kind,
            build_id=request.query.get("build_id"),
            attempt_id=request.query.get("attempt_id"),
            cursor=cursor,
        )
        return Response(200, result, truncated=bool(result.get("truncated")))

    def _finalize_candidate(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "finalize_candidate")
        body = self._body(
            request,
            [
                IDEMPOTENCY_FIELD,
                Field("session_id", "string", required=True),
                Field("candidate_id", "string", required=True),
                Field("build_id", "string", required=True),
            ],
        )
        return Response(
            200,
            self.service.finalize_candidate(
                session_id,
                idempotency_key=body["idempotency_key"],
                candidate_id=body["candidate_id"],
                build_id=body["build_id"],
            ),
        )

    def _close_session(self, request, session_id, token, params) -> Response:
        self._auth(session_id, token, "close_session")
        body = self._body(
            request,
            [
                IDEMPOTENCY_FIELD,
                Field("session_id", "string", required=True),
                Field("reason", "string", required=False),
            ],
        )
        return Response(
            200,
            self.service.close_session(
                session_id,
                idempotency_key=body["idempotency_key"],
                reason=body.get("reason", "agent_requested"),
            ),
        )

    # -- helpers -------------------------------------------------------------
    def _auth(self, session_id: str, token: str, method: str) -> None:
        record = self.service.sessions.authenticate(session_id, token)
        self.service.sessions.authorize(record, method)
        self.service.sessions.touch(session_id)

    def _body(self, request: Request, fields: list[Field]) -> dict[str, Any]:
        return validate_payload(request.body, fields, where=request.path)


PROTOCOL_ERROR_STATUS_FALLBACK = 500
