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


def read_records_or_none(path: Path) -> dict[int, bytes] | None:
    """`read_records(path)`, or `None` when the call itself RAISED.

    `read_records` above already absorbs everything a malformed MOBI can do — it
    answers `{}` for its own `struct.error`/`IndexError` parse failures and for the
    `OSError` of a file it cannot open — so that is emphatically NOT what this is for.
    What it does not absorb, and what this catches, is narrow and specific:

    - `ValueError` — `Path.read_bytes()` opens the file, and `open()` raises this
      (not `OSError`) for a path carrying an embedded NUL byte.
    - `MemoryError` — `read_records` reads the WHOLE file to reach a header in its
      first hundred bytes, so a pathologically large file can exhaust memory where a
      header-sized read never would.
    - `OSError` — already absorbed inside `read_records` today. Caught again here only
      so this wrapper's contract does not silently depend on that staying true.

    Deliberately NOT a bare `except Exception`: an `AttributeError`/`TypeError` from a
    future refactor of this module is a bug in this project, and the convention here
    (see `tasks.ebook.kindle.cli._run`) is that those escape to an honest
    `internal_error` rather than being disguised as a book with no records.

    This exists because callers read records for EVERY book on a device or in a
    library, where one unreadable file must never abort the whole run — for the Kindle
    commands, that means after the mandatory backup has already gone through. Use
    `read_records_safe` unless the difference between "raised" and "parsed as empty"
    actually changes what the caller does (it does for `kindle.cli._book_id_of`,
    which must not CACHE a failure as if it were an answer).
    """
    try:
        return read_records(path)
    except (OSError, ValueError, MemoryError):
        return None


def read_records_safe(path: Path) -> dict[int, bytes]:
    """`read_records_or_none`'s records, with a failure flattened to "no records" —
    exactly what `read_records` itself answers for a file it cannot parse. For the
    callers that report a book with no readable metadata the same way either way."""
    return read_records_or_none(path) or {}


def record_text(records: dict[int, bytes], tag: int) -> str | None:
    raw = records.get(tag)
    if raw is None:
        return None
    return raw.decode("utf-8", "replace").strip() or None
