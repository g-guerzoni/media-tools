"""`ebook kindle add`: put books on a simulated mass-storage Kindle, always after a
backup.

Driven against the shared `fake_kindle` fixture (`tests/conftest.py`) through the
injected `device_finder`/`backend_factory` seam `kindle.cli.resolve_device` exists
for — never real hardware, and never the network. Unlike
`tests/unit/test_kindle_cli.py`'s own thumbnail tests, nothing here stubs
`thumbnails._resize`: this file lives in `tests/integration/` precisely so the
cover really is resized by the bundled ffmpeg and really does land on the device,
which is the half of `add` a mocked resize cannot prove.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from media_tools.cli import build_parser
from media_tools.core.events import EXIT_DEPENDENCY, EXIT_FAILED, EXIT_OK, EXIT_USAGE
from media_tools.tasks.common import UsageError
from media_tools.tasks.ebook.covers import cache_path
from media_tools.tasks.ebook.kindle import backup as backup_module
from media_tools.tasks.ebook.kindle import cli as kindle_cli
from media_tools.tasks.ebook.kindle import massstorage
from media_tools.tasks.ebook.kindle.backend import DeviceFile, validate_writable_path
from media_tools.tasks.ebook.kindle.detect import Device

BLADE_ID = "BLADEITSELF00001"
LIVRO_ID = "UMLIVRO000000001"

# Deliberately unrelated to the filenames below: a regression to filename-derived
# metadata (or to filename-based identity) could not produce these values.
BLADE_TITLE = "Not The Filename At All"
BLADE_AUTHOR = "Someone Else Entirely"


# --- fixtures built here, not checked in ---------------------------------------------


def _mobi_bytes(
    *,
    book_id: str | None = None,
    title: str | None = None,
    author: str | None = None,
    language: str | None = None,
    cdetype: str = "EBOK",
    padding: int = 0,
) -> bytes:
    """A byte blob `tasks.ebook.exth.read_records` parses, carrying whichever of EXTH
    113 (book id)/501 (CDE type)/503 (title)/100 (author)/524 (language) is given.
    `padding` appends filler so a test can give one book a deliberately larger size
    than another without needing real book content."""
    entries: list[tuple[int, bytes]] = [(501, cdetype.encode())]
    if book_id is not None:
        entries.append((113, book_id.encode()))
    if title is not None:
        entries.append((503, title.encode("utf-8")))
    if author is not None:
        entries.append((100, author.encode("utf-8")))
    if language is not None:
        entries.append((524, language.encode("utf-8")))
    blob = b"".join(struct.pack(">II", tag, 8 + len(value)) + value for tag, value in entries)
    exth = b"EXTH" + struct.pack(">I", 12 + len(blob)) + struct.pack(">I", len(entries)) + blob

    header_length = 232
    record = bytearray(b"\0" * (16 + header_length))
    record[16:20] = b"MOBI"
    record[20:24] = struct.pack(">I", header_length)
    record[0x80:0x84] = struct.pack(">I", 0x40)
    record0 = bytes(record) + exth

    palm = bytearray(b"\0" * 94)
    palm[76:78] = struct.pack(">H", 2)
    palm[78:82] = struct.pack(">I", 94)
    palm[86:90] = struct.pack(">I", 94 + len(record0))
    return bytes(palm) + record0 + b"text record" + b"\0" * padding


def _plant_source(
    directory: Path,
    name: str,
    *,
    book_id: str,
    title: str,
    author: str,
    language: str,
    padding: int = 0,
) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        _mobi_bytes(book_id=book_id, title=title, author=author, language=language, padding=padding)
    )
    return path


def _plant_cover(root: Path, book_id: str, ffmpeg_path: str) -> None:
    """A real JPEG in the shared library cover cache — the first place
    `thumbnails.install` looks, and big enough to pass its own size floor."""
    target = cache_path(root / ".cache", book_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg_path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=400x800",
            "-frames:v",
            "1",
            str(target),
        ],
        check=True,
        capture_output=True,
    )


def _mass_storage_factory(device, *, cache_dir):
    return massstorage.MassStorageBackend(device.mount)


def _events(capsys) -> list[dict]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _add_args(root: Path, *extra: str):
    return build_parser().parse_args(["ebook", "kindle", "add", *extra, "--json", "-o", str(root)])


# --- backends that misbehave in exactly one way ---------------------------------------


class _BrokenListingBackend:
    """Every listing fails, so the mandatory pre-write backup fails — while `write`
    would have worked fine. Proves the backup really is a precondition and not merely
    a step that had nothing to do."""

    def __init__(self, mount: Path) -> None:
        self._inner = massstorage.MassStorageBackend(mount)

    def list_files(self, prefix: str = ""):
        raise FileNotFoundError("the Kindle is no longer mounted (simulated)")

    def read(self, path: str, dest: Path) -> None:
        self._inner.read(path, dest)

    def read_many(self, items) -> None:
        self._inner.read_many(items)

    def write(self, local: Path, path: str) -> None:
        self._inner.write(local, path)

    def remove(self, path: str) -> None:
        self._inner.remove(path)

    def exists(self, path: str) -> bool:
        return self._inner.exists(path)

    def free_space(self) -> int:
        return self._inner.free_space()

    def eject(self) -> None:
        self._inner.eject()

    def close(self) -> None:
        self._inner.close()


class _FixedFreeSpaceBackend:
    """A real mass-storage backend that reports a fixed number of free bytes, so an
    out-of-space run can be exercised without filling a real filesystem."""

    def __init__(self, mount: Path, *, free: int) -> None:
        self._inner = massstorage.MassStorageBackend(mount)
        self._free = free

    def list_files(self, prefix: str = ""):
        return self._inner.list_files(prefix)

    def read(self, path: str, dest: Path) -> None:
        self._inner.read(path, dest)

    def read_many(self, items) -> None:
        self._inner.read_many(items)

    def write(self, local: Path, path: str) -> None:
        self._inner.write(local, path)

    def remove(self, path: str) -> None:
        self._inner.remove(path)

    def exists(self, path: str) -> bool:
        return self._inner.exists(path)

    def free_space(self) -> int:
        return self._free

    def eject(self) -> None:
        self._inner.eject()

    def close(self) -> None:
        self._inner.close()


class _ThumbnailRejectingBackend:
    """Accepts a thumbnail write and silently drops it — Colorsoft and newer, by
    design. Books still land normally, which is the point: a cover the device refuses
    must not make the BOOK a failure."""

    def __init__(self, mount: Path) -> None:
        self._inner = massstorage.MassStorageBackend(mount)

    def list_files(self, prefix: str = ""):
        return self._inner.list_files(prefix)

    def read(self, path: str, dest: Path) -> None:
        self._inner.read(path, dest)

    def read_many(self, items) -> None:
        self._inner.read_many(items)

    def write(self, local: Path, path: str) -> None:
        if path.startswith("system/thumbnails/"):
            return  # accepted, then silently dropped
        self._inner.write(local, path)

    def remove(self, path: str) -> None:
        self._inner.remove(path)

    def exists(self, path: str) -> bool:
        return self._inner.exists(path)

    def free_space(self) -> int:
        return self._inner.free_space()

    def eject(self) -> None:
        self._inner.eject()

    def close(self) -> None:
        self._inner.close()


class _ShortWriteBackend:
    """Accepts a book write and lands only part of it — the failure mode MTP writes
    can genuinely have (no rename primitive exists there, per the MTP backend's own
    ruling), and the exact case the `verify` stage exists to catch. `exists` still
    answers `True`, so only a SIZE check can tell this apart from a good write."""

    def __init__(self, mount: Path, *, short_prefix: str = "documents/") -> None:
        self._inner = massstorage.MassStorageBackend(mount)
        self._short_prefix = short_prefix

    def list_files(self, prefix: str = ""):
        return self._inner.list_files(prefix)

    def read(self, path: str, dest: Path) -> None:
        self._inner.read(path, dest)

    def read_many(self, items) -> None:
        self._inner.read_many(items)

    def write(self, local: Path, path: str) -> None:
        if path.startswith(self._short_prefix):
            truncated = Path(local).with_suffix(".truncated")
            truncated.write_bytes(Path(local).read_bytes()[:10])
            self._inner.write(truncated, path)
            truncated.unlink()
            return
        self._inner.write(local, path)

    def remove(self, path: str) -> None:
        self._inner.remove(path)

    def exists(self, path: str) -> bool:
        return self._inner.exists(path)

    def free_space(self) -> int:
        return self._inner.free_space()

    def eject(self) -> None:
        self._inner.eject()

    def close(self) -> None:
        self._inner.close()


# --- argument wiring -------------------------------------------------------------------


def test_add_is_registered_with_its_batch_lang_and_match_flags():
    args = build_parser().parse_args(
        ["ebook", "kindle", "add", "--batch", "library", "--lang", "en", "--match", "x", "--json"]
    )
    assert args.kindle_command == "add"
    assert args.batch == "library"
    assert args.lang == "en"
    assert args.match == "x"


def test_add_with_neither_books_nor_batch_is_a_usage_error(tmp_path):
    """Raised, not returned: `cli.main` is the one place a `UsageError` becomes exit
    2 plus its `error`/`result` pair — the same shape `scan --compare` already uses."""
    args = _add_args(tmp_path / "media")
    with pytest.raises(UsageError) as caught:
        kindle_cli.run_add(args, device_finder=lambda: None, backend_factory=None)
    assert caught.value.exit_code == EXIT_USAGE


def test_add_with_both_books_and_batch_is_a_usage_error(tmp_path, fake_kindle):
    source = _plant_source(
        tmp_path / "books",
        "Book.azw3",
        book_id=BLADE_ID,
        title="Book",
        author="Author",
        language="en",
    )
    args = _add_args(tmp_path / "media", str(source), "--batch", "library")
    with pytest.raises(UsageError):
        kindle_cli.run_add(
            args, device_finder=lambda: fake_kindle, backend_factory=_mass_storage_factory
        )


def test_the_cli_turns_an_add_with_no_books_into_exit_2_with_an_error_and_a_result(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "media_tools",
            "ebook",
            "kindle",
            "add",
            "--json",
            "-o",
            str(tmp_path / "media"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == EXIT_USAGE
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(e["type"] == "error" and e.get("code") == "usage" for e in events)
    assert events[-1]["type"] == "result"


# --- the happy path ---------------------------------------------------------------------


def test_two_books_are_copied_to_the_device_with_their_thumbnails(
    fake_kindle, tmp_path, capsys, ffmpeg_path
):
    root = tmp_path / "media"
    books = tmp_path / "books"
    blade = _plant_source(
        books,
        "The Blade Itself.azw3",
        book_id=BLADE_ID,
        title=BLADE_TITLE,
        author=BLADE_AUTHOR,
        language="en",
    )
    livro = _plant_source(
        books,
        "Um Livro Novo.azw3",
        book_id=LIVRO_ID,
        title="Um Livro Novo",
        author="Um Autor",
        language="pt",
    )
    _plant_cover(root, BLADE_ID, ffmpeg_path)
    _plant_cover(root, LIVRO_ID, ffmpeg_path)

    args = _add_args(root, str(blade), str(livro))
    exit_code = kindle_cli.run_add(
        args, device_finder=lambda: fake_kindle, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_OK

    mount = fake_kindle.mount
    # Each book lands under documents/<its own language>/, byte-for-byte.
    assert (mount / "documents" / "en" / "The Blade Itself.azw3").read_bytes() == blade.read_bytes()
    assert (mount / "documents" / "pt" / "Um Livro Novo.azw3").read_bytes() == livro.read_bytes()
    # ...with a real, ffmpeg-resized thumbnail named from its own EXTH 113 id.
    for book_id in (BLADE_ID, LIVRO_ID):
        thumb = mount / "system" / "thumbnails" / f"thumbnail_{book_id}_EBOK_portrait.jpg"
        assert thumb.is_file()
        assert thumb.read_bytes()[:3] == b"\xff\xd8\xff"

    events = _events(capsys)
    result = events[-1]
    assert result["type"] == "result"
    assert result["ok"] is True
    assert result["counts"]["done"] == 2
    assert result["counts"]["failed"] == 0

    start = events[0]
    assert start["type"] == "start"
    assert start["stages"] == ["detect", "backup", "plan", "copy", "thumbnails", "verify"]
    # Unlike the device-driven commands, `add` knows how many books it was given
    # before it ever looks at a Kindle.
    assert start["items"] == 2

    stages = [e["stage"] for e in events if e["type"] == "stage"]
    assert stages == ["detect", "backup", "plan", "copy", "thumbnails", "verify"]

    items = {e["input"]: e for e in events if e["type"] == "item"}
    assert items[str(blade)]["status"] == "done"
    assert items[str(blade)]["outputs"] == ["documents/en/The Blade Itself.azw3"]
    assert items[str(livro)]["status"] == "done"

    # The mandatory pre-write backup really ran, before anything was written.
    assert (root / "_kindle" / fake_kindle.serial / "backups").is_dir()


def test_a_book_already_on_the_device_is_skipped_as_exists(
    fake_kindle, tmp_path, capsys, ffmpeg_path
):
    """Matched by EXTH 113, never by filename: the device copy is named nothing like
    the source, so only a real id comparison can find it."""
    mount = fake_kindle.mount
    (mount / "documents" / "en" / "Totally Different Name.azw3").write_bytes(
        _mobi_bytes(book_id=BLADE_ID, title=BLADE_TITLE, author=BLADE_AUTHOR, language="en")
    )

    root = tmp_path / "media"
    blade = _plant_source(
        tmp_path / "books",
        "The Blade Itself.azw3",
        book_id=BLADE_ID,
        title=BLADE_TITLE,
        author=BLADE_AUTHOR,
        language="en",
    )
    _plant_cover(root, BLADE_ID, ffmpeg_path)

    args = _add_args(root, str(blade))
    exit_code = kindle_cli.run_add(
        args, device_finder=lambda: fake_kindle, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_OK

    events = _events(capsys)
    item = next(e for e in events if e["type"] == "item")
    assert item["status"] == "skipped"
    assert item["reason"] == "exists"
    # Nothing was written under the source's own name...
    assert not (mount / "documents" / "en" / "The Blade Itself.azw3").exists()
    # ...and nothing was journalled, because nothing changed on the device.
    assert backup_module.journal_read(root, fake_kindle.serial) == []


# --- the backup is a precondition, not a step -------------------------------------------


def test_a_failed_backup_aborts_the_add_and_writes_nothing(fake_kindle, tmp_path, capsys):
    """Exit 3 with all-zero counts, NOT `backup`'s own exit 1: here the snapshot is a
    precondition that was never met, so nothing the user asked for was attempted."""
    root = tmp_path / "media"
    blade = _plant_source(
        tmp_path / "books",
        "The Blade Itself.azw3",
        book_id=BLADE_ID,
        title=BLADE_TITLE,
        author=BLADE_AUTHOR,
        language="en",
    )

    args = _add_args(root, str(blade))
    exit_code = kindle_cli.run_add(
        args,
        device_finder=lambda: fake_kindle,
        backend_factory=lambda d, *, cache_dir: _BrokenListingBackend(d.mount),
    )
    assert exit_code == EXIT_DEPENDENCY

    events = _events(capsys)
    results = [e for e in events if e["type"] == "result"]
    assert len(results) == 1
    assert results[0]["ok"] is False
    assert results[0]["exit_code"] == EXIT_DEPENDENCY
    assert results[0]["counts"] == {"total": 0, "done": 0, "skipped": 0, "failed": 0, "pending": 0}
    assert any(e["code"] == "backup_failed" for e in events if e["type"] == "error")
    assert [e for e in events if e["type"] == "item"] == []

    # NOTHING was written: not the book, not a thumbnail, not the documents folder.
    assert not (fake_kindle.mount / "documents" / "en" / "The Blade Itself.azw3").exists()
    assert list((fake_kindle.mount / "system" / "thumbnails").iterdir()) == [
        fake_kindle.mount / "system" / "thumbnails" / "cover.jpg"
    ]


# --- one book's failure never stops the rest ---------------------------------------------


def test_a_device_with_too_little_space_fails_the_big_book_and_still_places_the_small_one(
    fake_kindle, tmp_path, capsys, ffmpeg_path
):
    root = tmp_path / "media"
    books = tmp_path / "books"
    big = _plant_source(
        books,
        "Big Book.azw3",
        book_id="BIGBOOK000000001",
        title="Big Book",
        author="Some Author",
        language="en",
        padding=40_000,
    )
    small = _plant_source(
        books,
        "Small Book.azw3",
        book_id="SMALLBOOK0000001",
        title="Small Book",
        author="Some Author",
        language="en",
    )
    _plant_cover(root, "SMALLBOOK0000001", ffmpeg_path)

    args = _add_args(root, str(big), str(small))
    exit_code = kindle_cli.run_add(
        args,
        device_finder=lambda: fake_kindle,
        backend_factory=lambda d, *, cache_dir: _FixedFreeSpaceBackend(d.mount, free=5_000),
    )
    assert exit_code == EXIT_FAILED

    events = _events(capsys)
    items = {e["input"]: e for e in events if e["type"] == "item"}
    assert items[str(big)]["status"] == "failed"
    assert items[str(big)]["reason"] == "engine_error"
    assert items[str(small)]["status"] == "done"

    mount = fake_kindle.mount
    assert not (mount / "documents" / "en" / "Big Book.azw3").exists()
    # What DID fit is kept, and really is on the device.
    assert (mount / "documents" / "en" / "Small Book.azw3").read_bytes() == small.read_bytes()

    result = events[-1]
    assert result["ok"] is False
    assert result["counts"] == {"total": 2, "done": 1, "skipped": 0, "failed": 1, "pending": 0}
    # The registry has no "out of space" reason, so the cause rides along on `detail`
    # — behind a STABLE PREFIX, so an agent branches on a term instead of
    # substring-matching English prose.
    failed = next(entry for entry in result["failed"] if entry["input"] == str(big))
    assert failed["detail"].startswith(f"{kindle_cli.DETAIL_OUT_OF_SPACE}: ")
    # And the same detail is on the streaming surface, not only in the summary.
    assert items[str(big)]["detail"] == failed["detail"]


def test_a_thumbnail_the_device_discards_warns_but_never_fails_the_book(
    fake_kindle, tmp_path, capsys, ffmpeg_path
):
    root = tmp_path / "media"
    blade = _plant_source(
        tmp_path / "books",
        "The Blade Itself.azw3",
        book_id=BLADE_ID,
        title=BLADE_TITLE,
        author=BLADE_AUTHOR,
        language="en",
    )
    _plant_cover(root, BLADE_ID, ffmpeg_path)

    args = _add_args(root, str(blade))
    exit_code = kindle_cli.run_add(
        args,
        device_finder=lambda: fake_kindle,
        backend_factory=lambda d, *, cache_dir: _ThumbnailRejectingBackend(d.mount),
    )
    # The book is on the device; only its cover was refused. Nothing failed.
    assert exit_code == EXIT_OK

    events = _events(capsys)
    item = next(e for e in events if e["type"] == "item")
    assert item["status"] == "done"
    assert item["warnings"] == ["device_rejected_thumbnail"]
    assert events[-1]["data"]["thumbnails"][BLADE_ID] == "rejected"
    assert (fake_kindle.mount / "documents" / "en" / "The Blade Itself.azw3").is_file()


def test_a_write_that_lands_the_wrong_size_is_reported_failed_not_done(
    fake_kindle, tmp_path, capsys, ffmpeg_path
):
    """A "successful" write that left the wrong number of bytes is a failed item —
    the whole point of the `verify` stage. `exists` alone says yes here."""
    root = tmp_path / "media"
    blade = _plant_source(
        tmp_path / "books",
        "The Blade Itself.azw3",
        book_id=BLADE_ID,
        title=BLADE_TITLE,
        author=BLADE_AUTHOR,
        language="en",
    )
    _plant_cover(root, BLADE_ID, ffmpeg_path)

    args = _add_args(root, str(blade))
    exit_code = kindle_cli.run_add(
        args,
        device_finder=lambda: fake_kindle,
        backend_factory=lambda d, *, cache_dir: _ShortWriteBackend(d.mount),
    )
    assert exit_code == EXIT_FAILED

    events = _events(capsys)
    item = next(e for e in events if e["type"] == "item")
    assert item["status"] == "failed"
    assert item["reason"] == "engine_error"
    assert events[-1]["counts"]["done"] == 0
    assert events[-1]["counts"]["failed"] == 1

    # The short file really is on the device — this is exactly why `exists` is not
    # enough and the size has to be compared.
    landed = fake_kindle.mount / "documents" / "en" / "The Blade Itself.azw3"
    assert landed.is_file()
    assert landed.stat().st_size != blade.stat().st_size


# --- the journal ---------------------------------------------------------------------------


def test_the_journal_records_what_was_added(fake_kindle, tmp_path, capsys, ffmpeg_path):
    root = tmp_path / "media"
    blade = _plant_source(
        tmp_path / "books",
        "The Blade Itself.azw3",
        book_id=BLADE_ID,
        title=BLADE_TITLE,
        author=BLADE_AUTHOR,
        language="en",
    )
    _plant_cover(root, BLADE_ID, ffmpeg_path)

    args = _add_args(root, str(blade))
    assert (
        kindle_cli.run_add(
            args, device_finder=lambda: fake_kindle, backend_factory=_mass_storage_factory
        )
        == EXIT_OK
    )

    entries = backup_module.journal_read(root, fake_kindle.serial)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["op"] == "add"
    assert "documents/en/The Blade Itself.azw3" in entry["paths"]
    assert f"system/thumbnails/thumbnail_{BLADE_ID}_EBOK_portrait.jpg" in entry["paths"]
    # The snapshot that protects this operation is named, so an undo knows which
    # backup the device looked like before it ran.
    assert entry["snapshot"]
    # ...and the same operation id is handed to the caller, for `restore --op`.
    assert _events(capsys)[-1]["data"]["operation"] == entry["id"]


# --- selection -------------------------------------------------------------------------------


def test_match_narrows_by_the_books_own_title_even_when_the_filename_says_nothing(
    fake_kindle, tmp_path, capsys, ffmpeg_path
):
    root = tmp_path / "media"
    books = tmp_path / "books"
    blade = _plant_source(
        books,
        "tmp1603.azw3",  # an opaque filename, like a real library's leftovers
        book_id=BLADE_ID,
        title=BLADE_TITLE,
        author=BLADE_AUTHOR,
        language="en",
    )
    other = _plant_source(
        books,
        "Um Livro Novo.azw3",
        book_id=LIVRO_ID,
        title="Um Livro Novo",
        author="Um Autor",
        language="pt",
    )
    _plant_cover(root, BLADE_ID, ffmpeg_path)

    args = _add_args(root, str(blade), str(other), "--match", "someone else")
    exit_code = kindle_cli.run_add(
        args, device_finder=lambda: fake_kindle, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_OK

    inputs = [e["input"] for e in _events(capsys) if e["type"] == "item"]
    assert inputs == [str(blade)]
    assert (fake_kindle.mount / "documents" / "en" / "tmp1603.azw3").is_file()
    assert not (fake_kindle.mount / "documents" / "pt" / "Um Livro Novo.azw3").exists()


def test_a_name_already_taken_on_the_device_by_another_book_is_refused_not_overwritten(
    fake_kindle, tmp_path, capsys
):
    """Spec 8.8's case-insensitive collision check. The device file is a DIFFERENT
    book (different EXTH 113 id), so the id comparison cannot skip it — without this
    guard the add would silently overwrite a book the user still has."""
    mount = fake_kindle.mount
    occupied = mount / "documents" / "en" / "The Blade Itself.azw3"
    occupied.write_bytes(
        _mobi_bytes(book_id="SOMEOTHERBOOK001", title="Some Other Book", language="en")
    )
    before = occupied.read_bytes()

    blade = _plant_source(
        tmp_path / "books",
        "the blade itself.AZW3",  # FAT32 compares case-insensitively; so does this
        book_id=BLADE_ID,
        title=BLADE_TITLE,
        author=BLADE_AUTHOR,
        language="en",
    )
    args = _add_args(tmp_path / "media", str(blade))
    exit_code = kindle_cli.run_add(
        args, device_finder=lambda: fake_kindle, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_FAILED

    item = next(e for e in _events(capsys) if e["type"] == "item")
    assert item["status"] == "failed"
    assert item["reason"] == "output_collision"
    assert occupied.read_bytes() == before


def test_two_sources_resolving_to_one_device_path_do_not_overwrite_each_other(
    fake_kindle, tmp_path, capsys, ffmpeg_path
):
    root = tmp_path / "media"
    first = _plant_source(
        tmp_path / "one",
        "Shared Name.azw3",
        book_id="FIRSTBOOK0000001",
        title="First",
        author="A",
        language="en",
    )
    second = _plant_source(
        tmp_path / "two",
        "Shared Name.azw3",
        book_id="SECONDBOOK000001",
        title="Second",
        author="B",
        language="en",
        padding=64,
    )
    _plant_cover(root, "FIRSTBOOK0000001", ffmpeg_path)

    args = _add_args(root, str(first), str(second))
    exit_code = kindle_cli.run_add(
        args, device_finder=lambda: fake_kindle, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_FAILED

    items = {e["input"]: e for e in _events(capsys) if e["type"] == "item"}
    assert items[str(first)]["status"] == "done"
    assert items[str(second)]["status"] == "failed"
    assert items[str(second)]["reason"] == "output_collision"
    # The first book's bytes are what landed — the second never touched them.
    assert (
        fake_kindle.mount / "documents" / "en" / "Shared Name.azw3"
    ).read_bytes() == first.read_bytes()


def test_a_batch_whose_recorded_output_has_vanished_reports_source_missing(
    fake_kindle, tmp_path, capsys
):
    root = tmp_path / "media"
    batch_dir = root / "library"
    batch_dir.mkdir(parents=True)
    (batch_dir / "run.json").write_text(
        json.dumps(
            {
                "v": 1,
                "items": [
                    {
                        "id": 1,
                        "status": "done",
                        "data": {
                            "book_id": LIVRO_ID,
                            "language": "pt",
                            "output": str(tmp_path / "gone" / "Um Livro Novo.azw3"),
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    args = _add_args(root, "--batch", "library")
    exit_code = kindle_cli.run_add(
        args, device_finder=lambda: fake_kindle, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_FAILED

    item = next(e for e in _events(capsys) if e["type"] == "item")
    assert item["status"] == "failed"
    assert item["reason"] == "source_missing"


def test_lang_overrides_the_books_own_language_folder(fake_kindle, tmp_path, ffmpeg_path):
    root = tmp_path / "media"
    livro = _plant_source(
        tmp_path / "books",
        "Um Livro Novo.azw3",
        book_id=LIVRO_ID,
        title="Um Livro Novo",
        author="Um Autor",
        language="pt",
    )
    _plant_cover(root, LIVRO_ID, ffmpeg_path)

    args = _add_args(root, str(livro), "--lang", "es")
    assert (
        kindle_cli.run_add(
            args, device_finder=lambda: fake_kindle, backend_factory=_mass_storage_factory
        )
        == EXIT_OK
    )
    assert (fake_kindle.mount / "documents" / "es" / "Um Livro Novo.azw3").is_file()
    assert not (fake_kindle.mount / "documents" / "pt" / "Um Livro Novo.azw3").exists()


def test_an_unusable_lang_is_a_usage_error_rather_than_a_folder_name(tmp_path):
    """A value the USER typed is refused outright — unlike a language read off a book,
    which quietly falls back to `documents/unknown/`. Silently shelving their books
    somewhere else is worse than saying the code was not understood."""
    args = _add_args(tmp_path / "media", "book.azw3", "--lang", "../escape")
    with pytest.raises(UsageError):
        kindle_cli.run_add(args, device_finder=lambda: None, backend_factory=None)


def test_a_book_with_no_usable_language_lands_under_documents_unknown(
    fake_kindle, tmp_path, ffmpeg_path
):
    root = tmp_path / "media"
    source = _plant_source(
        tmp_path / "books",
        "No Language.azw3",
        book_id="NOLANGBOOK000001",
        title="No Language",
        author="Some Author",
        language="zxx",  # a real code, but not the two/three letters a folder needs
    )
    _plant_cover(root, "NOLANGBOOK000001", ffmpeg_path)

    args = _add_args(root, str(source))
    assert (
        kindle_cli.run_add(
            args, device_finder=lambda: fake_kindle, backend_factory=_mass_storage_factory
        )
        == EXIT_OK
    )
    assert (
        fake_kindle.mount / "documents" / kindle_cli.UNKNOWN_LANGUAGE / "No Language.azw3"
    ).is_file()


def test_batch_takes_its_books_and_their_languages_from_an_ebook_build_run(
    fake_kindle, tmp_path, capsys, ffmpeg_path
):
    root = tmp_path / "media"
    livro = _plant_source(
        tmp_path / "books",
        "Um Livro Novo.azw3",
        book_id=LIVRO_ID,
        title="Um Livro Novo",
        author="Um Autor",
        language="pt",
    )
    _plant_cover(root, LIVRO_ID, ffmpeg_path)
    batch_dir = root / "library"
    batch_dir.mkdir(parents=True)
    (batch_dir / "run.json").write_text(
        json.dumps(
            {
                "v": 1,
                "task": "ebook",
                "batch": "library",
                "status": "done",
                "items": [
                    {
                        "id": 1,
                        "status": "done",
                        "data": {
                            "book_id": LIVRO_ID,
                            "title": "Um Livro Novo",
                            "author": "Um Autor",
                            "language": "pt",
                            "output": str(livro),
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    args = _add_args(root, "--batch", "library")
    assert (
        kindle_cli.run_add(
            args, device_finder=lambda: fake_kindle, backend_factory=_mass_storage_factory
        )
        == EXIT_OK
    )
    assert (fake_kindle.mount / "documents" / "pt" / "Um Livro Novo.azw3").is_file()
    assert _events(capsys)[-1]["counts"]["done"] == 1


# --- naming ------------------------------------------------------------------------------------


def test_an_illegal_fat32_name_is_sanitised_and_the_whole_device_path_is_capped(
    fake_kindle, tmp_path, ffmpeg_path
):
    root = tmp_path / "media"
    # Long enough that `documents/en/<name>` overruns the 250-character device path
    # budget, but still under the host filesystem's own 255-byte name limit.
    long_stem = "A" * 230
    source = _plant_source(
        tmp_path / "books",
        f'{long_stem}: a "tale"?.azw3',
        book_id="LONGNAMEBOOK0001",
        title="A Long One",
        author="Some Author",
        language="en",
    )

    args = _add_args(root, str(source))
    assert (
        kindle_cli.run_add(
            args, device_finder=lambda: fake_kindle, backend_factory=_mass_storage_factory
        )
        == EXIT_OK
    )

    # The fixture's own `A Book - An Author.azw3` is already in this folder; the added
    # book is the only other one, recognisable by the stem it was given.
    names = [
        path.name
        for path in sorted((fake_kindle.mount / "documents" / "en").iterdir())
        if path.is_file() and path.name.startswith("AAA")
    ]
    assert len(names) == 1
    name = names[0]
    assert not any(ch in name for ch in ':"?')
    assert name.endswith(".azw3")
    # The FULL device path, not just the name component, is what FAT32/MTP cap.
    assert len(f"documents/en/{name}") <= kindle_cli.MAX_DEVICE_PATH["mass_storage"]


# --- `detail` is a machine-readable vocabulary, not prose ------------------------------


def test_every_detail_this_command_produces_starts_with_a_vocabulary_term(
    fake_kindle, tmp_path, capsys
):
    """One run that hits four different causes at once. The closed registry has a
    single `engine_error` covering several of them, so the term before the colon is
    what an agent branches on — and it must be one of a known, finite set."""
    root = tmp_path / "media"
    books = tmp_path / "books"
    gone = books / "Vanished.azw3"
    gone.parent.mkdir(parents=True, exist_ok=True)
    first = _plant_source(
        books, "Twin.azw3", book_id="TWINONE000000001", title="One", author="A", language="en"
    )
    second_dir = tmp_path / "other"
    second = _plant_source(
        second_dir, "Twin.azw3", book_id="TWINTWO000000001", title="Two", author="B", language="en"
    )
    big = _plant_source(
        books,
        "Huge.azw3",
        book_id="HUGEBOOK00000001",
        title="Huge",
        author="C",
        language="en",
        padding=40_000,
    )

    args = _add_args(root, str(gone), str(first), str(second), str(big))
    # `gone` must exist at argument-validation time and vanish before the plan reads
    # it — the window `source_missing` is about.
    gone.write_bytes(_mobi_bytes(book_id="GONEBOOK00000001", title="Gone", language="en"))

    def finder():
        gone.unlink()
        return fake_kindle

    exit_code = kindle_cli.run_add(
        args,
        device_finder=finder,
        backend_factory=lambda d, *, cache_dir: _FixedFreeSpaceBackend(d.mount, free=5_000),
    )
    assert exit_code == EXIT_FAILED

    events = _events(capsys)
    items = {e["input"]: e for e in events if e["type"] == "item"}
    terms = {
        path: item["detail"].split(":", 1)[0]
        for path, item in items.items()
        if item["detail"] is not None
    }
    assert terms[str(gone)] == kindle_cli.DETAIL_SOURCE_MISSING
    assert terms[str(second)] == kindle_cli.DETAIL_OUTPUT_COLLISION
    assert terms[str(big)] == kindle_cli.DETAIL_OUT_OF_SPACE
    assert set(terms.values()) <= kindle_cli.DETAIL_TERMS
    # `first` was placed, so it has no detail at all — present as a key, null as a
    # value, never absent.
    assert items[str(first)]["detail"] is None


def test_an_unknown_detail_term_is_refused_the_way_an_unknown_reason_is():
    """The vocabulary is only useful if it cannot be typo'd into something an agent
    silently stops matching — the same reason `Reporter` refuses an unknown `reason`."""
    with pytest.raises(KeyError):
        kindle_cli._detail("out-of-space", "close, but not the term")


# --- provenance: an id-less book is recognised on a re-run -------------------------------


def test_a_re_added_epub_is_skipped_by_provenance_not_refused_as_a_collision(
    fake_kindle, tmp_path, capsys
):
    """An `.epub` carries no EXTH 113, so the id check cannot recognise it. The
    journal says this tool put this exact file at this exact path, and the device
    still holds it at the size it was sent — the CONJUNCTION, which is provenance, not
    a filename comparison. Neither half alone would do: the journal alone would skip a
    book the user has since deleted, and the listing alone is a name match."""
    root = tmp_path / "media"
    source = tmp_path / "books" / "Some Book.epub"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"PK\x03\x04" + b"epub bytes" * 40)

    first = kindle_cli.run_add(
        _add_args(root, str(source)),
        device_finder=lambda: fake_kindle,
        backend_factory=_mass_storage_factory,
    )
    assert first == EXIT_OK
    landed = fake_kindle.mount / "documents" / kindle_cli.UNKNOWN_LANGUAGE / "Some Book.epub"
    assert landed.is_file()
    capsys.readouterr()

    second = kindle_cli.run_add(
        _add_args(root, str(source)),
        device_finder=lambda: fake_kindle,
        backend_factory=_mass_storage_factory,
    )
    assert second == EXIT_OK
    item = next(e for e in _events(capsys) if e["type"] == "item")
    assert item["status"] == "skipped"
    assert item["reason"] == "exists"
    assert item["detail"].startswith(f"{kindle_cli.DETAIL_EXISTS}: ")


def test_provenance_does_not_skip_a_book_the_user_has_since_deleted(fake_kindle, tmp_path, capsys):
    """The listing half of the conjunction. The journal still remembers the placement;
    the device no longer has the file, so it is copied again."""
    root = tmp_path / "media"
    source = tmp_path / "books" / "Some Book.epub"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"PK\x03\x04" + b"epub bytes" * 40)

    assert (
        kindle_cli.run_add(
            _add_args(root, str(source)),
            device_finder=lambda: fake_kindle,
            backend_factory=_mass_storage_factory,
        )
        == EXIT_OK
    )
    landed = fake_kindle.mount / "documents" / kindle_cli.UNKNOWN_LANGUAGE / "Some Book.epub"
    landed.unlink()
    capsys.readouterr()

    assert (
        kindle_cli.run_add(
            _add_args(root, str(source)),
            device_finder=lambda: fake_kindle,
            backend_factory=_mass_storage_factory,
        )
        == EXIT_OK
    )
    item = next(e for e in _events(capsys) if e["type"] == "item")
    assert item["status"] == "done"
    assert landed.is_file()


def test_a_short_write_is_copied_again_on_the_next_run_not_skipped(fake_kindle, tmp_path, capsys):
    """The journal records the size that was SENT, so a book that landed short fails
    the conjunction next time instead of being mistaken for a completed placement."""
    root = tmp_path / "media"
    source = tmp_path / "books" / "Some Book.epub"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"PK\x03\x04" + b"epub bytes" * 40)

    assert (
        kindle_cli.run_add(
            _add_args(root, str(source)),
            device_finder=lambda: fake_kindle,
            backend_factory=lambda d, *, cache_dir: _ShortWriteBackend(d.mount),
        )
        == EXIT_FAILED
    )
    capsys.readouterr()

    assert (
        kindle_cli.run_add(
            _add_args(root, str(source)),
            device_finder=lambda: fake_kindle,
            backend_factory=_mass_storage_factory,
        )
        == EXIT_OK
    )
    item = next(e for e in _events(capsys) if e["type"] == "item")
    assert item["status"] == "done"
    landed = fake_kindle.mount / "documents" / kindle_cli.UNKNOWN_LANGUAGE / "Some Book.epub"
    assert landed.read_bytes() == source.read_bytes()


# --- an interrupted run still records what it put on the device --------------------------


class _InterruptingBackend:
    """Writes the first book, then raises `KeyboardInterrupt` — exactly what
    `MassStorageBackend.write` re-raises when a user hits Ctrl+C mid-copy."""

    def __init__(self, mount: Path, *, after: int = 1) -> None:
        self._inner = massstorage.MassStorageBackend(mount)
        self._after = after
        self.writes = 0

    def list_files(self, prefix: str = ""):
        return self._inner.list_files(prefix)

    def read(self, path: str, dest: Path) -> None:
        self._inner.read(path, dest)

    def read_many(self, items) -> None:
        self._inner.read_many(items)

    def write(self, local: Path, path: str) -> None:
        self.writes += 1
        if self.writes > self._after:
            raise KeyboardInterrupt
        self._inner.write(local, path)

    def remove(self, path: str) -> None:
        self._inner.remove(path)

    def exists(self, path: str) -> bool:
        return self._inner.exists(path)

    def free_space(self) -> int:
        return self._inner.free_space()

    def eject(self) -> None:
        self._inner.eject()

    def close(self) -> None:
        self._inner.close()


def test_an_interrupted_add_still_journals_the_books_it_already_placed(
    fake_kindle, tmp_path, capsys
):
    """Ctrl+C between the first successful write and the journal line used to leave
    books on the device with no record of how they got there, while the run reported
    all-zero counts and `outputs: []` — nothing for a later `restore --op` to undo."""
    root = tmp_path / "media"
    books = tmp_path / "books"
    first = _plant_source(
        books, "First.azw3", book_id="FIRSTBOOK0000001", title="First", author="A", language="en"
    )
    second = _plant_source(
        books, "Second.azw3", book_id="SECONDBOOK000001", title="Second", author="B", language="en"
    )

    exit_code = kindle_cli.run_add(
        _add_args(root, str(first), str(second)),
        device_finder=lambda: fake_kindle,
        backend_factory=lambda d, *, cache_dir: _InterruptingBackend(d.mount, after=1),
    )
    assert exit_code == 130

    events = _events(capsys)
    assert any(e["type"] == "error" and e["code"] == "interrupted" for e in events)
    assert events[-1]["type"] == "result"

    # The first book really is on the device...
    assert (fake_kindle.mount / "documents" / "en" / "First.azw3").is_file()
    assert not (fake_kindle.mount / "documents" / "en" / "Second.azw3").exists()
    # ...and the journal says so, which is the whole point.
    entries = backup_module.journal_read(root, fake_kindle.serial)
    assert len(entries) == 1
    assert entries[0]["paths"] == ["documents/en/First.azw3"]
    assert entries[0]["books"][0]["source"] == str(first.resolve())
    assert entries[0]["books"][0]["size"] == first.stat().st_size
    assert len(entries[0]["books"][0]["sha256"]) == 64


# --- --dry-run ------------------------------------------------------------------------------


def test_dry_run_reports_a_plan_takes_no_backup_and_writes_nothing(fake_kindle, tmp_path, capsys):
    root = tmp_path / "media"
    books = tmp_path / "books"
    new_book = _plant_source(
        books, "New Book.azw3", book_id="NEWBOOK000000001", title="New", author="A", language="en"
    )
    # A second source that the device already holds, so the plan has both verdicts.
    (fake_kindle.mount / "documents" / "en" / "Already There.azw3").write_bytes(
        _mobi_bytes(book_id="OLDBOOK000000001", title="Old", author="B", language="en")
    )
    known = _plant_source(
        books, "Known.azw3", book_id="OLDBOOK000000001", title="Old", author="B", language="en"
    )

    exit_code = kindle_cli.run_add(
        _add_args(root, str(new_book), str(known), "--dry-run"),
        device_finder=lambda: fake_kindle,
        backend_factory=_mass_storage_factory,
    )
    assert exit_code == EXIT_OK

    events = _events(capsys)
    assert [e["stage"] for e in events if e["type"] == "stage"] == ["detect", "plan"]
    items = {e["input"]: e for e in events if e["type"] == "item"}
    assert items[str(new_book)]["status"] == "pending"
    assert items[str(known)]["status"] == "skipped"
    assert items[str(known)]["reason"] == "exists"

    result = events[-1]
    assert result["pending"] == [str(new_book)]
    # The same keys a real run reports, with a missing VALUE where a dry run has
    # nothing to say — never a missing key.
    assert result["data"]["snapshot"] is None
    assert result["data"]["operation"] is None
    assert result["data"]["thumbnails"] == {}

    # Nothing was written to the DEVICE, and no backup was taken. (Host-side
    # planning caches under `_kindle/<serial>/.cache/` are still filled, exactly as
    # `thumbnails --dry-run` fills its own — they are reads, not writes.)
    assert not (fake_kindle.mount / "documents" / "en" / "New Book.azw3").exists()
    assert not (root / "_kindle" / fake_kindle.serial / "backups").exists()
    assert backup_module.journal_read(root, fake_kindle.serial) == []


# --- MTP: no mount, a tighter path budget, and one index instead of a rescan ---------------


class _FakeMtpBackend:
    """Enough of `DeviceBackend` for `add` over MTP, backed by an in-memory
    `{path: bytes}` map. Records every `read_many` call so a test can prove `add` does
    NOT re-fetch the whole library to collect EXTH ids on a second run."""

    def __init__(self, files: dict[str, bytes] | None = None, *, free: int = 10_000_000) -> None:
        self.files = dict(files or {})
        self._free = free
        self.read_many_calls: list[list[str]] = []

    def list_files(self, prefix: str = ""):
        entries = [
            DeviceFile(path=path, size=len(data), mtime=1_700_000_000.0)
            for path, data in sorted(self.files.items())
        ]
        if not prefix:
            return entries
        head = prefix.strip("/")
        return [e for e in entries if e.path == head or e.path.startswith(f"{head}/")]

    def read(self, path: str, dest: Path) -> None:
        if path not in self.files:
            raise FileNotFoundError(path)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(self.files[path])

    def read_many(self, items) -> None:
        self.read_many_calls.append([path for path, _dest in items])
        for path, dest in items:
            self.read(path, dest)

    def write(self, local: Path, path: str) -> None:
        validate_writable_path(path)
        self.files[path] = Path(local).read_bytes()

    def remove(self, path: str) -> None:
        del self.files[path]

    def exists(self, path: str) -> bool:
        return path in self.files

    def free_space(self) -> int:
        return self._free

    def eject(self) -> None:
        pass

    def close(self) -> None:
        pass


def _mtp_device(serial: str = "MTPTESTSERIAL01", mode: str = "mtp") -> Device:
    return Device(serial=serial, product_id=0x9981, mode=mode, mount=None)


def test_add_over_mtp_places_books_without_a_mount(tmp_path, capsys, ffmpeg_path):
    """MTP has no mounted filesystem at all, so every device read goes through the
    backend — including reading the existing books' EXTH ids, which needs a local copy
    of each one first."""
    root = tmp_path / "media"
    on_device = _mobi_bytes(book_id="ONDEVICE00000001", title="On Device", language="en")
    backend = _FakeMtpBackend({"documents/en/Already.azw3": on_device})
    device = _mtp_device()

    source = _plant_source(
        tmp_path / "books",
        "Brand New.azw3",
        book_id="BRANDNEW00000001",
        title="Brand New",
        author="A",
        language="en",
    )
    _plant_cover(root, "BRANDNEW00000001", ffmpeg_path)

    exit_code = kindle_cli.run_add(
        _add_args(root, str(source)),
        device_finder=lambda: device,
        backend_factory=lambda d, *, cache_dir: backend,
    )
    assert exit_code == EXIT_OK
    assert backend.files["documents/en/Brand New.azw3"] == source.read_bytes()
    assert "system/thumbnails/thumbnail_BRANDNEW00000001_EBOK_portrait.jpg" in backend.files
    assert _events(capsys)[-1]["counts"]["done"] == 1


def test_add_over_mtp_skips_a_book_already_there_by_its_id(tmp_path, capsys):
    root = tmp_path / "media"
    backend = _FakeMtpBackend(
        {
            "documents/en/Named Nothing Like It.azw3": _mobi_bytes(
                book_id="SAMEBOOK00000001", title="Same", language="en"
            )
        }
    )
    source = _plant_source(
        tmp_path / "books",
        "Some Book.azw3",
        book_id="SAMEBOOK00000001",
        title="Same",
        author="A",
        language="en",
    )
    exit_code = kindle_cli.run_add(
        _add_args(root, str(source)),
        device_finder=lambda: _mtp_device(),
        backend_factory=lambda d, *, cache_dir: backend,
    )
    assert exit_code == EXIT_OK
    item = next(e for e in _events(capsys) if e["type"] == "item")
    assert item["status"] == "skipped"
    assert item["reason"] == "exists"
    assert "documents/en/Some Book.azw3" not in backend.files


def test_add_over_mtp_reads_each_device_book_once_not_once_per_run(tmp_path, capsys, ffmpeg_path):
    """`exth.read_records` needs a book's header but reads the WHOLE file to reach it,
    so collecting the device's ids the naive way re-fetches the entire library over MTP
    every time one book is added. A persistent index makes the second run fetch only
    what is genuinely new to it."""
    root = tmp_path / "media"
    backend = _FakeMtpBackend(
        {
            "documents/en/One.azw3": _mobi_bytes(book_id="DEVICEONE0000001", language="en"),
            "documents/en/Two.azw3": _mobi_bytes(book_id="DEVICETWO0000001", language="en"),
        }
    )
    device = _mtp_device()
    first_source = _plant_source(
        tmp_path / "books",
        "A.azw3",
        book_id="AAAA000000000001",
        title="A",
        author="A",
        language="en",
    )
    second_source = _plant_source(
        tmp_path / "books",
        "B.azw3",
        book_id="BBBB000000000001",
        title="B",
        author="B",
        language="en",
    )

    assert (
        kindle_cli.run_add(
            _add_args(root, str(first_source)),
            device_finder=lambda: device,
            backend_factory=lambda d, *, cache_dir: backend,
        )
        == EXIT_OK
    )
    # The LAST fetch of a run is always the id-index one: the plan stage runs after
    # the mandatory backup (which does its own fetching), and nothing after the plan
    # reads books off the device. Run 1 knew nothing, so it read both.
    assert backend.read_many_calls[-1] == ["documents/en/One.azw3", "documents/en/Two.azw3"]
    capsys.readouterr()

    assert (
        kindle_cli.run_add(
            _add_args(root, str(second_source)),
            device_finder=lambda: device,
            backend_factory=lambda d, *, cache_dir: backend,
        )
        == EXIT_OK
    )
    # Run 2 re-read ONLY the book run 1 added — never the two it already indexed.
    assert backend.read_many_calls[-1] == ["documents/en/A.azw3"]


def test_the_whole_device_path_is_capped_at_230_over_mtp_not_250(tmp_path):
    """FAT32 allows 250; MTP is tighter. The budget is per BACKEND, not a single
    constant, and `add` is the command that joins a directory onto a name and so is
    the one that has to honour it."""
    root = tmp_path / "media"
    long_stem = "B" * 230
    source = _plant_source(
        tmp_path / "books",
        f"{long_stem}.azw3",
        book_id="LONGMTPBOOK00001",
        title="Long",
        author="A",
        language="en",
    )
    backend = _FakeMtpBackend()
    assert (
        kindle_cli.run_add(
            _add_args(root, str(source)),
            device_finder=lambda: _mtp_device(),
            backend_factory=lambda d, *, cache_dir: backend,
        )
        == EXIT_OK
    )
    landed = [path for path in backend.files if path.startswith("documents/en/B")]
    assert len(landed) == 1
    assert len(landed[0]) <= kindle_cli.MAX_DEVICE_PATH["mtp"]
    assert kindle_cli.MAX_DEVICE_PATH["mtp"] < len(f"documents/en/{long_stem}.azw3")


def test_an_unrecognised_device_mode_falls_back_to_the_stricter_budget(tmp_path):
    """A third backend nobody has written yet must not silently get mass storage's
    looser limit: an unknown mode takes the tightest one this project knows about."""
    root = tmp_path / "media"
    long_stem = "C" * 230
    source = _plant_source(
        tmp_path / "books",
        f"{long_stem}.azw3",
        book_id="UNKNOWNMODE00001",
        title="Long",
        author="A",
        language="en",
    )
    backend = _FakeMtpBackend()
    assert (
        kindle_cli.run_add(
            _add_args(root, str(source)),
            device_finder=lambda: _mtp_device(mode="something-new"),
            backend_factory=lambda d, *, cache_dir: backend,
        )
        == EXIT_OK
    )
    landed = [path for path in backend.files if path.startswith("documents/en/C")]
    assert len(landed[0]) <= kindle_cli.MAX_DEVICE_PATH["mtp"]


# --- selection and naming contracts -----------------------------------------------------------


def test_match_that_hits_nothing_reports_no_items_and_writes_nothing(fake_kindle, tmp_path, capsys):
    root = tmp_path / "media"
    source = _plant_source(
        tmp_path / "books",
        "The Blade Itself.azw3",
        book_id=BLADE_ID,
        title=BLADE_TITLE,
        author=BLADE_AUTHOR,
        language="en",
    )
    exit_code = kindle_cli.run_add(
        _add_args(root, str(source), "--match", "nothing here matches this"),
        device_finder=lambda: fake_kindle,
        backend_factory=_mass_storage_factory,
    )
    # Nothing matched is not a failure — it is a run with no items, the same way a
    # `--match` that selects nothing behaves for `thumbnails`.
    assert exit_code == EXIT_OK

    events = _events(capsys)
    assert [e for e in events if e["type"] == "item"] == []
    result = events[-1]
    assert result["counts"] == {"total": 0, "done": 0, "skipped": 0, "failed": 0, "pending": 0}
    assert result["data"]["books"] == []
    assert not (fake_kindle.mount / "documents" / "en" / "The Blade Itself.azw3").exists()
    # A run that put nothing on the device journals nothing to undo.
    assert result["data"]["operation"] is None
    assert backup_module.journal_read(root, fake_kindle.serial) == []


def test_a_directory_prefix_too_long_for_the_budget_raises_instead_of_overrunning():
    """Unreachable from `run_add`, whose language is two or three letters or
    `UNKNOWN_LANGUAGE` — but this helper is exactly what Task 8's `sync` will reuse
    with a different directory, and a contract that fails loudly is the point of
    stating one."""
    with pytest.raises(ValueError) as error:
        kindle_cli._device_path_for("Book.azw3", "x" * 240, max_path=250)
    assert "budget" in str(error.value)


def test_a_batch_output_in_a_format_no_kindle_reads_is_skipped_not_copied(
    fake_kindle, tmp_path, capsys
):
    """A batch is a scan result, not a file the user pointed at, so an output in some
    other format is filtered out the way a folder scan filters — and never turned into
    a usage error."""
    root = tmp_path / "media"
    keeper = _plant_source(
        tmp_path / "books",
        "Keeper.azw3",
        book_id="KEEPER0000000001",
        title="Keeper",
        author="A",
        language="en",
    )
    odd = tmp_path / "books" / "Notes.txt"
    odd.write_text("not a book", encoding="utf-8")
    batch_dir = root / "library"
    batch_dir.mkdir(parents=True)
    (batch_dir / "run.json").write_text(
        json.dumps(
            {
                "v": 1,
                "items": [
                    {"id": 1, "status": "done", "data": {"language": "en", "output": str(odd)}},
                    {
                        "id": 2,
                        "status": "done",
                        "data": {"language": "en", "output": str(keeper)},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    assert (
        kindle_cli.run_add(
            _add_args(root, "--batch", "library"),
            device_finder=lambda: fake_kindle,
            backend_factory=_mass_storage_factory,
        )
        == EXIT_OK
    )
    inputs = [e["input"] for e in _events(capsys) if e["type"] == "item"]
    assert inputs == [str(keeper)]
    assert not (fake_kindle.mount / "documents" / "en" / "Notes.txt").exists()


# --- provenance authorises overwriting only this tool's own unfinished write ------------


def _add_epub(root: Path, source: Path, kindle, backend_factory=_mass_storage_factory) -> int:
    return kindle_cli.run_add(
        _add_args(root, str(source)),
        device_finder=lambda: kindle,
        backend_factory=backend_factory,
    )


def test_a_users_own_file_at_a_journalled_path_is_refused_not_overwritten(
    fake_kindle, tmp_path, capsys
):
    """The journal says this tool put these bytes at P — but it also says that write
    was VERIFIED, so whatever is at P now differs because something other than this
    tool changed it. A stale journal entry must not be sufficient authorisation to
    overwrite a file the user put there."""
    root = tmp_path / "media"
    source = tmp_path / "books" / "Some Book.epub"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"PK\x03\x04" + b"epub bytes" * 40)

    assert _add_epub(root, source, fake_kindle) == EXIT_OK
    landed = fake_kindle.mount / "documents" / kindle_cli.UNKNOWN_LANGUAGE / "Some Book.epub"
    assert landed.is_file()
    capsys.readouterr()

    # The user replaces it with something of their own, at the same path.
    theirs = b"MY OWN COMPLETELY DIFFERENT FILE"
    landed.write_bytes(theirs)

    assert _add_epub(root, source, fake_kindle) == EXIT_FAILED
    item = next(e for e in _events(capsys) if e["type"] == "item")
    assert item["status"] == "failed"
    assert item["reason"] == "output_collision"
    assert landed.read_bytes() == theirs


def test_an_edited_source_is_not_skipped_as_already_on_the_device(fake_kindle, tmp_path, capsys):
    """Deliberately edited to the SAME SIZE, so only the recorded digest can tell the
    two versions apart. Path-and-size matching alone would report `exists` for a file
    whose contents had changed. It is REFUSED rather than silently replaced: the
    earlier placement verified, so replacing it is a decision for the user, not for a
    re-run that was only asked to add."""
    root = tmp_path / "media"
    source = tmp_path / "books" / "Some Book.epub"
    source.parent.mkdir(parents=True, exist_ok=True)
    original = b"PK\x03\x04" + b"a" * 400
    source.write_bytes(original)

    assert _add_epub(root, source, fake_kindle) == EXIT_OK
    capsys.readouterr()

    edited = b"PK\x03\x04" + b"b" * 400
    assert len(edited) == len(original)
    source.write_bytes(edited)

    assert _add_epub(root, source, fake_kindle) == EXIT_FAILED
    item = next(e for e in _events(capsys) if e["type"] == "item")
    assert item["status"] != "skipped"
    assert item["reason"] == "output_collision"
    landed = fake_kindle.mount / "documents" / kindle_cli.UNKNOWN_LANGUAGE / "Some Book.epub"
    assert landed.read_bytes() == original


def test_the_journal_records_whether_each_write_was_verified(fake_kindle, tmp_path, capsys):
    """The flag the collision waiver turns on. A completed write is `verified: true`;
    a short one is `verified: false`, which is what lets the next run replace it."""
    root = tmp_path / "media"
    good = tmp_path / "books" / "Good.epub"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_bytes(b"PK\x03\x04" + b"good" * 60)

    assert _add_epub(root, good, fake_kindle) == EXIT_OK
    assert backup_module.journal_read(root, fake_kindle.serial)[0]["books"][0]["verified"] is True
    capsys.readouterr()

    short = tmp_path / "books" / "Short.epub"
    short.write_bytes(b"PK\x03\x04" + b"short" * 60)
    assert (
        _add_epub(
            root,
            short,
            fake_kindle,
            backend_factory=lambda d, *, cache_dir: _ShortWriteBackend(d.mount),
        )
        == EXIT_FAILED
    )
    entries = backup_module.journal_read(root, fake_kindle.serial)
    assert entries[-1]["books"][0]["verified"] is False


def test_an_interrupt_while_hashing_still_journals_the_book_already_on_the_device(
    fake_kindle, tmp_path, capsys, monkeypatch
):
    """The provenance hash re-reads the whole source, a window proportional to file
    size that opens AFTER the bytes are already on the device. A Ctrl+C there must not
    lose the record — the journal entry is created before the hash runs and simply
    carries no digest."""
    root = tmp_path / "media"
    source = tmp_path / "books" / "Some Book.epub"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"PK\x03\x04" + b"epub bytes" * 40)

    def interrupted_digest(path):
        raise KeyboardInterrupt

    monkeypatch.setattr(kindle_cli, "_source_digest", interrupted_digest)

    assert _add_epub(root, source, fake_kindle) == 130

    landed = fake_kindle.mount / "documents" / kindle_cli.UNKNOWN_LANGUAGE / "Some Book.epub"
    assert landed.is_file()
    entries = backup_module.journal_read(root, fake_kindle.serial)
    assert len(entries) == 1
    record = entries[0]["books"][0]
    assert record["device_path"] == f"documents/{kindle_cli.UNKNOWN_LANGUAGE}/Some Book.epub"
    assert record["sha256"] == ""
    assert record["verified"] is False


def test_a_record_with_no_digest_never_authorises_a_skip_or_an_overwrite(
    fake_kindle, tmp_path, capsys, monkeypatch
):
    """A journal entry whose hash could not be computed proves nothing about the bytes,
    so it is ignored in both directions rather than trusted on its path alone."""
    root = tmp_path / "media"
    source = tmp_path / "books" / "Some Book.epub"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"PK\x03\x04" + b"epub bytes" * 40)

    monkeypatch.setattr(kindle_cli, "_source_digest", lambda path: "")
    assert _add_epub(root, source, fake_kindle) == EXIT_OK
    assert backup_module.journal_read(root, fake_kindle.serial)[0]["books"][0]["sha256"] == ""
    capsys.readouterr()

    monkeypatch.undo()
    # The device copy is intact and identical, but the digestless record cannot say so.
    assert _add_epub(root, source, fake_kindle) == EXIT_FAILED
    item = next(e for e in _events(capsys) if e["type"] == "item")
    assert item["reason"] == "output_collision"


# --- one corrupt book on the device is not a failed add ---------------------------------


def test_a_device_book_that_will_not_parse_does_not_abort_the_whole_add(
    fake_kindle, tmp_path, capsys, monkeypatch, ffmpeg_path
):
    """`exth.read_records` documents "unreadable -> no records", but it reaches that
    verdict through struct-unpacked offsets a malformed file can send off the end. This
    runs over EVERY book on the device, in the one command where aborting costs the
    most — the backup has already run."""
    corrupt = fake_kindle.mount / "documents" / "en" / "Corrupt.azw3"
    corrupt.write_bytes(b"\x00" * 64)

    real_read_records = kindle_cli.exth.read_records

    def exploding_read_records(path):
        if Path(path).name == "Corrupt.azw3":
            raise struct.error("unpack requires a buffer of 4 bytes")
        return real_read_records(path)

    monkeypatch.setattr(kindle_cli.exth, "read_records", exploding_read_records)

    root = tmp_path / "media"
    source = _plant_source(
        tmp_path / "books",
        "Fine Book.azw3",
        book_id="FINEBOOK00000001",
        title="Fine",
        author="A",
        language="en",
    )
    _plant_cover(root, "FINEBOOK00000001", ffmpeg_path)

    exit_code = kindle_cli.run_add(
        _add_args(root, str(source)),
        device_finder=lambda: fake_kindle,
        backend_factory=_mass_storage_factory,
    )
    assert exit_code == EXIT_OK
    assert (fake_kindle.mount / "documents" / "en" / "Fine Book.azw3").is_file()
    assert _events(capsys)[-1]["counts"]["done"] == 1
