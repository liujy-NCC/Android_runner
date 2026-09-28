"""Replaceable component interfaces (DESIGN.md §4.1).

These are responsibility contracts, not process boundaries. Every adapter gets
an explicit input/output shape, a declared version, and a declared set of
capabilities. Business flow depends only on these interfaces, never on a
device model or sample id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, Sequence, runtime_checkable


# ---------------------------------------------------------------------------
# Source / build
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SourceFile:
    relative_path: str
    data: bytes


@dataclass(frozen=True)
class BuildError:
    """Sanitized build diagnostic: relative path, position, summary only."""

    relative_path: str
    line: int
    column: int
    sanitized_summary: str
    code: str = "compile_error"

    def to_json(self) -> dict[str, Any]:
        return {
            "relative_candidate_source_path": self.relative_path,
            "line": self.line,
            "column": self.column,
            "sanitized_summary": self.sanitized_summary,
        }


@dataclass
class SourcePolicyResult:
    accepted: bool
    reason_code: str = ""
    errors: list[BuildError] = field(default_factory=list)
    tree_hash: str = ""
    manifest: list[dict[str, Any]] = field(default_factory=list)
    manifest_sha256: str = ""
    source_bytes: int = 0

    def summary_lines(self) -> list[str]:
        return [f"{e.relative_path}:{e.line}:{e.column}: {e.sanitized_summary}" for e in self.errors]


@dataclass
class BuildOutcome:
    ok: bool
    exit_code: int = 0
    log_text: str = ""
    errors: list[BuildError] = field(default_factory=list)
    apk_bytes: bytes | None = None
    apk_sha256: str | None = None
    package_name: str | None = None
    version_code: int | None = None
    signer_fingerprint: str | None = None
    manifest_sha256: str | None = None
    permissions: list[str] = field(default_factory=list)
    components: list[dict[str, Any]] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    entry_component: str | None = None
    application_id: str | None = None
    template_id: str = "unknown"
    template_sha256: str = ""
    toolchain_id: str = "unknown"
    toolchain_fingerprint: str = ""
    dependency_lock_sha256: str = ""
    signing_profile: str = "unknown"
    environment_fingerprint: str = ""
    failure_reason_code: str = "BUILD_FAILED"
    manifest_rejection_reason: str = ""


@dataclass
class BuildRequest:
    candidate_id: str
    build_profile: str
    tree_hash: str
    files: Sequence[SourceFile]
    application_id: str
    isolation_token: str


@runtime_checkable
class BuildProfile(Protocol):
    """Validates and freezes candidate input, then builds it in isolation."""

    name: str
    version: str

    def validate_source(self, files: Sequence[SourceFile], *, limits: dict[str, int]) -> SourcePolicyResult: ...

    def build(self, request: BuildRequest, *, limits: dict[str, int]) -> BuildOutcome: ...

    def cancel(self, request: BuildRequest) -> None: ...


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------
@dataclass
class DeviceInfo:
    device_id: str
    boot_id: str
    android_release: str = "unknown"
    api_level: int = 0
    abi: str = "unknown"
    build_id: str = "unknown"
    image_sha256: str = "unknown"
    health: str = "unknown"
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass
class InstallRecord:
    name: str
    package_name: str
    apk_sha256: str
    ok: bool
    actual_uid: int | None = None
    version_code: int | None = None
    signer_fingerprint: str | None = None
    permissions: list[str] = field(default_factory=list)
    reason_code: str = ""


@dataclass
class LaunchDispatch:
    dispatched_monotonic: float
    dispatched_utc: str
    command_token: str
    accepted: bool = True


@dataclass
class LaunchConfirmation:
    confirmed: bool
    confirmed_monotonic: float | None = None
    confirmed_utc: str | None = None
    evidence_kind: str = ""
    reason_code: str = ""


@runtime_checkable
class DeviceAdapter(Protocol):
    """Owns the device. Agent text never reaches this layer as a command."""

    name: str
    version: str

    def probe(self, resource_role: str) -> DeviceInfo: ...

    def reset_baseline(self, resource_role: str) -> DeviceInfo: ...

    def install_fixture(self, resource_role: str, name: str, package_name: str, apk: bytes) -> InstallRecord: ...

    def install_candidate(self, resource_role: str, package_name: str, apk: bytes) -> InstallRecord: ...

    def verify_install(self, resource_role: str, package_name: str, expected: dict[str, Any]) -> InstallRecord: ...

    def start_collectors(self, resource_role: str, required: Sequence[str]) -> dict[str, Any]: ...

    def launch_candidate(self, resource_role: str, package_name: str, entry_component: str) -> LaunchDispatch: ...

    def confirm_launch(
        self, resource_role: str, dispatch: LaunchDispatch, *, deadline_monotonic: float, now_monotonic: float
    ) -> LaunchConfirmation: ...

    def stop_package(self, resource_role: str, package_name: str) -> bool: ...

    def restore(self, resource_role: str) -> DeviceInfo: ...

    def health(self, resource_role: str) -> str: ...


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------
@dataclass
class CollectorChunk:
    """A byte range of a collector's append-only on-disk stream.

    Chunks reference a file range rather than carrying bytes in memory: the
    design requires collectors to stream to disk and to seal in chunks instead
    of accumulating unbounded logs, and it lets a restarted supervisor seal
    evidence it did not itself observe.
    """

    label: str
    file_path: str
    start_offset: int
    end_offset: int
    phase: str = "observe"
    occurred_utc: str = ""
    within_observation_window: bool = True

    @property
    def size(self) -> int:
        return max(0, self.end_offset - self.start_offset)


@dataclass
class CollectorHandle:
    task_id: str
    required: bool
    ready: bool
    readiness: str = "PENDING"
    reason_code: str = ""
    started_monotonic: float = 0.0
    started_utc: str = ""
    stream_path: str = ""


@dataclass
class CollectorCheckpoint:
    handle_id: str
    chunks: list[CollectorChunk] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = True
    stream_path: str = ""


@runtime_checkable
class Collector(Protocol):
    name: str
    version: str
    required_by_default: bool

    def start(self, *, attempt_id: str, device: DeviceInfo, session_id: str) -> CollectorHandle: ...

    def checkpoint(self, *, handle_id: str, window_open: bool) -> CollectorCheckpoint: ...

    def stop(self, *, handle_id: str) -> CollectorCheckpoint: ...


# ---------------------------------------------------------------------------
# Oracle (PRIVATE)
# ---------------------------------------------------------------------------
@dataclass
class SealedEvidence:
    artifact_id: str
    kind: str
    sha256: str
    data: bytes
    attempt_id: str | None = None


@dataclass
class OracleVerdict:
    """PRIVATE result. Never projected to the Agent through any interface."""

    verdict: str
    evaluation_status: str
    oracle_id: str
    oracle_version: str
    criteria_version: str
    observations: dict[str, str] = field(default_factory=dict)
    reason_private: str = ""
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)


@runtime_checkable
class OracleAdapter(Protocol):
    """Reads sealed evidence only, and never executes candidate scripts."""

    oracle_id: str
    version: str
    criteria_version: str

    def evaluate(self, evidence: Sequence[SealedEvidence], *, environment_valid: bool) -> OracleVerdict: ...


# ---------------------------------------------------------------------------
# Feedback projection
# ---------------------------------------------------------------------------
@dataclass
class DiagnosticEntry:
    kind: str
    ordinal: int
    payload: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "ordinal": self.ordinal, **self.payload}


@dataclass
class ProjectionResult:
    entries: list[DiagnosticEntry]
    diagnostics_incomplete: bool = False
    truncated: bool = False
    truncation_reason: str | None = None


@runtime_checkable
class FeedbackProjector(Protocol):
    """Whitelists what the Agent may see. Private evidence never passes here."""

    name: str
    version: str

    def project_build_errors(
        self,
        *,
        candidate_id: str,
        relative_root: str,
        errors: Iterable[BuildError],
        limit: int,
    ) -> ProjectionResult: ...

    def project_runtime_logs(
        self,
        *,
        candidate_id: str,
        attempt_id: str,
        package_name: str,
        actual_uid: int,
        raw_records: Iterable[dict[str, Any]],
        limit: int,
    ) -> ProjectionResult: ...

    def sanitize_system_event(self, raw: dict[str, Any], *, candidate_uid: int | None) -> dict[str, Any] | None: ...
