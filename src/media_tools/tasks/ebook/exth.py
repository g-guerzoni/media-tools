"""Read the EXTH records a MOBI/AZW3 file carries in its first PalmDB record.

A Kindle names a book's cover thumbnail after records 113 and 501, so these are
what identify a book on the device — never its filename.
"""

from __future__ import annotations

import struct
from pathlib import Path

TAG_AUTHOR = 100
TAG_UUID = 113
TAG_CDETYPE = 501
TAG_TITLE = 503
TAG_LANGUAGE = 524
TAG_COVER_OFFSET = 201
TAG_THUMB_OFFSET = 202

_EXTH_PRESENT = 0x40


def read_records(path: Path) -> dict[int, bytes]:
    """Return {tag: payload}. An unreadable or non-MOBI file yields an empty dict."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return {}
    try:
        count = struct.unpack(">H", data[76:78])[0]
        if count < 2:
            return {}
        first, second = (struct.unpack(">I", data[78 + i * 8 : 82 + i * 8])[0] for i in range(2))
        record = data[first:second]
        if record[16:20] != b"MOBI":
            return {}
        header_length = struct.unpack(">I", record[20:24])[0]
        if not struct.unpack(">I", record[0x80:0x84])[0] & _EXTH_PRESENT:
            return {}
        start = 16 + header_length
        if record[start : start + 4] != b"EXTH":
            return {}
        entries = struct.unpack(">I", record[start + 8 : start + 12])[0]
        position = start + 12
        found: dict[int, bytes] = {}
        for _ in range(entries):
            tag, length = struct.unpack(">II", record[position : position + 8])
            if length < 8:
                break
            found[tag] = record[position + 8 : position + length]
            position += length
        return found
    except (struct.error, IndexError):
        return {}


def record_text(records: dict[int, bytes], tag: int) -> str | None:
    raw = records.get(tag)
    if raw is None:
        return None
    return raw.decode("utf-8", "replace").strip() or None
