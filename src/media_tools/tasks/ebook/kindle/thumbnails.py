"""Cover thumbnails for a sideloaded Kindle book: `system/thumbnails/<name>.jpg`,
sized and named exactly as the device's own firmware expects.

A sideloaded book with no thumbnail shows a grey placeholder on the Kindle's home
screen instead of its cover. Two halves ship together: `thumbnail_name` builds the
filename a book's own EXTH 113 id (and 501 content-type tag) resolve to — stable
across a library rebuild, since the id is — and `install` writes it, then VERIFIES
the write by listing `system/thumbnails/` again. Colorsoft and newer models accept a
sideloaded cover and then silently discard it, which is reported as the
`device_rejected_thumbnail` warning and a `"rejected"` status, never a failure: the
user did nothing wrong and nothing is broken.

The cover itself comes from the library's own cover cache first
(`covers.cache_path`, `<cache_dir>/covers/<book id>.jpg` — the exact path
`tasks.ebook.covers.resolve` already writes, reused rather than reinvented) and, only
when the cache has nothing, is read straight out of the book's own PalmDB image
records on the device (EXTH 201/202 point at them, the same way the device's own
firmware finds a cover) — no Calibre call, so a mass-storage-only workflow, which
never needed Calibre for anything else in this whole Kindle path, does not suddenly
grow that dependency here. Either source is resized to roughly 500px tall with the
project's own bundled ffmpeg (`core.ffmpeg`), never a platform-specific tool like
`sips`, so Linux works too.
"""

from __future__ import annotations

import struct
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from media_tools.core.ffmpeg import ffmpeg_exe, run_ffmpeg
from media_tools.tasks.ebook import exth
from media_tools.tasks.ebook.covers import cache_path
from media_tools.tasks.ebook.kindle.backend import DeviceBackend

THUMBNAIL_DIR = "system/thumbnails/"
DEFAULT_CDETYPE = "EBOK"

_TARGET_HEIGHT = 500
# Mirrors `covers._MIN_COVER_BYTES`'s own floor: a cached file this small is more
# likely a previous run's empty/broken leftover than a real cover.
_MIN_SOURCE_BYTES = 1000
# The MOBI header's own "first image record" field lives at this offset from the
# header's own start (record0 byte 16 onward, `exth.read_records`'s own layout — this
# module re-validates the identifier and header length before trusting any fixed
# offset inside it, the same way that one does).
_FIRST_IMAGE_INDEX_OFFSET = 92
_NO_OFFSET = 0xFFFFFFFF


@dataclass(frozen=True)
class Book:
    """One book to install a thumbnail for.

    `device_path` is where the book ITSELF lives on the device (device-relative,
    POSIX-style) — read only as a fallback, to extract an embedded cover, when the
    library's own cover cache has nothing for `book_id`. `book_id` is the book's own
    EXTH 113 id; a book with none (`book_id == ""`) cannot be named a thumbnail at
    all and is reported `"no_cover"` without ever touching the device. `cdetype` is
    the book's own EXTH 501 content-type tag (`"EBOK"`, `"PDOC"`, ...), defaulting to
    `"EBOK"` like the device itself does for a book that carries none.
    """

    device_path: str
    book_id: str
    cdetype: str = DEFAULT_CDETYPE


def thumbnail_name(book_id: str, cdetype: str = DEFAULT_CDETYPE) -> str:
    """The exact filename the device's own firmware looks for under
    `system/thumbnails/` for a book with this EXTH 113 id and EXTH 501 content type.
    Stable across a library rebuild, because the id is."""
    return f"thumbnail_{book_id}_{cdetype}_portrait.jpg"


def install(
    backend: DeviceBackend,
    books: list[Book],
    *,
    cache_dir: Path,
    ffmpeg: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, str]:
    """Install one thumbnail per book. Returns `{book id: "installed" | "rejected" |
    "no_cover"}` — a book with no id at all is keyed by its `device_path` instead,
    since there is no id to key it by.

    Per book: the cover comes from the shared library cache first
    (`covers.cache_path(cache_dir, book_id)`); when that has nothing (missing, or too
    small to be a real cover), it is extracted from the book's own file on the device
    (`_extract_embedded_cover`). Either source is resized to roughly 500px tall with
    the bundled ffmpeg and written to
    `system/thumbnails/<thumbnail_name(book_id, cdetype)>`. The write is then
    VERIFIED with `backend.exists` — a device that accepted the write and silently
    dropped it (Colorsoft and newer, by design) reports `"rejected"` rather than
    `"installed"`; a book with no cover anywhere (cache miss AND no embedded image,
    or a source ffmpeg cannot decode) reports `"no_cover"` and nothing is written for
    it. Neither case raises: one broken book must never abort the rest of the batch.

    `on_progress(done, total)` fires once per book, after that book is fully
    resolved (installed, rejected, or given up on).
    """
    ffmpeg_bin = ffmpeg or ffmpeg_exe()
    cache_dir = Path(cache_dir)
    total = len(books)
    statuses: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="kindle-thumbnails-") as scratch:
        scratch_dir = Path(scratch)
        for done, book in enumerate(books, start=1):
            key = book.book_id or book.device_path
            statuses[key] = _install_one(
                backend, book, cache_dir=cache_dir, ffmpeg=ffmpeg_bin, scratch_dir=scratch_dir
            )
            if on_progress:
                on_progress(done, total)
    return statuses


