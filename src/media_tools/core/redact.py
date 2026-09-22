"""Strip credentials from URLs before anything is printed or stored."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


def redact_url(value: str) -> str:
    if "://" not in value:
        return value
    parts = urlsplit(value)
    # Rebuild netloc from hostname (+ port) rather than reusing `parts.netloc` verbatim:
    # netloc also carries any "user:password@" userinfo, which a bare scheme+netloc+path
    # would let straight through even though the query string right next to it gets
    # stripped a line below.
    netloc = parts.hostname or ""
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def redact_text(text: str) -> str:
    """Redact URLs embedded in plain text, leaving non-URL text intact."""
    return re.sub(r"https?://[^\s]+", lambda m: redact_url(m.group(0)), text)


def redact(value):
    if isinstance(value, str):
        return redact_url(value)
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value
