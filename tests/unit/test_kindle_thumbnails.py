"""`tasks.ebook.kindle.thumbnails`: the file name a device's own firmware expects for
a book's cover, and `install()`, which writes it.

Nothing here touches a real device — a tiny in-memory `FakeBackend` stands in for
`DeviceBackend`, exercising only the three methods `install()` actually calls
(`read`, `write`, `exists`). Real ffmpeg IS used, through the same `ffmpeg_path`
fixture `tests/conftest.py` already gives every other unit test that needs it
(`test_compress_video.py` and friends) — generating a synthetic cover with the
`lavfi` `color` source, never a checked-in fixture image.
"""

from __future__ import annotations

import struct
import subprocess
from pathlib import Path

from media_tools.tasks.ebook.covers import cache_path
from media_tools.tasks.ebook.kindle import thumbnails

BOOK_ID = "TESTBOOKID0001"


class FakeBackend:
    """Enough of `DeviceBackend` for `install()`'s own needs: `read` (the on-device
    extraction fallback), `write`, and `exists` (the post-write verification).
    `reject_writes_to` simulates a Colorsoft-style device that accepts a write and
    then silently discards it — `write()` runs, but the path never shows up as
    existing afterward, exactly the divergence `install()` is meant to detect.
    """

    def __init__(
        self,
        files: dict[str, bytes] | None = None,
        *,
        reject_writes_to: frozenset[str] = frozenset(),
    ) -> None:
        self.files = dict(files or {})
        self.written: dict[str, bytes] = {}
        self.reject_writes_to = set(reject_writes_to)

    def read(self, path: str, dest: Path) -> None:
        if path not in self.files:
            raise FileNotFoundError(path)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(self.files[path])

    def write(self, local: Path, path: str) -> None:
        if path in self.reject_writes_to:
            return  # accepted, then silently dropped — never lands in `written`
        self.written[path] = Path(local).read_bytes()

    def exists(self, path: str) -> bool:
        return path in self.written