def _install_one(
    backend: DeviceBackend, book: Book, *, cache_dir: Path, ffmpeg: str, scratch_dir: Path
) -> str:
    if not book.book_id:
        return "no_cover"

    source = cache_path(cache_dir, book.book_id)
    if not _is_usable(source):
        extracted = _extract_embedded_cover(backend, book, scratch_dir=scratch_dir)
        if extracted is None:
            return "no_cover"
        source = extracted

    resized = scratch_dir / f"{_safe_stem(book.book_id)}-thumb.jpg"
    if not _resize(source, resized, ffmpeg=ffmpeg):
        return "no_cover"

    device_path = f"{THUMBNAIL_DIR}{thumbnail_name(book.book_id, book.cdetype)}"
    backend.write(resized, device_path)
    return "installed" if backend.exists(device_path) else "rejected"


def _is_usable(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > _MIN_SOURCE_BYTES


def _safe_stem(book_id: str) -> str:
    """A scratch-file stem safe on every host filesystem. `book_id` is an EXTH 113
    value (Amazon's own book identifier) and, unlike a device path, is never expected
    to hold a path separator — but this is a local temp file, not the trusted device
    tree, so it is not trusted with one either."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in book_id) or "book"


# --- resizing -----------------------------------------------------------------------


def _resize(source: Path, dest: Path, *, ffmpeg: str, target_height: int = _TARGET_HEIGHT) -> bool:
    """Resize `source` to roughly `target_height` px tall, preserving aspect ratio,
    with the project's own bundled ffmpeg — never `sips` or any other
    platform-specific tool, so this works on Linux too. `scale=-2:H`, not `-1:H`,
    rounds the computed width to the nearest EVEN number: mjpeg's default pixel
    format (`yuvj420p`) needs an even width, and `-1` can hand back an odd one that
    ffmpeg then refuses to encode.
    """
    argv = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-vf",
        f"scale=-2:{target_height}",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(dest),
    ]
    returncode, _tail = run_ffmpeg(argv)
    return returncode == 0 and dest.is_file() and dest.stat().st_size > 0


# --- extracting a cover straight from the book's own PalmDB image records -----------


def _extract_embedded_cover(
    backend: DeviceBackend, book: Book, *, scratch_dir: Path
) -> Path | None:
    """Fetch `book.device_path` onto the host and pull its embedded cover (or,
    failing that, its embedded Kindle-generated thumbnail) straight out of the
    file's own PalmDB image records — the same mechanism a real Kindle uses, so no
    Calibre call is needed.

    Any failure here — the book vanished from the device between listing and this
    fetch, or its EXTH/image records do not parse — is treated as "no cover", not
    raised: one book's broken file must not abort a whole `install()` batch, the
    same isolation `covers.resolve` and `cli._materialize_for_scan` already apply.
    """
    suffix = Path(book.device_path).suffix
    local = scratch_dir / f"{_safe_stem(book.book_id)}-book{suffix}"
    try:
        backend.read(book.device_path, local)
    except (RuntimeError, OSError):
        return None
    image = _read_cover_image(local)
    if image is None:
        return None
    dest = scratch_dir / f"{_safe_stem(book.book_id)}-embedded.jpg"
    dest.write_bytes(image)
    return dest


def _read_cover_image(path: Path) -> bytes | None:
    """The bytes of `path`'s own embedded cover, read straight out of its PalmDB
    image records. Prefers EXTH 201 (the full cover); falls back to EXTH 202
    (Kindle's own smaller thumbnail) when 201 is absent or marked "no cover"
    (`0xFFFFFFFF`). Returns None for anything that does not parse as a MOBI file
    with a usable image at the resolved record, mirroring `exth.read_records`'s own
    "unreadable or not MOBI -> nothing" contract rather than raising.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    try:
        count = struct.unpack(">H", data[76:78])[0]
        if count < 2:
            return None
        offsets = [struct.unpack(">I", data[78 + i * 8 : 82 + i * 8])[0] for i in range(count)]
        record0 = data[offsets[0] : offsets[1]]
        if record0[16:20] != b"MOBI":
            return None
        header_length = struct.unpack(">I", record0[20:24])[0]
        if header_length < _FIRST_IMAGE_INDEX_OFFSET + 4:
            return None  # header too short to carry a first-image-record field at all
        field_start = 16 + _FIRST_IMAGE_INDEX_OFFSET
        first_image_index = struct.unpack(">I", record0[field_start : field_start + 4])[0]

        records = exth.read_records(path)
        index = _image_record_index(records, exth.TAG_COVER_OFFSET, first_image_index)
        if index is None:
            index = _image_record_index(records, exth.TAG_THUMB_OFFSET, first_image_index)
        if index is None or not (0 <= index < count):
            return None
        start = offsets[index]
        end = offsets[index + 1] if index + 1 < count else len(data)
        image = data[start:end]
        return image if _looks_like_image(image) else None
    except (struct.error, IndexError):
        return None


def _image_record_index(records: dict[int, bytes], tag: int, first_image_index: int) -> int | None:
    raw = records.get(tag)
    if raw is None or len(raw) < 4:
        return None
    offset = struct.unpack(">I", raw[:4])[0]
    if offset == _NO_OFFSET:
        return None
    return first_image_index + offset


def _looks_like_image(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"
