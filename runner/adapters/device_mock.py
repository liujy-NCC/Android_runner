"""Mock device adapter: deterministic, offline, no ADB, no emulator.

The mock owns a single abstract resource role. It never shells out, never
touches the network, and derives every value deterministically from the attempt
inputs so that acceptance runs are reproducible. Scenario knobs let tests drive
specific failure paths (baseline failure, install failure, launch never
confirmed, restore failure).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Sequence

from runner.adapters.base import (
    DeviceInfo,
    InstallRecord,
    LaunchConfirmation,
    LaunchDispatch,
)

MOCK_ANDROID_RELEASE = "13"
MOCK_API_LEVEL = 33
MOCK_ABI = "x86_64"
MOCK_BUILD_ID = "TE1A.220922.034"
MOCK_IMAGE_SHA256 = hashlib.sha256(b"mock-system-image").hexdigest()


def _uid_for(package_name: str) -> int:
    """Deterministic non-fixture UID in the ordinary-app range."""

    digest = hashlib.sha256(package_name.encode("utf-8")).digest()
    return 10000 + (int.from_bytes(digest[:2], "big") % 5000)


@dataclass
class MockDeviceState:
    boot_generation: int = 0
    installed: dict[str, InstallRecord] = field(default_factory=dict)
    baseline_verified: bool = False
    candidate_launch_count: int = 0
    stopped_packages: list[str] = field(default_factory=list)
    collectors_started: list[str] = field(default_factory=list)


class MockDeviceAdapter:
    name = "mock"
    version = "1"

    def __init__(
        self,
        *,
        baseline_fails: bool = False,
        install_fails: bool = False,
        launch_never_confirms: bool = False,
        launch_confirms_immediately: bool = True,
        restore_fails: bool = False,
        identity_mismatch: bool = False,
        device_id: str = "mock-device-1",
    ) -> None:
        self.baseline_fails = baseline_fails
        self.install_fails = install_fails
        self.launch_never_confirms = launch_never_confirms
        self.launch_confirms_immediately = launch_confirms_immediately
        self.restore_fails = restore_fails
        self.identity_mismatch = identity_mismatch
        self.device_id = device_id
        self.state = MockDeviceState()
        self.health_state = "ready"
        self.quarantined = False

    # -- probe / baseline ----------------------------------------------------
    def _info(self) -> DeviceInfo:
        return DeviceInfo(
            device_id=self.device_id,
            boot_id=f"mock-boot-{self.state.boot_generation:04d}",
            android_release=MOCK_ANDROID_RELEASE,
            api_level=MOCK_API_LEVEL,
            abi=MOCK_ABI,
            build_id=MOCK_BUILD_ID,
            image_sha256=MOCK_IMAGE_SHA256,
            health=self.health_state,
            capabilities={
                "cold_boot_wipe_data": True,
                "install_fixtures": True,
                "structured_system_events": True,
                "resource_hard_limits": False,
            },
        )

    def probe(self, resource_role: str) -> DeviceInfo:
        return self._info()

    def reset_baseline(self, resource_role: str) -> DeviceInfo:
        # A quarantined device stays quarantined: a failed restore means the
        # previous attempt's state cannot be trusted, so the next attempt must
        # fail preparation rather than silently reuse it.
        if self.quarantined:
            self.health_state = "unavailable"
            return self._info()
        # Every attempt starts from a wiped-data cold boot: a fresh boot
        # generation, no carried-over installed packages.
        self.state.boot_generation += 1
        self.state.installed.clear()
        self.state.baseline_verified = not self.baseline_fails
        self.health_state = "unavailable" if self.baseline_fails else "ready"
        return self._info()

    # -- install -------------------------------------------------------------
    def install_fixture(
        self, resource_role: str, name: str, package_name: str, apk: bytes
    ) -> InstallRecord:
        return self._install(name, package_name, apk)

    def install_candidate(self, resource_role: str, package_name: str, apk: bytes) -> InstallRecord:
        return self._install("candidate", package_name, apk)

    def _install(self, name: str, package_name: str, apk: bytes) -> InstallRecord:
        digest = hashlib.sha256(apk).hexdigest()
        if self.install_fails:
            record = InstallRecord(
                name=name,
                package_name=package_name,
                apk_sha256=digest,
                ok=False,
                reason_code="INSTALL_FAILED",
            )
            self.state.installed[package_name] = record
            return record
        record = InstallRecord(
            name=name,
            package_name=package_name,
            apk_sha256=digest,
            ok=True,
            actual_uid=_uid_for(package_name) + (7 if self.identity_mismatch else 0),
            version_code=1,
            signer_fingerprint="mock-signer:"
            + hashlib.sha256(b"runner-test-key-v1").hexdigest()[:32],
            permissions=[],
        )
        self.state.installed[package_name] = record
        return record

    def verify_install(
        self, resource_role: str, package_name: str, expected: dict[str, Any]
    ) -> InstallRecord:
        record = self.state.installed.get(package_name)
        if record is None:
            return InstallRecord(
                name="verify",
                package_name=package_name,
                apk_sha256="",
                ok=False,
                reason_code="INSTALL_FAILED",
            )
        mismatch = False
        for key in ("apk_sha256", "version_code", "signer_fingerprint"):
            want = expected.get(key)
            if want is None:
                continue
            if getattr(record, key) != want:
                mismatch = True
        if self.identity_mismatch:
            mismatch = True
        if mismatch:
            return InstallRecord(
                name="verify",
                package_name=package_name,
                apk_sha256=record.apk_sha256,
                ok=False,
                actual_uid=record.actual_uid,
                version_code=record.version_code,
                signer_fingerprint=record.signer_fingerprint,
                permissions=record.permissions,
                reason_code="IDENTITY_CHECK_FAILED",
            )
        return record

    def actual_uid(self, package_name: str) -> int | None:
        record = self.state.installed.get(package_name)
        return record.actual_uid if record else None

    # -- collectors ----------------------------------------------------------
    def start_collectors(self, resource_role: str, required: Sequence[str]) -> dict[str, Any]:
        self.state.collectors_started = list(required)
        return {"ready": True, "started": list(required), "missing": [], "device_boot_id": self._info().boot_id}

    # -- launch --------------------------------------------------------------
    def launch_candidate(
        self, resource_role: str, package_name: str, entry_component: str
    ) -> LaunchDispatch:
        self.state.candidate_launch_count += 1
        return LaunchDispatch(
            dispatched_monotonic=0.0,  # filled by core with the real host monotonic
            dispatched_utc="",
            command_token=hashlib.sha256(
                f"{package_name}:{entry_component}:{self.state.candidate_launch_count}".encode()
            ).hexdigest()[:16],
            accepted=True,
        )

    def confirm_launch(
        self,
        resource_role: str,
        dispatch: LaunchDispatch,
        *,
        deadline_monotonic: float,
        now_monotonic: float,
    ) -> LaunchConfirmation:
        if self.launch_never_confirms or now_monotonic > deadline_monotonic:
            return LaunchConfirmation(
                confirmed=False,
                evidence_kind="approved_activity_start_v1",
                reason_code="LAUNCH_TIMEOUT",
            )
        if not self.launch_confirms_immediately:
            # Confirmation arrives later in the window; the supervisor performs
            # a second poll and confirms then.
            if now_monotonic >= dispatch.dispatched_monotonic + 2.0:
                return LaunchConfirmation(
                    confirmed=True,
                    confirmed_monotonic=now_monotonic,
                    evidence_kind="approved_activity_start_v1",
                )
            return LaunchConfirmation(
                confirmed=False,
                evidence_kind="approved_activity_start_v1",
                reason_code="LAUNCH_TIMEOUT",
            )
        return LaunchConfirmation(
            confirmed=True,
            confirmed_monotonic=max(now_monotonic, dispatch.dispatched_monotonic),
            evidence_kind="approved_activity_start_v1",
        )

    # -- stop / restore ------------------------------------------------------
    def stop_package(self, resource_role: str, package_name: str) -> bool:
        self.state.stopped_packages.append(package_name)
        return True

    def restore(self, resource_role: str) -> DeviceInfo:
        if self.restore_fails:
            self.health_state = "unavailable"
            self.quarantined = True
            return self._info()
        self.health_state = "ready"
        return self._info()

    def health(self, resource_role: str) -> str:
        return self.health_state

    def quarantine(self) -> None:
        self.quarantined = True
        self.health_state = "unavailable"
