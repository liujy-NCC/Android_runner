"""Clocks: UTC wall clock plus a host monotonic clock.

Design §4.4 requires events to carry both UTC and monotonic time; monotonic
values are only comparable within one node boot, so the node boot id is
recorded alongside.
"""

from __future__ import annotations

import time
import uuid
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


@dataclass
class SystemClock:
    """Real clock. Monotonic is process-relative but stable for the process."""

    _boot_id: str = ""

    def __post_init__(self) -> None:
        if not self._boot_id:
            self._boot_id = uuid.uuid4().hex

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


class FixedClock:
    """Deterministic clock for tests and deterministic mock runs.

    Time only advances when ``advance`` is called, so mock attempt timing is
    fully reproducible.
    """

    def __init__(self, start_epoch: float = 1_700_000_000.0, monotonic_start: float = 1_000.0) -> None:
        self._epoch = float(start_epoch)
        self._mono = float(monotonic_start)
        self._boot_id = "boot-fixed-0001"

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

    def advance(self, seconds: float) -> None:
        self._epoch += float(seconds)
        self._mono += float(seconds)
