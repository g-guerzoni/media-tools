"""Strip credentials from URLs before anything is printed or stored."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def redact_url(value: str) -> str:
    if "://" not in value:
        return value
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def redact(value):
    if isinstance(value, str):
        return redact_url(value)
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value
