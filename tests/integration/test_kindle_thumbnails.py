"""Real-ffmpeg coverage for `tasks.ebook.kindle.thumbnails`: actual resize/decode
behaviour, which `tests/unit/test_kindle_thumbnails.py` deliberately stubs out
(`_resize` monkeypatched there, per CLAUDE.md's "fast, no real media required" rule
for `tests/unit/`). Uses the same `ffmpeg_path` fixture (`tests/conftest.py`) every
other integration test that needs ffmpeg uses, generating synthetic covers with the
`lavfi` `color` source — no checked-in fixture image.
"""

from __future__ import annotations

import struct
import subprocess
from pathlib import Path

from media_tools.tasks.ebook.covers import cache_path
from media_tools.tasks.ebook.kindle import thumbnails


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


class FakeBackend:
    """Enough of `DeviceBackend` for `install()`'s own needs — see
    `tests/unit/test_kindle_thumbnails.py`'s own copy for the full rationale; kept
    duplicated rather than shared, since the two files intentionally test different
    layers (mocked orchestration vs. real ffmpeg) and importing test-only fixtures
    across `tests/unit`/`tests/integration` is not a pattern this project uses
    anywhere else."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = dict(files or {})
        self.written: dict[str, bytes] = {}

    def read(self, path: str, dest: Path) -> None:
        if path not in self.files:
            raise FileNotFoundError(path)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(self.files[path])

    def write(self, local: Path, path: str) -> None:
        self.written[path] = Path(local).read_bytes()

    def exists(self, path: str) -> bool:
        return path in self.written


def _mobi_with_cover(book_id: str, image_bytes: bytes, *, cdetype: str = "EBOK") -> bytes:
    """A byte blob shaped like a real MOBI file with THREE PalmDB records: the usual
    header+EXTH record, a text record, and an IMAGE record holding `image_bytes`,
    with EXTH 201 (cover offset) pointing at it via the MOBI header's own "first
    image record" field."""
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


def test_resize_preserves_aspect_ratio(tmp_path, ffmpeg_path):
    source = tmp_path / "source.jpg"
    _make_image(source, ffmpeg_path, width=400, height=1000)
    dest = tmp_path / "resized.jpg"

    assert thumbnails._resize(source, dest, ffmpeg=ffmpeg_path) is True

    width, height = _jpeg_size(dest.read_bytes())
    assert height == 500
    assert width == 200  # 400 * 500/1000, exactly


def test_a_cached_cover_is_really_resized_by_ffmpeg_and_written_to_system_thumbnails(
    tmp_path, ffmpeg_path
):
    book_id = "REALCACHEBOOK1"
    cache_dir = tmp_path / "cache"
    _make_image(cache_path(cache_dir, book_id), ffmpeg_path, width=400, height=800)

    backend = FakeBackend()
    book = thumbnails.Book(device_path="documents/en/Book.azw3", book_id=book_id, cdetype="EBOK")

    statuses = thumbnails.install(backend, [book], cache_dir=cache_dir)

    assert statuses == {book_id: "installed"}
    device_path = f"system/thumbnails/thumbnail_{book_id}_EBOK_portrait.jpg"
    width, height = _jpeg_size(backend.written[device_path])
    assert height == 500
    assert width == 250  # 400 * 500/800, preserving the source's aspect ratio


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
