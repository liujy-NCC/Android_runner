"""Shared error types and the stable Agent-visible reason-code whitelist."""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Control-stage reason codes. Agent-visible. This is a CLOSED set: no code may
# express PoC/Oracle outcome, ASUS/viewer behaviour, or "agent may retry".
# ---------------------------------------------------------------------------
STAGE_REASON_CODES: tuple[str, ...] = (
    "REQUEST_REJECTED",
    "SOURCE_POLICY_REJECTED",
    "BUILD_FAILED",
    "INSTALL_FAILED",
    "IDENTITY_CHECK_FAILED",
    "LAUNCH_TIMEOUT",
    "CANDIDATE_CRASHED",
    "CANDIDATE_ANR",
    "DEVICE_UNAVAILABLE",
    "EVIDENCE_COLLECTION_INCOMPLETE",
    "CLEANUP_FAILED",
    "BUDGET_EXHAUSTED",
)

# Read-quota metadata. Distinct namespace from stage reason codes on purpose:
# truncation only reports read budget, never the existence of private evidence.
TRUNCATION_REASONS: tuple[str, ...] = (
    "STATUS_RESPONSE_BYTES_LIMIT",
    "STATUS_CALL_LIMIT",
    "DIAGNOSTIC_PAGE_BYTES_LIMIT",
    "DIAGNOSTIC_CANDIDATE_PAGE_LIMIT",
    "DIAGNOSTIC_CANDIDATE_BYTES_LIMIT",
    "DIAGNOSTIC_SESSION_BYTES_LIMIT",
    "DIAGNOSTIC_READ_BUDGET_EXHAUSTED",
)

# Protocol-level (pre-business) error codes with fixed HTTP status.
PROTOCOL_ERROR_STATUS: dict[str, int] = {
    "malformed_request": 400,
    "unauthenticated": 401,
    "session_mismatch": 403,
    "permission_denied": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "idempotency_conflict": 409,
    "environment_mismatch": 409,
    "payload_too_large": 413,
    "unsupported_capability": 422,
    "rate_limited": 429,
    "internal_error": 500,
}

# Fixed, sanitized protocol messages. Never derived from internal state.
PROTOCOL_MESSAGES: dict[str, str] = {
    "malformed_request": "request body or query is not well formed",
    "unauthenticated": "missing or invalid session credential",
    "session_mismatch": "request session does not match credential",
    "permission_denied": "method not permitted for this session",
    "not_found": "no such endpoint or object",
    "method_not_allowed": "HTTP method not allowed",
    "idempotency_conflict": "idempotency key already used with a different request",
    "environment_mismatch": "current environment does not match the session binding",
    "payload_too_large": "request payload exceeds the allowed size",
    "unsupported_capability": "capability is not installed, probed or authorized",
    "rate_limited": "too many rejected requests",
    "internal_error": "internal error",
}


class RunnerError(Exception):
    """Base class for all Runner failures."""

    code = "internal_error"


class ProtocolError(RunnerError):
    """Pre-business rejection: returned as a restricted protocol error."""

    def __init__(self, code: str, *, detail: str | None = None) -> None:
        if code not in PROTOCOL_ERROR_STATUS:
            code = "internal_error"
        super().__init__(PROTOCOL_MESSAGES.get(code, "internal error"))
        self.code = code
        # detail is for the private rejection ledger only; never sent to Agent.
        self.detail = detail

    @property
    def http_status(self) -> int:
        return PROTOCOL_ERROR_STATUS[self.code]

    def to_body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": PROTOCOL_MESSAGES[self.code]}}


class BusinessRejection(RunnerError):
    """Post-parse rejection carrying an Agent-visible control-stage reason code.

    Used only where the method's contract returns a domain receipt
    (e.g. ``begin_attempt`` -> status=rejected + reason_code).
    """

    def __init__(self, reason_code: str, *, detail: str | None = None) -> None:
        if reason_code not in STAGE_REASON_CODES:
            reason_code = "REQUEST_REJECTED"
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.detail = detail


class BudgetExhausted(BusinessRejection):
    def __init__(self, counter_key: str, *, detail: str | None = None) -> None:
        super().__init__("BUDGET_EXHAUSTED", detail=detail or f"counter exhausted: {counter_key}")
        self.counter_key = counter_key


class StorageError(RunnerError):
    """Content-addressed storage failure (hash mismatch, missing object, ...)."""

    code = "storage_error"

    def __init__(self, message: str, *, artifact_id: str | None = None) -> None:
        super().__init__(message)
        self.artifact_id = artifact_id


class StateError(RunnerError):
    """State machine violation detected internally (never a generic 500 leak)."""

    code = "state_error"


class CapabilityError(RunnerError):
    code = "unsupported_capability"
