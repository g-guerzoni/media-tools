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
    return _records_from(data)


def _records_from(data: bytes) -> dict[int, bytes]:
    """Parse the EXTH block out of bytes already in hand. Split from `read_records`
    so `read_records_or_none` can do its OWN read and still share one parser: the
    difference between the two is entirely in how the read failing is reported, and
    nothing else about the two functions should be able to drift apart."""
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
    except struct.error:
        # `struct.error` only: every read above is a SLICE of `data` or `record`, and
        # slicing never raises — a slice past the end is simply short, which is what
        # `struct.unpack` then rejects. There is no subscript in this function at all,
        # so the `IndexError` that used to be caught here could not happen.
        return {}


def read_records_or_none(path: Path) -> dict[int, bytes] | None:
    """`read_records(path)`'s records, or `None` when READING the file failed.

    It does its own read rather than delegating, because the whole point is the one
    distinction `read_records` cannot make: that function absorbs the `OSError` of a
    file it cannot open into the same `{}` it answers for a file that parsed and
    carries no records. A caller that must not CACHE a failure as an answer
    (`kindle.cli._book_id_of`) needs those two apart, and a wrapper built on top of
    `read_records` could never provide it. Three families are reported as `None`:

    - `OSError` — the file is not there (an MTP fetch that never landed), or the host
      refuses to open it. THE reachable one, and the one that used to be lost.
    - `ValueError` — `Path.read_bytes()` opens the file, and `open()` raises this (not
      `OSError`) for a path carrying an embedded NUL byte.
    - `MemoryError` — the whole file is read to reach a header in its first hundred
      bytes, so a pathologically large file can exhaust memory where a header-sized
      read never would.

    Parsing is a separate matter and is NOT reported here: a file that was read but
    will not parse is `{}`, exactly as `read_records` says, because a book that
    genuinely carries no EXTH records is indistinguishable from a malformed one and
    both are permanent facts about that file.

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
        data = Path(path).read_bytes()
    except (OSError, ValueError, MemoryError):
        return None
    return _records_from(data)


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
