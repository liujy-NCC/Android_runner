"""Build backends: mock (deterministic) and the NOT-integrated real position."""

from __future__ import annotations

from typing import Any

from runner.adapters.base import BuildOutcome, BuildRequest
from runner.adapters.profiles import get_profile
from runner.errors import CapabilityError


class MockBuildBackend:
    """Deterministic, side-effect-free builder used by the mock end-to-end run.

    Scenario knobs let tests force a build failure with a *positioned* error so
    that `read_feedback` build diagnostics can be exercised without an attempt.
    """

    name = "mock"
    version = "1"

    def __init__(self, *, fail: bool = False, failure_file: str | None = None) -> None:
        self.fail = fail
        self.failure_file = failure_file
        self.calls: list[str] = []

    def build(self, request: BuildRequest, *, limits: dict[str, int]) -> BuildOutcome:
        self.calls.append(request.candidate_id)
        profile = get_profile(request.build_profile)
        outcome = profile.build(request, limits=limits)  # type: ignore[attr-defined]
        if not self.fail:
            return outcome

        from runner.adapters.base import BuildError

        target = self.failure_file or "src/main/java/org/androidpoc/and000008/candidate/CandidateBehavior.java"
        # Only the candidate-relative path, line, column and a sanitized summary
        # are produced: no command line, no absolute path, no template internals.
        return BuildOutcome(
            ok=False,
            exit_code=1,
            log_text="mock compile failure (sanitized)\n",
            errors=[
                BuildError(target, 12, 5, "cannot find symbol: unresolved behaviour hook", "compile_error"),
                BuildError(target, 27, 9, "incompatible types in statement", "compile_error"),
            ],
            failure_reason_code="BUILD_FAILED",
            template_id=request.build_profile,
            toolchain_id="mock-offline-toolchain",
        )

    def cancel(self, request: BuildRequest) -> None:  # pragma: no cover - no async work
        return None


class NotIntegratedBuildBackend:
    """Position for the real templated toolchain. Refuses to pretend."""

    name = "and000008-template"
    version = "0"

    def __init__(self, reason: str = "real Android build toolchain is not integrated") -> None:
        self.reason = reason

    def build(self, request: BuildRequest, *, limits: dict[str, int]) -> BuildOutcome:  # pragma: no cover
        raise CapabilityError(self.reason)

    def cancel(self, request: BuildRequest) -> None:  # pragma: no cover
        return None


def get_build_backend(name: str) -> Any:
    if name == MockBuildBackend.name:
        return MockBuildBackend()
    if name == NotIntegratedBuildBackend.name:
        return NotIntegratedBuildBackend()
    raise CapabilityError(f"build backend not registered: {name}")
