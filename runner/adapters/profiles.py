"""Build profiles: candidate source validation and (mock) isolated build.

`and000008-java-v1` implements *input validation and the interface position
only*. Per AUDIT-v0.6.md the `CandidateBehavior` method signature must NOT be
frozen from the archived FGS APK, so this profile deliberately validates
nothing about method names or signatures — it only checks path policy, package
declaration, extension and the presence of the required behaviour source file.

The real templated Android build toolchain is NOT integrated: the profile's
build step produces a deterministic stand-in artifact and records
``build_provenance`` accordingly. It must never be presented as a real APK.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from typing import Iterable, Sequence

from runner.adapters.base import (
    BuildError,
    BuildOutcome,
    BuildRequest,
    SourceFile,
    SourcePolicyResult,
)

# ---- path policy ----------------------------------------------------------
JAVA_SOURCE_ROOT = "src/main/java/org/androidpoc/and000008/candidate/"
JAVA_NAMESPACE = "org.androidpoc.and000008.candidate"
REQUIRED_BEHAVIOUR_SOURCE = "CandidateBehavior.java"
PATH_POLICY_VERSION = "and000008-java-paths-v1"

_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)\s*;", re.MULTILINE)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")

FORBIDDEN_SUFFIXES = (
    ".xml",
    ".gradle",
    ".gradle.kts",
    ".kts",
    ".jar",
    ".aar",
    ".apk",
    ".so",
    ".a",
    ".zip",
    ".properties",
    ".toml",
    ".lock",
    ".png",
    ".jpg",
    ".webp",
    ".ttf",
    ".keystore",
    ".jks",
    ".p12",
    ".pem",
)

FORBIDDEN_BASENAMES = (
    "androidmanifest.xml",
    "gradlew",
    "gradlew.bat",
    "gradle-wrapper.properties",
    "settings.gradle",
    "settings.gradle.kts",
    "build.gradle",
    "build.gradle.kts",
    "proguard-rules.pro",
    "libs.versions.toml",
)

FORBIDDEN_PATH_SEGMENTS = (
    "gradle/wrapper",
    "src/main/resources",
    "src/main/assets",
    "src/main/jni",
    "src/main/jniLibs",
    "src/main/res",
    "src/test",
    "src/androidTest",
    "META-INF",
)


def _is_forbidden_relpath(rel: str) -> str | None:
    """Return a stable rejection token when ``rel`` is outside the profile."""

    lower = rel.lower()
    base = posixpath.basename(lower)
    if base in FORBIDDEN_BASENAMES:
        return "template_or_build_script"
    for segment in FORBIDDEN_PATH_SEGMENTS:
        if lower == segment.lower() or lower.startswith(segment.lower() + "/"):
            return "unapproved_directory"
    for suffix in FORBIDDEN_SUFFIXES:
        if lower.endswith(suffix):
            return "unapproved_file_type"
    return None


def normalize_relative_path(raw: str) -> tuple[str | None, str | None]:
    """Normalize and sanity-check a submitted relative path.

    Returns ``(normalized, rejection_token)``. Exactly one of them is set.
    Rejects traversal, absolute paths, Windows junctions/UNC, and drive-relative
    paths before any filesystem or manifest work happens.
    """

    if not raw or not isinstance(raw, str):
        return None, "empty_path"
    if "\x00" in raw:
        return None, "illegal_character"
    candidate = raw.replace("\\", "/").strip()
    if not candidate:
        return None, "empty_path"
    if candidate.startswith("/") or candidate.startswith("//"):
        return None, "absolute_path"
    if _WINDOWS_DRIVE_RE.match(candidate):
        return None, "absolute_path"
    if candidate.startswith("~"):
        return None, "absolute_path"
    parts = [p for p in candidate.split("/")]
    if any(p in ("..",) for p in parts):
        return None, "path_traversal"
    if any(p == "." for p in parts):
        return None, "path_traversal"
    parts = [p for p in parts if p != ""]
    if not parts:
        return None, "empty_path"
    normalized = "/".join(parts)
    if normalized.startswith("/") or ".." in normalized.split("/"):
        return None, "path_traversal"
    return normalized, None


def compute_tree_hash(manifest: Iterable[tuple[str, str]]) -> str:
    """SHA-256 over the sorted (relative path, file sha256) manifest."""

    lines = sorted(f"{rel}\0{sha}" for rel, sha in manifest)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _manifest_sha256(manifest: list[dict[str, object]]) -> str:
    payload = "\n".join(
        f"{m['relative_path']}\0{m['sha256']}\0{m['size_bytes']}" for m in manifest
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class GenericSourceProfile:
    """Permissive profile used by the mock end-to-end runs.

    Language-agnostic (any text source file), but it keeps exactly the same
    freeze discipline as the strict profile: traversal, absolute paths,
    duplicate and case-conflicting paths, oversized files, binary blobs and
    build scripts / prebuilt artifacts are all refused.
    """

    name = "mock-generic-v1"
    version = "1"
    path_policy_version = "generic-paths-v1"

    def validate_source(self, files: Sequence[SourceFile], *, limits: dict[str, int]) -> SourcePolicyResult:
        return _validate_common(
            files,
            limits=limits,
            name=self.name,
            allow=self._allow,
        )

    @staticmethod
    def _allow(rel: str) -> tuple[bool, str | None]:
        token = _is_forbidden_relpath(rel)
        if token is not None:
            return False, token
        if posixpath.basename(rel).startswith("."):
            return False, "hidden_file_not_permitted"
        return True, None

    def build(self, request: BuildRequest, *, limits: dict[str, int]) -> BuildOutcome:
        return _mock_build(request, profile=self.name)

    def cancel(self, request: BuildRequest) -> None:  # pragma: no cover - no async work
        return None


class And000008JavaProfile:
    """`and000008-java-v1`: fixed namespace/path Java-only candidate input.

    NOT a real Android build. The templated toolchain and signed APK production
    remain un-integrated; the returned artifact is an explicitly-labelled
    stand-in so that no test can mistake it for a real APK.
    """

    name = "and000008-java-v1"
    version = "1"
    path_policy_version = PATH_POLICY_VERSION

    def validate_source(self, files: Sequence[SourceFile], *, limits: dict[str, int]) -> SourcePolicyResult:
        return _validate_common(
            files,
            limits=limits,
            name=self.name,
            allow=self._allow,
            require=[JAVA_SOURCE_ROOT + REQUIRED_BEHAVIOUR_SOURCE],
        )

    def _allow(self, rel: str) -> tuple[bool, str | None]:
        lowered = rel.lower()
        if lowered.startswith(JAVA_SOURCE_ROOT.lower()):
            tail = rel[len(JAVA_SOURCE_ROOT) :]
            if not tail:
                return False, "empty_path"
            if not lowered.endswith(".java"):
                token = _is_forbidden_relpath(rel)
                return False, token or "unapproved_file_type"
            return True, None
        token = _is_forbidden_relpath(rel)
        if token is not None:
            return False, token
        return False, "outside_candidate_source_root"

    def build(self, request: BuildRequest, *, limits: dict[str, int]) -> BuildOutcome:
        # The behaviour entry signature is intentionally unchecked: the zero
        # permission / single Activity positive control is not yet validated in
        # the historical environment, so no signature may be frozen here.
        return _mock_build(request, profile=self.name)

    def cancel(self, request: BuildRequest) -> None:  # pragma: no cover
        return None


def _validate_common(
    files: Sequence[SourceFile],
    *,
    limits: dict[str, int],
    name: str,
    allow,
    require: Sequence[str] = (),
) -> SourcePolicyResult:
    errors: list[BuildError] = []
    max_files = int(limits.get("max_candidate_files", 64))
    max_bytes = int(limits.get("max_candidate_bytes", 262144))

    if not files:
        return SourcePolicyResult(
            accepted=False,
            reason_code="SOURCE_POLICY_REJECTED",
            errors=[BuildError("", 0, 0, "candidate submission is empty", "empty_submission")],
        )
    if len(files) > max_files:
        return SourcePolicyResult(
            accepted=False,
            reason_code="SOURCE_POLICY_REJECTED",
            errors=[
                BuildError("", 0, 0, f"too many files: limit is {max_files}", "too_many_files")
            ],
        )

    seen: dict[str, str] = {}  # normalized path -> original
    folded: dict[str, str] = {}  # casefolded path -> normalized
    manifest: list[dict[str, object]] = []
    total = 0

    for source in files:
        normalized, reject = normalize_relative_path(source.relative_path)
        if reject is not None:
            errors.append(
                BuildError(
                    _safe_relpath_for_error(source.relative_path),
                    0,
                    0,
                    f"path rejected: {reject}",
                    reject,
                )
            )
            continue
        assert normalized is not None
        if normalized in seen:
            errors.append(
                BuildError(normalized, 0, 0, "duplicate path in submission", "duplicate_path")
            )
            continue
        key = normalized.casefold()
        if key in folded:
            errors.append(
                BuildError(normalized, 0, 0, "path conflicts with another entry by case", "case_conflict")
            )
            continue
        seen[normalized] = source.relative_path
        folded[key] = normalized

        allowed, token = allow(normalized)
        if not allowed:
            errors.append(
                BuildError(normalized, 0, 0, f"file not permitted by {name}: {token}", token or "not_allowed")
            )
            continue

        size = len(source.data)
        if size > max_bytes:
            errors.append(
                BuildError(normalized, 0, 0, f"file exceeds size limit ({max_bytes} bytes)", "file_too_large")
            )
            continue
        total += size
        if total > max_bytes:
            errors.append(
                BuildError(normalized, 0, 0, "submission exceeds total size limit", "submission_too_large")
            )
            continue
        if b"\x00" in source.data[:4096]:
            errors.append(BuildError(normalized, 0, 0, "binary content is not accepted", "binary_content"))
            continue

        if normalized.endswith(".java"):
            error = _check_java_package(normalized, source.data, name)
            if error is not None:
                errors.append(error)

        manifest.append(
            {
                "relative_path": normalized,
                "size_bytes": size,
                "sha256": hashlib.sha256(source.data).hexdigest(),
            }
        )

    for required in require:
        if required not in seen:
            errors.append(
                BuildError(required, 0, 0, "required behaviour source is missing", "required_source_missing")
            )

    if errors:
        return SourcePolicyResult(
            accepted=False,
            reason_code="SOURCE_POLICY_REJECTED",
            errors=errors,
        )

    manifest.sort(key=lambda m: str(m["relative_path"]))
    tree_hash = compute_tree_hash((str(m["relative_path"]), str(m["sha256"])) for m in manifest)
    return SourcePolicyResult(
        accepted=True,
        tree_hash=tree_hash,
        manifest=manifest,
        manifest_sha256=_manifest_sha256(manifest),
        source_bytes=total,
    )


def _check_java_package(rel: str, data: bytes, profile_name: str) -> BuildError | None:
    if profile_name != And000008JavaProfile.name:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return BuildError(rel, 0, 0, "source is not valid UTF-8", "not_utf8")
    match = _PACKAGE_RE.search(text)
    if match is None:
        return BuildError(rel, 0, 0, "missing package declaration", "missing_package")
    declared = match.group(1)
    if declared != JAVA_NAMESPACE and not declared.startswith(JAVA_NAMESPACE + "."):
        return BuildError(
            rel,
            text[: match.start()].count("\n") + 1,
            1,
            "package declaration is outside the permitted namespace",
            "package_not_permitted",
        )
    return None


def _safe_relpath_for_error(raw: str) -> str:
    """Never echo absolute paths back inside a diagnostic."""

    if not isinstance(raw, str):
        return ""
    text = raw.replace("\\", "/")
    if text.startswith("/") or _WINDOWS_DRIVE_RE.match(text) or text.startswith("//"):
        return "<rejected-absolute-path>"
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if ".." in parts:
        return "<rejected-traversal-path>"
    return "/".join(parts)[:200]


def _mock_build(request: BuildRequest, *, profile: str) -> BuildOutcome:
    """Deterministic stand-in build used only by the mock backend.

    The output is a labelled text object, not an APK. ``build_provenance`` is
    recorded as ``mock_build_not_a_real_apk`` by the caller via
    ``environment_fingerprint``/``failure_reason_code`` handling.
    """

    payload = [
        f"runner-mock-build-profile={profile}",
        f"candidate_id={request.candidate_id}",
        f"tree_hash={request.tree_hash}",
        f"application_id={request.application_id}",
        "artifact_class=stand-in (NOT a real APK)",
    ]
    blob = ("\n".join(payload) + "\n").encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()
    return BuildOutcome(
        ok=True,
        exit_code=0,
        log_text=f"mock build ok for {request.tree_hash}\n",
        apk_bytes=blob,
        apk_sha256=digest,
        package_name=request.application_id,
        version_code=1,
        signer_fingerprint="mock-signer:" + hashlib.sha256(b"runner-test-key-v1").hexdigest()[:32],
        manifest_sha256=hashlib.sha256(b"mock-merged-manifest").hexdigest(),
        permissions=[],
        components=[
            {
                "type": "activity",
                "name": f"{request.application_id}.CandidateActivity",
                "exported": True,
                "main_launcher": True,
            }
        ],
        queries=["com.asus.aihome"] if profile == And000008JavaProfile.name else [],
        entry_component=f"{request.application_id}.CandidateActivity",
        application_id=request.application_id,
        template_id=profile,
        template_sha256=hashlib.sha256(f"template::{profile}".encode()).hexdigest(),
        toolchain_id="mock-offline-toolchain",
        toolchain_fingerprint=hashlib.sha256(b"mock-toolchain").hexdigest(),
        dependency_lock_sha256=hashlib.sha256(b"mock-dependency-lock").hexdigest(),
        signing_profile="runner-test-key-v1",
        environment_fingerprint=hashlib.sha256(b"mock-build-environment").hexdigest(),
    )


def get_profile(name: str) -> object:
    if name == And000008JavaProfile.name:
        return And000008JavaProfile()
    if name == GenericSourceProfile.name:
        return GenericSourceProfile()
    from runner.errors import CapabilityError

    raise CapabilityError(f"build profile not registered: {name}")
