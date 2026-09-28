"""Agent-visible feedback projection (``diagnostic-only-v1``).

This module is the *only* place that decides what the Agent may see. It is
auditable on purpose: every projection function is a whitelist, and anything
that cannot be reliably attributed or sanitized is dropped and flagged with
``diagnostics_incomplete``.

Invariants enforced here (DESIGN.md §7, AUDIT-v0.6.md):
  * no ``oracle_verdict`` / observation results / repaired-state results
  * no ASUS identity, no fixed viewer identity, no URI, no Intent content,
    no non-candidate UID
  * no raw full logcat
  * build errors: candidate-relative path + line + column + sanitized summary
  * runtime logs: same attempt AND candidate package AND candidate actual UID
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from runner.adapters.base import BuildError, DiagnosticEntry, ProjectionResult
from runner.adapters.collectors_mock import SAFE_SYSTEM_EVENT_TYPES

# ---------------------------------------------------------------------------
# Denylist: private identifiers and evidence shapes that must never survive.
# ---------------------------------------------------------------------------
_FORBIDDEN_SUBSTRINGS = (
    "com.asus",
    "asus",
    "action_view",
    "android.intent.action.view",
    "fixed.viewer",
    "viewer",
    "commandservice",
    "oracle",
    "verdict",
    "repaired",
    "exported=false",
)

_FORBIDDEN_FIELD_TOKENS = (
    "asus",
    "viewer",
    "uri",
    "url",
    "intent",
    "extras",
    "action_view",
    "verdict",
    "oracle",
    "observation",
    "non_candidate_uid",
    "private",
)

_URI_RE = re.compile(r"\b(?:https?|content|file|android-app)://\S+", re.IGNORECASE)
_INTENT_RE = re.compile(r"\b(?:act|dat|cmp|flg|extras)=", re.IGNORECASE)
_ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\[^\s]+|/(?:home|Users|tmp|var|opt|root|srv)/)\S*")
_UID_IN_TEXT_RE = re.compile(r"\buid[=: ]+(\d{3,7})\b", re.IGNORECASE)
_CMD_RE = re.compile(r"\b(?:javac|gradle|aapt2?|adb|cmd\.exe|powershell|d8|dx)\b\s", re.IGNORECASE)

# A UID is required to be in this range to count as an ordinary application UID.
_APP_UID_MIN = 10000
_APP_UID_MAX = 19999

_SAFE_SUMMARY_FALLBACK = "candidate source failed to compile"


class DiagnosticOnlyProjector:
    """Whitelisting projector implementing the ``diagnostic-only-v1`` profile."""

    name = "diagnostic-only-v1"
    version = "1"

    # -- build diagnostics ---------------------------------------------------
    def project_build_errors(
        self,
        *,
        candidate_id: str,
        relative_root: str,
        errors: Iterable[BuildError],
        limit: int,
    ) -> ProjectionResult:
        entries: list[DiagnosticEntry] = []
        incomplete = False
        ordinal = 0
        for error in errors:
            ordinal += 1
            rel = _safe_relative_path(error.relative_path)
            summary, changed = _sanitize_summary(error.sanitized_summary)
            if rel is None:
                # Cannot attribute the error to a candidate-relative path.
                incomplete = True
                ordinal -= 1
                continue
            if changed:
                incomplete = True
            entries.append(
                DiagnosticEntry(
                    kind="build_error",
                    ordinal=ordinal,
                    payload={
                        "relative_candidate_source_path": rel,
                        "line": int(error.line),
                        "column": int(error.column),
                        "sanitized_summary": summary,
                    },
                )
            )
            if len(entries) >= limit:
                break
        return ProjectionResult(entries=entries, diagnostics_incomplete=incomplete)

    # -- runtime diagnostics -------------------------------------------------
    def project_runtime_logs(
        self,
        *,
        candidate_id: str,
        attempt_id: str,
        package_name: str,
        actual_uid: int,
        raw_records: Iterable[dict[str, Any]],
        limit: int,
    ) -> ProjectionResult:
        entries: list[DiagnosticEntry] = []
        incomplete = False
        ordinal = 0
        for raw in raw_records:
            ordinal += 1
            record_attempt = raw.get("attempt_id")
            if record_attempt is not None and record_attempt != attempt_id:
                # Runtime diagnostics must belong to this candidate's own attempt.
                incomplete = True
                continue
            source = str(raw.get("source", ""))
            if source == "system_event":
                cleaned = self.sanitize_system_event(raw, candidate_uid=actual_uid)
                if cleaned is None:
                    incomplete = True
                    continue
                entries.append(
                    DiagnosticEntry(kind="system_event", ordinal=ordinal, payload=cleaned)
                )
            else:
                cleaned = self._project_candidate_line(raw, package_name=package_name, actual_uid=actual_uid)
                if cleaned is None:
                    # Not attributable to the candidate package+uid, or it could
                    # not be sanitized: omit and mark the projection incomplete.
                    incomplete = True
                    continue
                entries.append(DiagnosticEntry(kind="candidate_log", ordinal=ordinal, payload=cleaned))
            if len(entries) >= limit:
                break
        return ProjectionResult(entries=entries, diagnostics_incomplete=incomplete)

    def _project_candidate_line(
        self, raw: dict[str, Any], *, package_name: str, actual_uid: int
    ) -> dict[str, Any] | None:
        if _has_forbidden_field(raw):
            return None
        pkg = raw.get("package")
        uid = raw.get("uid")
        if pkg != package_name:
            return None
        if not isinstance(uid, int) or uid != actual_uid:
            return None
        message = raw.get("message")
        if not isinstance(message, str):
            return None
        message = _sanitize_text(message)
        if message is None:
            return None
        return {
            "level": _safe_level(raw.get("level")),
            "message": message,
        }

    # -- system events -------------------------------------------------------
    def sanitize_system_event(
        self, raw: dict[str, Any], *, candidate_uid: int | None
    ) -> dict[str, Any] | None:
        """Convert a raw system record into a structured safe event.

        Returns ``None`` when the event cannot be reliably attributed to the
        candidate or cannot be sanitized. ``None`` never carries a reason: the
        caller only records ``diagnostics_incomplete=true``, which must not
        reveal the kind of omitted content.
        """

        if raw.get("source") != "system_event":
            return None
        event_type = raw.get("event_type")
        if event_type not in SAFE_SYSTEM_EVENT_TYPES:
            return None
        if candidate_uid is None:
            return None
        pkg = raw.get("package")
        uid = raw.get("uid")
        if not isinstance(uid, int) or uid != candidate_uid:
            return None
        if not _is_app_uid(uid):
            return None
        if not isinstance(pkg, str) or not pkg:
            return None
        # Any private marker anywhere in the record disqualifies it entirely.
        if _has_forbidden_field(raw):
            return None
        message = raw.get("message")
        safe_message = _sanitize_text(message) if isinstance(message, str) else None
        if message is not None and safe_message is None:
            return None
        payload: dict[str, Any] = {
            "event_type": event_type,
            "candidate_package": pkg,
            "candidate_uid": uid,
        }
        if safe_message:
            payload["message"] = safe_message
        return payload


def _has_forbidden_field(record: dict[str, Any]) -> bool:
    for key in record:
        lowered = str(key).lower()
        for token in _FORBIDDEN_FIELD_TOKENS:
            if token in lowered:
                return True
    return False


def _is_app_uid(uid: int) -> bool:
    return _APP_UID_MIN <= uid <= _APP_UID_MAX


def _safe_level(value: Any) -> str:
    if isinstance(value, str) and value.upper() in ("V", "D", "I", "W", "E", "F"):
        return value.upper()
    return "I"


def _sanitize_text(text: str) -> str | None:
    """Sanitize one free-text message. ``None`` means "drop the record"."""

    lowered = text.lower()
    for token in _FORBIDDEN_SUBSTRINGS:
        if token in lowered:
            return None
    if _URI_RE.search(text) or _INTENT_RE.search(text):
        return None
    if _ABS_PATH_RE.search(text):
        return None
    if _CMD_RE.search(text):
        return None
    match = _UID_IN_TEXT_RE.search(text)
    if match is not None:
        return None
    clipped = text.replace("\r", " ").replace("\n", " ").strip()
    if len(clipped) > 512:
        clipped = clipped[:512] + "..."
    return clipped or None


def _sanitize_summary(text: str) -> tuple[str, bool]:
    """Sanitize a build error summary, keeping it useful for locating errors."""

    original = text or ""
    cleaned = original.replace("\r", " ").replace("\n", " ").strip()
    changed = False
    if _ABS_PATH_RE.search(cleaned):
        cleaned = _ABS_PATH_RE.sub("<path>", cleaned)
        changed = True
    if _CMD_RE.search(cleaned):
        cleaned = _CMD_RE.sub("<tool> ", cleaned)
        changed = True
    lowered = cleaned.lower()
    if any(token in lowered for token in _FORBIDDEN_SUBSTRINGS):
        return _SAFE_SUMMARY_FALLBACK, True
    if _URI_RE.search(cleaned) or _INTENT_RE.search(cleaned):
        return _SAFE_SUMMARY_FALLBACK, True
    if len(cleaned) > 200:
        cleaned = cleaned[:200] + "..."
        changed = True
    return (cleaned or _SAFE_SUMMARY_FALLBACK), changed


def _safe_relative_path(raw: str) -> str | None:
    """Return a candidate-relative path, or ``None`` when not attributable."""

    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.replace("\\", "/").strip()
    if text.startswith("/") or text.startswith("//"):
        return None
    if re.match(r"^[A-Za-z]:", text):
        return None
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    joined = "/".join(parts)
    lowered = joined.lower()
    if any(token in lowered for token in _FORBIDDEN_SUBSTRINGS):
        return None
    if not lowered.endswith((".java", ".txt", ".json", ".md", ".kt")):
        return None
    return joined[:240]
