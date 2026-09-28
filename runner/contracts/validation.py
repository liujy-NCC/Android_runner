"""Minimal declarative JSON schema validation for the v1 request contracts.

The design requires unknown *required* fields and unsupported versions to be
rejected explicitly, and it forbids leaking internals through error text. This
module therefore produces terse, positional error strings that never include
request values.
"""

from __future__ import annotations

import re
from typing import Any

from runner.errors import ProtocolError

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{20,128}$")
_ID_RE = re.compile(r"^[a-z]{2,4}_[0-9A-HJKMNP-TV-Z]{26}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

MAX_BODY_BYTES = 262_144  # 256 KiB request body cap
MAX_CANDIDATE_FILES = 64
MAX_CANDIDATE_BYTES = 262_144


class Field:
    """One validated request field."""

    __slots__ = ("name", "kind", "required", "enum", "pattern", "maximum", "minimum", "item_enum")

    def __init__(
        self,
        name: str,
        kind: str,
        *,
        required: bool = False,
        enum: tuple[str, ...] | None = None,
        pattern: re.Pattern[str] | None = None,
        maximum: int | None = None,
        minimum: int | None = None,
        item_enum: tuple[str, ...] | None = None,
    ) -> None:
        self.name = name
        self.kind = kind
        self.required = required
        self.enum = enum
        self.pattern = pattern
        self.maximum = maximum
        self.minimum = minimum
        self.item_enum = item_enum


def validate_payload(payload: Any, fields: list[Field], *, where: str) -> dict[str, Any]:
    """Validate ``payload`` against ``fields``; reject unknown keys.

    Unknown keys are rejected rather than ignored: the design requires unknown
    or unsupported fields to be refused explicitly instead of silently
    widening the accepted contract.
    """

    if not isinstance(payload, dict):
        raise ProtocolError("malformed_request", detail=f"{where}: body must be an object")

    allowed = {f.name for f in fields}
    for key in payload:
        if not isinstance(key, str) or key not in allowed:
            raise ProtocolError("malformed_request", detail=f"{where}: unexpected field")

    out: dict[str, Any] = {}
    for field in fields:
        if field.name not in payload or payload[field.name] is None:
            if field.required:
                raise ProtocolError("malformed_request", detail=f"{where}: missing {field.name}")
            continue
        value = payload[field.name]
        _check_kind(field, value, where)
        out[field.name] = value
    return out


def _check_kind(field: Field, value: Any, where: str) -> None:
    name = field.name
    if field.kind == "string":
        if not isinstance(value, str):
            raise ProtocolError("malformed_request", detail=f"{where}: {name} must be a string")
        if field.pattern is not None and not field.pattern.match(value):
            raise ProtocolError("malformed_request", detail=f"{where}: {name} has invalid form")
    elif field.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProtocolError("malformed_request", detail=f"{where}: {name} must be an integer")
    elif field.kind == "bool":
        if not isinstance(value, bool):
            raise ProtocolError("malformed_request", detail=f"{where}: {name} must be a boolean")
    elif field.kind == "object":
        if not isinstance(value, dict):
            raise ProtocolError("malformed_request", detail=f"{where}: {name} must be an object")
    elif field.kind == "list":
        if not isinstance(value, list):
            raise ProtocolError("malformed_request", detail=f"{where}: {name} must be a list")
        if field.item_enum is not None:
            for item in value:
                if not isinstance(item, str) or item not in field.item_enum:
                    raise ProtocolError(
                        "malformed_request", detail=f"{where}: {name} contains an unsupported value"
                    )
    elif field.kind == "map_of_string":
        if not isinstance(value, dict):
            raise ProtocolError("malformed_request", detail=f"{where}: {name} must be an object")
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise ProtocolError(
                    "malformed_request", detail=f"{where}: {name} must map strings to strings"
                )
    else:  # pragma: no cover - programming error
        raise ProtocolError("internal_error", detail=f"{where}: unknown field kind")
    if field.enum is not None and value not in field.enum:
        raise ProtocolError("malformed_request", detail=f"{where}: {name} not in allowed set")
    if field.maximum is not None and isinstance(value, (int, str)) and len(value) > field.maximum:
        raise ProtocolError("malformed_request", detail=f"{where}: {name} exceeds maximum")
    if field.minimum is not None and isinstance(value, (int, str)) and len(value) < field.minimum:
        raise ProtocolError("malformed_request", detail=f"{where}: {name} below minimum")


IDEMPOTENCY_FIELD = Field("idempotency_key", "string", required=True, pattern=_IDEMPOTENCY_RE)
SESSION_ID_FIELD = Field("session_id", "string", required=True, pattern=_ID_RE)
SESSION_ID_OPTIONAL_FIELD = Field("session_id", "string", required=False, pattern=_ID_RE)
ATTEMPT_ID_FIELD = Field("attempt_id", "string", required=True, pattern=_ID_RE)
TASK_ID_FIELD = Field("task_id", "string", required=True, pattern=_ID_RE)
CANDIDATE_ID_FIELD = Field("candidate_id", "string", required=True, pattern=_ID_RE)
BUILD_ID_FIELD = Field("build_id", "string", required=True, pattern=_ID_RE)
SHA256_FIELD_PATTERN = _SHA256_RE


def is_valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))