def _make_image(
    path: Path, ffmpeg_path: str, *, width: int, height: int, color: str = "blue"
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg_path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={width}x{height}",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """(width, height) read straight out of a JPEG's own SOF marker — good enough to
    confirm ffmpeg's resize did what it claims without a new dependency or ffprobe
    (the bundled ffmpeg ships none, per `core.ffmpeg`'s own docstring)."""
    i = 2  # past the SOI marker (FFD8)
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + length
    raise ValueError("no SOF marker found")


def _mobi_with_cover(book_id: str, image_bytes: bytes, *, cdetype: str = "EBOK") -> bytes:
    """A byte blob shaped like a real MOBI file with THREE PalmDB records: the usual
    header+EXTH record (record0, mirroring `test_kindle_backup.py`'s own
    `mobi_bytes` helper), a text record, and — new here — an IMAGE record holding
    `image_bytes`, with EXTH 201 (cover offset) pointing at it via the MOBI header's
    own "first image record" field. This is the ONLY way to exercise the on-device
    extraction fallback end to end without a real Kindle."""
    exth_entries = [
        (113, book_id.encode()),
        (501, cdetype.encode()),
        (201, struct.pack(">I", 0)),  # the cover is the FIRST image record
    ]
    blob = b"".join(struct.pack(">II", tag, 8 + len(v)) + v for tag, v in exth_entries)
    exth_block = (
        b"EXTH" + struct.pack(">I", 12 + len(blob)) + struct.pack(">I", len(exth_entries)) + blob
    )

    header_length = 232
    record = bytearray(b"\0" * (16 + header_length))
    record[16:20] = b"MOBI"
    record[20:24] = struct.pack(">I", header_length)
    record[0x80:0x84] = struct.pack(">I", 0x40)  # EXTH present
    # The "first image record" field: MOBI-header offset 92, i.e. record0 offset 108.
    record[108:112] = struct.pack(">I", 2)  # PalmDB record 2 is the first image
    record0 = bytes(record) + exth_block

    text_record = b"text record"

    palm = bytearray(b"\0" * 102)  # 78 + 3 record-info entries * 8 bytes
    palm[76:78] = struct.pack(">H", 3)
    r0 = 102
    r1 = r0 + len(record0)
    r2 = r1 + len(text_record)
    palm[78:82] = struct.pack(">I", r0)
    palm[86:90] = struct.pack(">I", r1)
    palm[94:98] = struct.pack(">I", r2)
    return bytes(palm) + record0 + text_record + image_bytes


# --- thumbnail_name -------------------------------------------------------------


def test_thumbnail_name_matches_the_device_naming_scheme():
    assert (
        thumbnails.thumbnail_name("ABCDEF0123456789")
        == "thumbnail_ABCDEF0123456789_EBOK_portrait.jpg"
    )
    assert (
        thumbnails.thumbnail_name("ABCDEF0123456789", "PDOC")
        == "thumbnail_ABCDEF0123456789_PDOC_portrait.jpg"
    )


# --- install: cached cover -------------------------------------------------------


def test_a_cached_cover_is_resized_and_written_to_system_thumbnails(tmp_path, ffmpeg_path):
    cache_dir = tmp_path / "cache"
    _make_image(cache_path(cache_dir, BOOK_ID), ffmpeg_path, width=400, height=800)

    backend = FakeBackend()
    book = thumbnails.Book(device_path="documents/en/Book.azw3", book_id=BOOK_ID, cdetype="EBOK")

    statuses = thumbnails.install(backend, [book], cache_dir=cache_dir)

    assert statuses == {BOOK_ID: "installed"}
    device_path = f"system/thumbnails/thumbnail_{BOOK_ID}_EBOK_portrait.jpg"
    assert device_path in backend.written
    width, height = _jpeg_size(backend.written[device_path])
    assert height == 500
    assert width == 250  # 400 * 500/800, preserving the source's aspect ratio


def test_install_reports_progress_once_per_book(tmp_path, ffmpeg_path):
    cache_dir = tmp_path / "cache"
    _make_image(cache_path(cache_dir, BOOK_ID), ffmpeg_path, width=400, height=800)
    backend = FakeBackend()
    book = thumbnails.Book(device_path="documents/en/Book.azw3", book_id=BOOK_ID)

    calls: list[tuple[int, int]] = []
    thumbnails.install(
        backend, [book], cache_dir=cache_dir, on_progress=lambda d, t: calls.append((d, t))
    )

    assert calls == [(1, 1)]


# --- install: resize preserves aspect ratio --------------------------------------


def test_resize_preserves_aspect_ratio(tmp_path, ffmpeg_path):
    source = tmp_path / "source.jpg"
    _make_image(source, ffmpeg_path, width=400, height=1000)
    dest = tmp_path / "resized.jpg"

    assert thumbnails._resize(source, dest, ffmpeg=ffmpeg_path) is True

    width, height = _jpeg_size(dest.read_bytes())
    assert height == 500
    assert width == 200  # 400 * 500/1000, exactly


# --- install: device silently discards the write ---------------------------------


def test_a_device_that_discards_the_write_reports_rejected(tmp_path, ffmpeg_path):
    cache_dir = tmp_path / "cache"
    _make_image(cache_path(cache_dir, BOOK_ID), ffmpeg_path, width=400, height=800)
    device_path = f"system/thumbnails/thumbnail_{BOOK_ID}_EBOK_portrait.jpg"
    backend = FakeBackend(reject_writes_to=frozenset({device_path}))
    book = thumbnails.Book(device_path="documents/en/Book.azw3", book_id=BOOK_ID)

    statuses = thumbnails.install(backend, [book], cache_dir=cache_dir)

    assert statuses == {BOOK_ID: "rejected"}
    assert device_path not in backend.written


# --- install: no cover anywhere --------------------------------------------------


def test_a_book_with_no_cover_anywhere_reports_no_cover_and_writes_nothing(tmp_path):
    backend = FakeBackend(files={"documents/en/Book.azw3": b"not a real mobi file at all"})
    book = thumbnails.Book(device_path="documents/en/Book.azw3", book_id="NOCOVERBOOK01")

    statuses = thumbnails.install(backend, [book], cache_dir=tmp_path / "cache")

    assert statuses == {"NOCOVERBOOK01": "no_cover"}
    assert backend.written == {}


def test_a_book_vanished_from_the_device_reports_no_cover_not_a_crash(tmp_path):
    """Nothing cached, and the book itself is no longer on the device (renamed or
    deleted between listing and this call) — `backend.read` raises, which must be
    absorbed the same way one bad book is absorbed everywhere else in this Kindle
    path, not propagated to abort the whole `install()` batch."""
    backend = FakeBackend(files={})  # the book is simply not there
    book = thumbnails.Book(device_path="documents/en/Gone.azw3", book_id="GONEBOOK000001")

    statuses = thumbnails.install(backend, [book], cache_dir=tmp_path / "cache")

    assert statuses == {"GONEBOOK000001": "no_cover"}


# --- install: no EXTH 113 at all -------------------------------------------------


def test_a_book_with_no_book_id_is_handled_without_crashing(tmp_path):
    backend = FakeBackend()
    book = thumbnails.Book(device_path="documents/en/Unknown.azw3", book_id="")

    statuses = thumbnails.install(backend, [book], cache_dir=tmp_path / "cache")

    # Keyed by device_path, since there is no id to key it by.
    assert statuses == {"documents/en/Unknown.azw3": "no_cover"}
    assert backend.written == {}


# --- install: extracted from the book itself when nothing is cached --------------


def test_extracts_the_books_own_embedded_cover_when_nothing_is_cached(tmp_path, ffmpeg_path):
    source_image = tmp_path / "source.jpg"
    _make_image(source_image, ffmpeg_path, width=400, height=800)
    mobi_bytes = _mobi_with_cover("EMBEDDEDCOVER1", source_image.read_bytes())

    backend = FakeBackend(files={"documents/en/Book.azw3": mobi_bytes})
    book = thumbnails.Book(device_path="documents/en/Book.azw3", book_id="EMBEDDEDCOVER1")

    statuses = thumbnails.install(backend, [book], cache_dir=tmp_path / "cache")

    assert statuses == {"EMBEDDEDCOVER1": "installed"}
    device_path = "system/thumbnails/thumbnail_EMBEDDEDCOVER1_EBOK_portrait.jpg"
    width, height = _jpeg_size(backend.written[device_path])
    assert height == 500
    assert width == 250  # 400 * 500/800


def test_read_cover_image_returns_none_for_a_plain_non_mobi_file(tmp_path):
    plain = tmp_path / "plain.txt"
    plain.write_bytes(b"just some bytes, not a PalmDB file at all")
    assert thumbnails._read_cover_image(plain) is None
