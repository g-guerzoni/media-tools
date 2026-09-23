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
    # The registry has no "out of space" reason, so the human-readable cause rides
    # along on the result's own `failed` entry instead of being lost.
    failed = next(entry for entry in result["failed"] if entry["input"] == str(big))
    assert "space" in failed["detail"].lower()


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
