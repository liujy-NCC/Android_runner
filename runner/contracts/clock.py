"""Clocks: UTC wall clock plus a host monotonic clock.

Design §4.4 requires events to carry both UTC and monotonic time; monotonic
values are only comparable within one node boot, so the node boot id is
recorded alongside.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def iso_from_epoch(epoch_seconds: float) -> str:
    return (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )


def epoch_from_iso(value: str | None) -> float | None:
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


class Clock(Protocol):
    node_boot_id: str

    def utc(self) -> str: ...
    def monotonic(self) -> float: ...
    def epoch(self) -> float: ...
    def boot_id(self) -> str: ...
    def boot_anchor(self) -> float | None: ...


@dataclass
class SystemClock:
    """Real clock with a host-boot-scoped node identity.

    ``boot_id`` must identify the *host boot*, not the process: the API process
    and the supervisor process share persisted monotonic deadlines, and events
    from both must be attributable to one boot. It is therefore derived from the
    host boot instant (see :mod:`runner.contracts.boot`) rather than from a
    per-process random value.
    """

    _boot_id: str = ""
    _boot_anchor: float | None = None

    def __post_init__(self) -> None:
        from runner.contracts.boot import boot_id_for_anchor, host_boot_anchor

        if self._boot_anchor is None:
            self._boot_anchor = host_boot_anchor()
        if not self._boot_id:
            self._boot_id = boot_id_for_anchor(float(self._boot_anchor))

    @property
    def node_boot_id(self) -> str:  # pragma: no cover - trivial
        return self._boot_id

    def utc(self) -> str:
        return utc_now_iso()

    def epoch(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def boot_id(self) -> str:
        return self._boot_id

    def boot_anchor(self) -> float | None:
        return self._boot_anchor

    def adopt_boot_id(self, boot_id: str, anchor: float | None) -> None:
        """Adopt the persisted host-boot identity for this process.

        The store persists the first observed anchor; every later process in the
        same host boot adopts it, so a single boot never appears as two nodes and
        bucket edges cannot split it.
        """

        self._boot_id = boot_id
        if anchor is not None:
            self._boot_anchor = float(anchor)


class FixedClock:
    """Deterministic clock for tests and deterministic mock runs.

    Time only advances when ``advance`` is called, so mock attempt timing is
    fully reproducible.
    """

    def __init__(
        self,
        start_epoch: float = 1_700_000_000.0,
        monotonic_start: float = 1_000.0,
        boot_id: str = "boot-fixed-0001",
    ) -> None:
        self._epoch = float(start_epoch)
        self._mono = float(monotonic_start)
        self._boot_id = boot_id
        # The anchor is defined so that ``epoch - monotonic`` is a constant for
        # this clock, matching the real host invariant.
        self._boot_anchor = self._epoch - self._mono

    @property
    def node_boot_id(self) -> str:
        return self._boot_id

    def utc(self) -> str:
        return iso_from_epoch(self._epoch)

    def epoch(self) -> float:
        return self._epoch

    def monotonic(self) -> float:
        return self._mono

    def boot_id(self) -> str:
        return self._boot_id

    def boot_anchor(self) -> float | None:
        return self._boot_anchor

    def adopt_boot_id(self, boot_id: str, anchor: float | None) -> None:
        self._boot_id = boot_id
        if anchor is not None:
            self._boot_anchor = float(anchor)

    def simulate_host_reboot(
        self, *, epoch: float | None = None, monotonic_start: float = 5.0, boot_id: str | None = None
    ) -> None:
        """Model a host reboot: the monotonic clock restarts from ~0.

        The wall clock jumps forward (it kept counting), while monotonic resets,
        so the boot anchor moves forward by the down time and every previously
        persisted monotonic deadline becomes stale.
        """

        if epoch is not None:
            self._epoch = float(epoch)
        self._mono = float(monotonic_start)
        self._boot_anchor = self._epoch - self._mono
        if boot_id is None:
            from runner.contracts.boot import boot_id_for_anchor

            boot_id = boot_id_for_anchor(self._boot_anchor)
        self._boot_id = boot_id

    def advance(self, seconds: float) -> None:
        self._epoch += float(seconds)
        self._mono += float(seconds)
