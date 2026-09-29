"""Independent, prefixed identifiers for every object class."""

from __future__ import annotations

import secrets

# Crockford base32 without I/L/O/U to stay unambiguous in logs and receipts.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

PREFIXES = {
    "session": "ses",
    "candidate": "cnd",
    "build": "bld",
    "attempt": "att",
    "operation": "op",
    "action": "act",
    "task": "tsk",
    "artifact": "art",
    "seal": "seal",
    "event": "evt",
    "diagnostic": "diag",
    "oracle": "orc",
    "rejection": "rej",
    "lease": "lse",
    "reservation": "rsv",
}


def _random_b32(n_chars: int) -> str:
    # 5 bits per character.
    bits = 0
    value = 0
    out: list[str] = []
    while len(out) < n_chars:
        if bits < 5:
            value = (value << 8) | secrets.randbits(8)
            bits += 8
        bits -= 5
        out.append(_ALPHABET[(value >> bits) & 0x1F])
    return "".join(out)


def new_id(kind: str) -> str:
    """Return a new identifier such as ``ses_01J...`` (128 bits of entropy)."""

    try:
        prefix = PREFIXES[kind]
    except KeyError as exc:  # pragma: no cover - programming error
        raise KeyError(f"unknown id kind: {kind}") from exc
    # 128 bits -> 26 base32 chars.
    return f"{prefix}_{_random_b32(26)}"
