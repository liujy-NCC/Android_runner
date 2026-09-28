"""Host boot identity.

DESIGN.md §4.4: monotonic time is only comparable "within the same node/boot
cycle". Two consequences drive this module:

* Processes that belong to the *same* host boot must agree on one shared,
  verifiable node-boot identity, so events emitted by the API process and by the
  supervisor process can be correlated as one boot instead of looking like two
  unrelated nodes.
* After a *host reboot* the monotonic clock resets, so any previously persisted
  monotonic deadline is meaningless. It must never be compared across that
  boundary, and an observation that was interrupted by a reboot must never be
  recorded as a normally completed window.

The identity is derived from the host's boot instant, estimated as
``wall_clock - monotonic``. Within one boot that estimate is stable across
processes to well under a second; after a reboot it jumps forward (the new boot
instant is later, and the monotonic clock has restarted from ~0). Comparison is
tolerance-based against the *persisted* anchor, so bucket boundaries cannot
split one boot, and the first process to persist an anchor owns the identity
that later processes adopt.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

# Two processes in the same host boot estimate the boot instant consistently to
# well under a second; a real reboot moves it by at least the down time. The
# tolerance absorbs clock slew/NTP steps while staying far below any plausible
# reboot interval. A host that reboots within this window of its *own* previous
# boot would be misclassified, which is documented as a known mock-stage limit.
BOOT_ANCHOR_TOLERANCE_SECONDS = 120.0


def host_boot_anchor() -> float:
    """Best-effort UTC epoch at which this host booted.

    ``time.monotonic()`` is defined as time since an unspecified point, which on
    every supported platform is the host boot, so the difference is the boot
    instant. The value is only ever compared to another estimate of the same
    quantity, never used as a real timestamp.
    """

    return time.time() - time.monotonic()


def boot_id_for_anchor(anchor_epoch: float, *, tolerance: float = BOOT_ANCHOR_TOLERANCE_SECONDS) -> str:
    """A stable, human-comparable label for the boot that produced ``anchor``.

    The anchor is snapped to a tolerance-sized bucket so that the label is
    deterministic for a single estimate. It is only a *first-boot* label: once an
    anchor is persisted, later processes adopt the persisted identity instead of
    deriving their own, so bucket edges can never split one host boot.
    """

    bucket = int(anchor_epoch // max(1.0, tolerance / 6.0))
    moment = datetime.fromtimestamp(bucket * (tolerance / 6.0), tz=timezone.utc)
    return "boot-" + moment.strftime("%Y%m%dT%H%M%SZ")


def boot_anchor_utc(anchor_epoch: float) -> str:
    return (
        datetime.fromtimestamp(anchor_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )


def classify_boot_anchor(
    persisted_anchor: float | None,
    current_anchor: float,
    *,
    tolerance: float = BOOT_ANCHOR_TOLERANCE_SECONDS,
) -> str:
    """Classify ``current_anchor`` against the persisted one.

    Returns ``first_boot`` (nothing persisted yet), ``same_boot`` (the monotonic
    clock is continuous, deadlines remain comparable) or ``host_reboot``.
    """

    if persisted_anchor is None:
        return "first_boot"
    if abs(float(current_anchor) - float(persisted_anchor)) <= tolerance:
        return "same_boot"
    return "host_reboot"
