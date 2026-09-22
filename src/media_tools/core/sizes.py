"""Parse and render byte sizes. MB/GB are decimal; MiB/GiB are binary."""

from __future__ import annotations

import re

_UNITS = {
    "": 10**6,
    "b": 1,
    "kb": 10**3,
    "mb": 10**6,
    "gb": 10**9,
    "tb": 10**12,
    "kib": 2**10,
    "mib": 2**20,
    "gib": 2**30,
    "tib": 2**40,
}
_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")


def parse_size(text: str) -> int:
    match = _PATTERN.match(text or "")
    if not match:
        raise ValueError(f"invalid size: {text!r} (examples: 25, 25MB, 25MiB, 1.5GB)")
    number, unit = match.group(1), match.group(2).lower()
    if unit not in _UNITS:
        raise ValueError(f"unknown size unit: {match.group(2)!r} (use B, KB, MB, GB, MiB, GiB)")
    value = float(number) * _UNITS[unit]
    if value <= 0:
        raise ValueError(f"size must be positive: {text!r}")
    return int(value)


def format_size(n: int) -> str:
    if n < 1000:
        return f"{n} B"
    for unit, factor in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if n >= factor:
            return f"{n / factor:.1f} {unit}"
    return f"{n} B"
