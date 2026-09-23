"""Backups: the safety feature every writing command depends on.

Nothing here touches a real device. The mass-storage backend is driven against the
`fake_kindle` fixture, and the MTP backend against a fake runner that serves the same
tree — which is what makes the `read_many` parity tests possible at all.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
from pathlib import Path

import pytest

from media_tools.core.events import ERROR_CODES, WARNING_CODES
from media_tools.integrations.calibre import CalibreError
from media_tools.tasks.ebook import exth
from media_tools.tasks.ebook.kindle import backup, massstorage, mtp
from media_tools.tasks.ebook.kindle.detect import Device

BOOK = "documents/en/A Book - An Author.azw3"
SDR = "documents/en/A Book - An Author.sdr/book.mbp"
THUMB = "system/thumbnails/thumbnail_TESTBOOKID01_EBOK_portrait.jpg"
BOOK_ID = "TESTBOOKID01"


# --- fixtures and stand-ins -----------------------------------------------------


def mobi_bytes(book_id: str, *, cdetype: str = "EBOK", filler: bytes = b"") -> bytes:
    """A byte blob `tasks.ebook.exth.read_records` parses, carrying records 113/501.

    Restore pairs a book with its thumbnail through the book's own EXTH id, so the
    test needs a book that actually has one — the fixture's `b"english book"` has not.
    """
    exth_entries = [(113, book_id.encode()), (501, cdetype.encode())]
    blob = b"".join(struct.pack(">II", tag, 8 + len(v)) + v for tag, v in exth_entries)
    exth = b"EXTH" + struct.pack(">I", 12 + len(blob)) + struct.pack(">I", len(exth_entries)) + blob

    header_length = 232
    record = bytearray(b"\0" * (16 + header_length))
    record[16:20] = b"MOBI"
    record[20:24] = struct.pack(">I", header_length)
    record[0x80:0x84] = struct.pack(">I", 0x40)
    record0 = bytes(record) + exth + filler

    palm = bytearray(b"\0" * 94)
    palm[76:78] = struct.pack(">H", 2)
    palm[78:82] = struct.pack(">I", 94)
    palm[86:90] = struct.pack(">I", 94 + len(record0))
    return bytes(palm) + record0 + b"text record"


@pytest.fixture
def kindle(fake_kindle):
    """The shared fixture, with the extras this task's rules are about: a real MOBI
    with an EXTH id, its `.sdr` sidecar content, a matching thumbnail, and one file in
    each area that must never be backed up."""
    mount = fake_kindle.mount
    (mount / BOOK).write_bytes(mobi_bytes(BOOK_ID))
    (mount / SDR).parent.mkdir(parents=True, exist_ok=True)
    (mount / SDR).write_bytes(b"reading position")
    (mount / THUMB).write_bytes(b"thumbnail bytes")
    (mount / "audible" / "Audiobook.aax").write_bytes(b"audible content")
    (mount / "documents" / "en" / "._A Book - An Author.azw3").write_bytes(b"resource fork")
    (mount / ".Trashes").mkdir(exist_ok=True)
    (mount / ".Trashes" / "junk").write_bytes(b"junk")
    (mount / "driveinfo.calibre").write_bytes(b'{"device":"kindle"}')
    return fake_kindle


@pytest.fixture
def mass(kindle):
    return massstorage.MassStorageBackend(kindle.mount)


class TreeRunner:
    """An MTP runner backed by a real directory, so both backends can be driven
    through the same operations and compared. Records every ops list it was given."""

    def __init__(self, mount: Path, *, fail_paths: set[str] | None = None) -> None:
        self.mount = Path(mount)
        self.calls: list[list[dict]] = []
        # Paths whose fetch dies part-way. The real helper writes into the local file
        # and, on any exception, removes it before reporting the failure
        # (`integrations/kindle_mtp.py:_op_get`), so the fake does exactly that — the
        # removal is the behaviour the parity test is about.
        self.fail_paths = set(fail_paths or ())

    def __call__(self, ops: list[dict]) -> dict:
        self.calls.append([dict(op) for op in ops])
        return {
            "v": 1,
            "device": {"serial": "G000TESTSERIAL"},
            "results": [self._run(o) for o in ops],
        }

    def _run(self, op: dict) -> dict:
        if op["op"] == "list":
            return {"op": "list", "ok": True, "files": self._files(op.get("path", ""))}
        if op["op"] == "get":
            source = self.mount / op["path"]
            if not source.is_file():
                return {
                    "op": "get",
                    "ok": False,
                    "code": "not_found",
                    "error": f"{op['path']} is not on the device",
                }
            local = Path(op["local"])
            local.parent.mkdir(parents=True, exist_ok=True)
            if op["path"] in self.fail_paths:
                local.write_bytes(source.read_bytes()[:3])
                local.unlink()
                return {"op": "get", "ok": False, "code": "io", "error": "the fetch died"}
            local.write_bytes(source.read_bytes())
            return {"op": "get", "ok": True, "size": local.stat().st_size}
        if op["op"] == "put":
            target = self.mount / op["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(Path(op["local"]).read_bytes())
            return {"op": "put", "ok": True, "size": target.stat().st_size}
        if op["op"] == "free":
            return {"op": "free", "ok": True, "free": 1 << 30}
        return {"op": op["op"], "ok": True}

    def _files(self, prefix: str) -> list[dict]:
        start = self.mount / prefix if prefix else self.mount
        found = []
        for path in sorted(start.rglob("*")):
            if path.is_file():
                stat = path.stat()
                found.append(
                    {
                        "path": path.relative_to(self.mount).as_posix(),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                )
        return found


def mtp_backend(mount: Path, runner=None):
    device = Device(serial="G000TESTSERIAL", product_id=0x9981, mode="mtp", mount=None)
    return mtp.MtpBackend(
        device,
        runner=runner or TreeRunner(mount),
        cache_dir=Path("/unused"),
        gui_check=lambda: False,
    )


class ExplodingListing:
    """A backend whose root listing fails — the precondition a backup must never
    mistake for an empty device."""

    def list_files(self, prefix: str = ""):
        raise CalibreError("the listing failed part-way through")

    def read_many(self, items) -> None:
        raise AssertionError("a failed listing must never reach the transfer stage")

    def free_space(self) -> int:
        return 1 << 30


class EmptyDevice:
    """A device with no files on it. `answers` is whether it is still THERE — a
    factory-reset Kindle lists zero files and answers `free_space`; a vanished one
    lists zero files and cannot."""

    def __init__(self, *, answers: bool = True) -> None:
        self.answers = answers
        self.written: list[str] = []

    def list_files(self, prefix: str = ""):
        return []

    def free_space(self) -> int:
        if not self.answers:
            raise OSError(2, "No such file or directory")
        return 1 << 30

    def read_many(self, items) -> None:
        assert not items, "there is nothing on the device to transfer"

    def write(self, local: Path, path: str) -> None:
        self.written.append(path)


def manifest_paths(snapshot_dir: Path) -> set[str]:
    data = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    return {entry["path"] for entry in data["files"]}


def manifest_of(snapshot_dir: Path) -> dict:
    return json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))


# --- registry codes -------------------------------------------------------------


def test_the_codes_this_module_relies_on_are_already_registered():
    # The plan called these `backup_hash_from_previous`; the registry's own names win,
    # and nothing in this task may invent a code (an unregistered one raises by design).
    assert "hash_from_previous" in WARNING_CODES
    assert "backup_failed" in ERROR_CODES
    assert backup.HASH_FROM_PREVIOUS_WARNING == "hash_from_previous"
    assert backup.BACKUP_FAILED_ERROR == "backup_failed"


# --- scope ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "documents/en/A Book.azw3",
        "documents/en/A Book.sdr/book.mbp",
        "documents/My Clippings.txt",
        "system/thumbnails/cover.jpg",
        "amazon-cover-bug/cover.jpg",
        "fonts/MyFont.ttf",
        "driveinfo.calibre",
        "My Clippings.txt",
    ],
)
def test_default_scope_takes_user_content(path):
    assert backup.DEFAULT_SCOPE.includes(path)


@pytest.mark.parametrize(
    "path",
    [
        "audible/Audiobook.aax",
        "documents/audible/Audiobook.aax",
        "system/wifi/wifi.cfg",
        "system/com.amazon.ebook.booklet.reader/reader.pref",
        "documents/en/._A Book.azw3",
        "._store",
        ".Trashes/junk",
        ".fseventsd/log",
        ".Spotlight-V100/index",
        "recipes/daily.mobi",
    ],
)
def test_default_scope_refuses_everything_else(path):
    assert not backup.DEFAULT_SCOPE.includes(path)


# --- a first backup -------------------------------------------------------------


def test_a_first_backup_copies_every_in_scope_file_and_writes_a_manifest(mass, tmp_path):
    root = tmp_path / "out"
    snap = backup.snapshot(mass, root=root, serial="G000TESTSERIAL")

    expected = {
        BOOK,
        SDR,
        THUMB,
        "documents/pt/Um Livro - Um Autor.azw3",
        "system/thumbnails/cover.jpg",
        "My Clippings.txt",
        "driveinfo.calibre",
    }
    assert manifest_paths(snap.path) == expected
    assert snap.files == len(expected)
    assert snap.bytes_linked == 0
    assert snap.bytes_copied == sum((mass.mount / p).stat().st_size for p in expected)
    assert snap.manifest == snap.path / "manifest.json"

    for path in expected:
        assert (snap.path / "files" / path).read_bytes() == (mass.mount / path).read_bytes()

    data = manifest_of(snap.path)
    assert data["v"] == 1
    assert data["serial"] == "G000TESTSERIAL"
    assert data["previous"] is None
    assert all(entry["hash_from"] == "computed" for entry in data["files"])
    assert all(len(entry["sha256"]) == 64 for entry in data["files"])

    assert snap.path.parent == root / "_kindle" / "G000TESTSERIAL" / "backups"
    assert backup.latest(root, "G000TESTSERIAL") == snap.path
    assert not snap.path.name.endswith(".partial")


def test_excluded_paths_never_reach_a_manifest(mass, tmp_path):
    snap = backup.snapshot(mass, root=tmp_path / "out", serial="S")
    paths = manifest_paths(snap.path)

    assert not any(p.startswith("audible/") for p in paths)
    assert not any(Path(p).name.startswith("._") for p in paths)
    assert not any(
        p.startswith("system/") and not p.startswith("system/thumbnails/") for p in paths
    )
    assert not any(p.startswith((".Trashes/", ".fseventsd/", ".Spotlight-V100/")) for p in paths)
    # And nothing excluded was written to disk either, not just left out of the manifest.
    stored = {
        p.relative_to(snap.path / "files").as_posix()
        for p in (snap.path / "files").rglob("*")
        if p.is_file()
    }
    assert stored == paths


# --- incremental ----------------------------------------------------------------


def test_a_second_backup_hard_links_everything_that_did_not_change(mass, tmp_path):
    root = tmp_path / "out"
    first = backup.snapshot(mass, root=root, serial="S")

    changed = mass.mount / "documents/pt/Um Livro - Um Autor.azw3"
    changed.write_bytes(b"a longer, rewritten book")

    second = backup.snapshot(mass, root=root, serial="S")

    unchanged = second.path / "files" / BOOK
    assert unchanged.stat().st_nlink > 1
    assert unchanged.samefile(first.path / "files" / BOOK)

    rewritten = second.path / "files" / "documents/pt/Um Livro - Um Autor.azw3"
    assert rewritten.stat().st_nlink == 1
    assert rewritten.read_bytes() == b"a longer, rewritten book"

    assert second.bytes_copied == len(b"a longer, rewritten book")
    # Summed on `origin`, not on `linked`: on a filesystem without hard links the
    # reuse falls back to a local copy, which is still reuse and still not a device read.
    assert second.bytes_linked == sum(
        entry["size"] for entry in manifest_of(second.path)["files"] if entry["origin"] != "device"
    )
    assert second.bytes_linked > 0
    assert manifest_of(second.path)["previous"] == first.path.name
    assert backup.latest(root, "S") == second.path


def test_a_dst_shifted_mtime_links_instead_of_recopying(mass, tmp_path):
    """FAT stores local time, so a DST change shifts every mtime by exactly an hour.
    Treating that as "changed" would re-copy the whole library twice a year."""
    root = tmp_path / "out"
    backup.snapshot(mass, root=root, serial="S")

    for path in sorted(mass.mount.rglob("*")):
        if path.is_file():
            stat = path.stat()
            os.utime(path, (stat.st_atime, stat.st_mtime + 3600))

    second = backup.snapshot(mass, root=root, serial="S")
    # Everything links EXCEPT `.sdr` content, which is exempt from the whole-hour
    # clause — a sidecar is rewritten in place as you read, often at an identical size.
    assert [e["path"] for e in manifest_of(second.path)["files"] if e["origin"] == "device"] == [
        SDR
    ]
    assert second.bytes_copied == len(b"reading position")
    assert second.bytes_linked > 0
    assert (second.path / "files" / BOOK).stat().st_nlink > 1


def test_an_mtime_shifted_by_more_than_max_dst_hours_is_recopied(mass, tmp_path):
    """The clause exists for DST, which never needs more than an hour. Unbounded, it
    would forgive a 24-hour or a year-long gap for any file of an unchanged size."""
    root = tmp_path / "out"
    backup.snapshot(mass, root=root, serial="S")

    book = mass.mount / BOOK
    stat = book.stat()
    os.utime(book, (stat.st_atime, stat.st_mtime + 24 * 3600))

    second = backup.snapshot(mass, root=root, serial="S")
    assert (second.path / "files" / BOOK).stat().st_nlink == 1
    assert second.bytes_copied == book.stat().st_size


def test_sdr_content_is_never_linked_on_a_whole_hour_shift(mass, tmp_path):
    """The realistic false link. A sidecar is rewritten in place as you read, often at
    a byte-identical size, and people read at roughly the same time each day — so an
    edit landing within two seconds of an hour boundary is a real event, not a
    theoretical one, and it would silently carry a stale reading position forward."""
    root = tmp_path / "out"
    backup.snapshot(mass, root=root, serial="S")

    sidecar = mass.mount / SDR
    sidecar.write_bytes(b"newer position!!")  # same length as b"reading position"
    stat = sidecar.stat()
    os.utime(sidecar, (stat.st_atime, stat.st_mtime + 3600))
    # The book beside it gets the same shift and MUST still link.
    book_stat = (mass.mount / BOOK).stat()
    os.utime(mass.mount / BOOK, (book_stat.st_atime, book_stat.st_mtime + 3600))

    second = backup.snapshot(mass, root=root, serial="S")
    assert (second.path / "files" / SDR).read_bytes() == b"newer position!!"
    assert (second.path / "files" / SDR).stat().st_nlink == 1
    assert (second.path / "files" / BOOK).stat().st_nlink > 1


def test_a_changed_mtime_that_is_not_a_whole_hour_is_recopied(mass, tmp_path):
    root = tmp_path / "out"
    backup.snapshot(mass, root=root, serial="S")

    book = mass.mount / BOOK
    stat = book.stat()
    os.utime(book, (stat.st_atime, stat.st_mtime + 600))

    second = backup.snapshot(mass, root=root, serial="S")
    assert second.bytes_copied == book.stat().st_size
    assert (second.path / "files" / BOOK).stat().st_nlink == 1


def test_full_ignores_the_previous_snapshot_entirely(mass, tmp_path):
    root = tmp_path / "out"
    first = backup.snapshot(mass, root=root, serial="S")
    second = backup.snapshot(mass, root=root, serial="S", full=True)

    assert second.bytes_linked == 0
    assert second.bytes_copied == first.bytes_copied
    assert (second.path / "files" / BOOK).stat().st_nlink == 1


# --- carried-over hashes --------------------------------------------------------


def test_a_carried_over_hash_is_recorded_and_warned_about_once_per_run(mass, tmp_path):
    root = tmp_path / "out"
    backup.snapshot(mass, root=root, serial="S")

    seen: list[tuple[str, str]] = []
    second = backup.snapshot(
        mass, root=root, serial="S", on_warning=lambda code, message: seen.append((code, message))
    )

    carried = [e for e in manifest_of(second.path)["files"] if e["hash_from"] == "previous"]
    assert len(carried) > 1, "several files carried a hash over"
    assert [code for code, _ in seen] == ["hash_from_previous"], "warned once, not once per file"
    assert second.warnings == ("hash_from_previous",)


def test_verify_hashes_recomputes_every_hash_instead_of_carrying_one_over(mass, tmp_path):
    root = tmp_path / "out"
    backup.snapshot(mass, root=root, serial="S")

    seen: list[str] = []
    second = backup.snapshot(
        mass,
        root=root,
        serial="S",
        verify_hashes=True,
        on_warning=lambda code, message: seen.append(code),
    )

    data = manifest_of(second.path)
    assert all(entry["hash_from"] == "computed" for entry in data["files"])
    assert seen == []
    assert second.warnings == ()
    # Recomputing is not a reason to re-fetch: the bytes are already local.
    assert second.bytes_copied == 0


def test_verify_hashes_fails_when_a_stored_file_no_longer_matches_its_hash(mass, tmp_path):
    """The flag's whole value. A reused file is usually a hard link to the previous
    snapshot's inode, so recomputing its digest without COMPARING it would re-read the
    same bytes and write down whatever they now produce — rot included, recorded as
    `"computed"` and reported as a success."""
    root = tmp_path / "out"
    first = backup.snapshot(mass, root=root, serial="S")

    rotted = first.path / "files" / "My Clippings.txt"
    assert rotted.read_bytes() == b"clippings"
    rotted.write_bytes(b"ROTTEDxxx")  # same length, so the file still looks unchanged

    with pytest.raises(backup.BackupFailed) as error:
        backup.snapshot(mass, root=root, serial="S", verify_hashes=True)
    assert "My Clippings.txt" in str(error.value)
    assert first.path.name in str(error.value)

    # The damaged snapshot is reported, not replaced: `latest` still points at it and
    # the failed run left nothing that looks finished.
    assert backup.latest(root, "S") == first.path
    backups = backup.backup_root(root, "S") / "backups"
    assert [q.name for q in backups.iterdir() if q.is_dir()] == [first.path.name]


def test_verify_hashes_passes_when_the_stored_bytes_still_agree(mass, tmp_path):
    root = tmp_path / "out"
    first = backup.snapshot(mass, root=root, serial="S")
    second = backup.snapshot(mass, root=root, serial="S", verify_hashes=True)

    before = {e["path"]: e["sha256"] for e in manifest_of(first.path)["files"]}
    after = {e["path"]: e["sha256"] for e in manifest_of(second.path)["files"]}
    assert after == before, "verifying agrees with what it verified against"
    assert all(e["hash_from"] == "computed" for e in manifest_of(second.path)["files"])


def test_the_hard_link_fallback_copies_from_the_previous_snapshot(mass, tmp_path, monkeypatch):
    """exFAT, a network share, or a link count at its limit. Reuse must fall back to a
    LOCAL copy of the previous snapshot, never to a re-read of the device."""
    root = tmp_path / "out"
    first = backup.snapshot(mass, root=root, serial="S")

    def no_links(source, target):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(backup.os, "link", no_links)
    second = backup.snapshot(mass, root=root, serial="S")

    assert second.bytes_copied == 0, "the device was not re-read"
    assert second.bytes_linked > 0
    copied = second.path / "files" / BOOK
    assert copied.stat().st_nlink == 1
    assert copied.read_bytes() == (first.path / "files" / BOOK).read_bytes()
    entries = {e["path"]: e for e in manifest_of(second.path)["files"]}
    assert entries[BOOK]["origin"] == "previous-copy"
    assert entries[BOOK]["linked"] is False


# --- failure and interruption ---------------------------------------------------


def test_a_failed_listing_aborts_the_backup_instead_of_writing_an_empty_snapshot(tmp_path):
    root = tmp_path / "out"
    with pytest.raises(backup.BackupFailed):
        backup.snapshot(ExplodingListing(), root=root, serial="S")

    assert backup.latest(root, "S") is None
    backups = backup.backup_root(root, "S") / "backups"
    assert not backups.exists() or list(backups.iterdir()) == []


def test_a_failed_mtp_listing_aborts_the_backup(kindle, tmp_path):
    def failing(ops):
        return {
            "v": 1,
            "device": {"serial": "G000TESTSERIAL"},
            "results": [
                {
                    "op": "list",
                    "ok": False,
                    "code": "list_partial",
                    "partial": True,
                    "files": [],
                    "error": "the listing failed part-way through",
                }
            ],
        }

    root = tmp_path / "out"
    with pytest.raises(backup.BackupFailed):
        backup.snapshot(mtp_backend(kindle.mount, failing), root=root, serial="S")
    assert backup.latest(root, "S") is None


def test_an_interrupted_backup_leaves_no_directory_without_partial(mass, tmp_path, monkeypatch):
    root = tmp_path / "out"
    first = backup.snapshot(mass, root=root, serial="S")
    mass.mount.joinpath(BOOK).write_bytes(mobi_bytes(BOOK_ID, filler=b"rewritten and longer"))

    def interrupt(items):
        raise KeyboardInterrupt

    monkeypatch.setattr(mass, "read_many", interrupt)
    with pytest.raises(KeyboardInterrupt):
        backup.snapshot(mass, root=root, serial="S")

    backups = backup.backup_root(root, "S") / "backups"
    complete = [p.name for p in backups.iterdir() if p.is_dir() and not p.name.endswith(".partial")]
    assert complete == [first.path.name]
    assert backup.latest(root, "S") == first.path
    # A Ctrl+C leaves its half-built directory behind on purpose — the `.partial` name
    # is what makes that safe, and deleting the user's data during an interrupt is not
    # this tool's job.
    assert [p.name for p in backups.iterdir() if p.name.endswith(".partial")]


def test_latest_moves_only_after_the_snapshot_directory_is_renamed(mass, tmp_path, monkeypatch):
    root = tmp_path / "out"
    first = backup.snapshot(mass, root=root, serial="S")
    mass.mount.joinpath(BOOK).write_bytes(mobi_bytes(BOOK_ID, filler=b"rewritten and longer"))

    def refuse(partial, final):
        raise OSError(39, "Directory not empty")

    monkeypatch.setattr(backup, "_promote", refuse)
    with pytest.raises(backup.BackupFailed):
        backup.snapshot(mass, root=root, serial="S")

    assert backup.latest(root, "S") == first.path
    backups = backup.backup_root(root, "S") / "backups"
    assert [p.name for p in backups.iterdir() if p.is_dir()] == [first.path.name]


def test_a_transfer_failure_moves_neither_latest_nor_a_finished_directory(
    mass, tmp_path, monkeypatch
):
    root = tmp_path / "out"
    first = backup.snapshot(mass, root=root, serial="S")
    mass.mount.joinpath(BOOK).write_bytes(mobi_bytes(BOOK_ID, filler=b"rewritten and longer"))

    def explode(items):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(mass, "read_many", explode)
    with pytest.raises(backup.BackupFailed):
        backup.snapshot(mass, root=root, serial="S")

    backups = backup.backup_root(root, "S") / "backups"
    assert [p.name for p in backups.iterdir() if p.is_dir()] == [first.path.name]
    assert backup.latest(root, "S") == first.path


def test_snapshots_are_never_pruned(mass, tmp_path):
    root = tmp_path / "out"
    made = [backup.snapshot(mass, root=root, serial="S") for _ in range(3)]
    backups = backup.backup_root(root, "S") / "backups"
    assert {p.name for p in backups.iterdir() if p.is_dir()} == {s.path.name for s in made}


def test_an_empty_listing_from_a_live_device_is_a_successful_zero_file_snapshot(tmp_path):
    """A factory-reset Kindle really does list zero files. Aborting there would refuse
    to back up a device that is simply empty."""
    root = tmp_path / "out"
    snap = backup.snapshot(EmptyDevice(), root=root, serial="S")

    assert snap.files == 0
    assert snap.bytes_copied == 0
    assert manifest_of(snap.path)["files"] == []
    assert backup.latest(root, "S") == snap.path


def test_an_empty_listing_from_a_device_that_cannot_answer_aborts_the_backup(tmp_path):
    root = tmp_path / "out"
    with pytest.raises(backup.BackupFailed) as error:
        backup.snapshot(EmptyDevice(answers=False), root=root, serial="S")
    assert "free space" in str(error.value)
    assert backup.latest(root, "S") is None


def test_a_vanished_mount_aborts_the_backup_instead_of_listing_nothing(mass, tmp_path):
    """Closed at the source: the mass-storage backend raises for a mount that is not a
    directory, rather than answering `[]` and letting an empty snapshot pass."""
    root = tmp_path / "out"
    shutil.rmtree(mass.mount)

    with pytest.raises(backup.BackupFailed):
        backup.snapshot(mass, root=root, serial="S")
    assert backup.latest(root, "S") is None


def test_an_unwritable_output_root_is_a_backup_failure_not_a_raw_oserror(mass, tmp_path):
    """Task 5's CLI catches `BackupFailed` to emit `backup_failed`; a raw OSError from
    the staging mkdir would reach it as an unhandled exception instead."""
    root = tmp_path / "out"
    root.write_text("a file where a directory should be", encoding="utf-8")

    with pytest.raises(backup.BackupFailed):
        backup.snapshot(mass, root=root, serial="S")


# --- device keys ----------------------------------------------------------------


def test_two_serial_less_devices_never_share_a_backup_root(tmp_path):
    first = Device(serial=None, product_id=4, mode="mass_storage", mount=tmp_path / "Kindle")
    second = Device(serial=None, product_id=4, mode="mass_storage", mount=tmp_path / "Kindle 2")

    assert backup.device_key(first).startswith("unknown-")
    assert backup.device_key(first) != backup.device_key(second)
    assert backup.device_key(first) == backup.device_key(first)
    assert backup.device_key(Device("G123", 4, "mass_storage", tmp_path / "K")) == "G123"


def test_a_serial_less_mtp_device_falls_back_to_its_model_hint(tmp_path):
    device = Device(serial=None, product_id=0x9981, mode="mtp", mount=None, model_hint="Scribe")
    assert backup.device_key(device).startswith("unknown-")


# --- restore --------------------------------------------------------------------


def test_restore_puts_a_book_back_with_its_sdr_and_its_thumbnail(mass, tmp_path):
    snap = backup.snapshot(mass, root=tmp_path / "out", serial="S")
    for path in (BOOK, SDR, THUMB):
        (mass.mount / path).unlink()

    report = backup.restore(mass, snap.path, only=[BOOK], dry_run=False)

    assert set(report.paths) == {BOOK, SDR, THUMB}
    assert report.files == 3
    assert (mass.mount / BOOK).read_bytes() == mobi_bytes(BOOK_ID)
    assert (mass.mount / SDR).read_bytes() == b"reading position"
    assert (mass.mount / THUMB).read_bytes() == b"thumbnail bytes"
    # Nothing else was touched.
    assert (mass.mount / "documents/pt/Um Livro - Um Autor.azw3").exists()


def test_restore_survives_a_stored_book_whose_records_cannot_be_read(mass, tmp_path, monkeypatch):
    """The one EXTH read in this module, and the worst place for an unreadable file to
    abort: the user is RECOVERING, so the snapshot is what they have left. The book
    and its `.sdr` still go back; only the thumbnail cannot be paired, which is the
    same outcome as a book that carries no EXTH 113 id at all."""
    snap = backup.snapshot(mass, root=tmp_path / "out", serial="S")
    for path in (BOOK, SDR, THUMB):
        (mass.mount / path).unlink()

    calls: list[str] = []

    def exploding_read_records(path):
        calls.append(str(path))
        raise MemoryError("cannot allocate")

    monkeypatch.setattr(exth, "read_records", exploding_read_records)

    report = backup.restore(mass, snap.path, only=[BOOK], dry_run=False)

    assert calls, "the exploding read was never called"
    assert set(report.paths) == {BOOK, SDR}
    assert report.no_thumbnail == [BOOK]
    assert (mass.mount / BOOK).read_bytes() == mobi_bytes(BOOK_ID)
    assert (mass.mount / SDR).read_bytes() == b"reading position"


def test_restore_without_only_puts_the_whole_snapshot_back(mass, tmp_path):
    snap = backup.snapshot(mass, root=tmp_path / "out", serial="S")
    for path in manifest_paths(snap.path):
        (mass.mount / path).unlink()

    report = backup.restore(mass, snap.path, dry_run=False)
    assert report.files == snap.files
    assert all((mass.mount / path).is_file() for path in manifest_paths(snap.path))


def test_restore_dry_run_writes_nothing(mass, tmp_path):
    snap = backup.snapshot(mass, root=tmp_path / "out", serial="S")
    for path in (BOOK, SDR, THUMB):
        (mass.mount / path).unlink()

    report = backup.restore(mass, snap.path, only=[BOOK], dry_run=True)

    assert report.dry_run is True
    assert set(report.paths) == {BOOK, SDR, THUMB}
    assert not (mass.mount / BOOK).exists()
    assert not (mass.mount / SDR).exists()
    assert not (mass.mount / THUMB).exists()


def test_restore_refuses_a_snapshot_it_cannot_read(mass, tmp_path):
    with pytest.raises(backup.BackupFailed):
        backup.restore(mass, tmp_path / "nowhere", dry_run=False)


def test_restore_refuses_a_file_whose_hash_no_longer_matches(mass, tmp_path):
    """Recovery is where a corrupt snapshot does the most damage: it is the one path
    that writes backup bytes over a book the user still has."""
    snap = backup.snapshot(mass, root=tmp_path / "out", serial="S")
    (snap.path / "files" / BOOK).write_bytes(b"rotted bytes, not this book")
    (mass.mount / BOOK).write_bytes(b"whatever is on the device now")

    report = backup.restore(mass, snap.path, only=[BOOK], dry_run=False)

    assert report.corrupt == [BOOK]
    assert BOOK not in report.paths
    assert (mass.mount / BOOK).read_bytes() == b"whatever is on the device now"
    # The sound `.sdr` sidecar still went back. The thumbnail did not — rotted bytes
    # are not a readable book either, so no EXTH id could be read to pair one against,
    # and that is reported too rather than passing in silence.
    assert set(report.paths) == {SDR}
    assert report.no_thumbnail == [BOOK]


def test_restore_dry_run_reports_corruption_before_anything_is_written(mass, tmp_path):
    snap = backup.snapshot(mass, root=tmp_path / "out", serial="S")
    (snap.path / "files" / THUMB).write_bytes(b"not a thumbnail")

    report = backup.restore(mass, snap.path, only=[BOOK], dry_run=True)
    assert report.corrupt == [THUMB]


def test_restore_records_a_book_it_cannot_pair_with_a_thumbnail(mass, tmp_path):
    """A book with no EXTH 113 goes back without a thumbnail — that has to be said, not
    passed over in silence."""
    plain = "documents/pt/Um Livro - Um Autor.azw3"
    snap = backup.snapshot(mass, root=tmp_path / "out", serial="S")

    report = backup.restore(mass, snap.path, only=[plain], dry_run=True)
    assert report.no_thumbnail == [plain]

    paired = backup.restore(mass, snap.path, only=[BOOK], dry_run=True)
    assert paired.no_thumbnail == []


def test_restore_pairs_a_thumbnail_by_its_exact_name_not_an_id_substring(mass, tmp_path):
    """The id-inside-the-name substring match this replaced could pair the WRONG
    book's thumbnail (an id that is itself a substring of another book's id), or the
    WRONG content-type's thumbnail for the SAME book, as long as the id happened to
    appear somewhere in the name. `thumbnails.thumbnail_name`'s exact name rules both
    out — only the one filename the device's own firmware would look for is paired.
    """
    other_id = f"{BOOK_ID}X"  # a superstring: BOOK_ID is a substring of this one
    other_thumb = f"system/thumbnails/thumbnail_{other_id}_EBOK_portrait.jpg"
    wrong_type_thumb = f"system/thumbnails/thumbnail_{BOOK_ID}_PDOC_portrait.jpg"
    (mass.mount / other_thumb).write_bytes(b"a different book's thumbnail")
    (mass.mount / wrong_type_thumb).write_bytes(b"the same book, wrong content type")

    snap = backup.snapshot(mass, root=tmp_path / "out", serial="S")
    report = backup.restore(mass, snap.path, only=[BOOK], dry_run=True)

    assert THUMB in report.paths
    assert other_thumb not in report.paths
    assert wrong_type_thumb not in report.paths


# --- journal --------------------------------------------------------------------


def test_the_journal_round_trips_an_operation_id(tmp_path):
    root = tmp_path / "out"
    first = backup.journal_append(root, "S", {"op": "remove", "paths": [BOOK]})
    second = backup.journal_append(root, "S", {"op": "add", "paths": ["documents/en/New.azw3"]})

    assert first != second
    entries = backup.journal_read(root, "S")
    assert [entry["id"] for entry in entries] == [first, second]
    assert entries[0]["op"] == "remove"
    assert entries[0]["paths"] == [BOOK]
    assert all(entry["at"].endswith("Z") for entry in entries)


def test_an_empty_journal_reads_back_as_an_empty_list(tmp_path):
    assert backup.journal_read(tmp_path / "out", "S") == []


def test_a_garbled_journal_line_never_hides_the_good_ones(tmp_path):
    root = tmp_path / "out"
    kept = backup.journal_append(root, "S", {"op": "remove"})
    path = backup.backup_root(root, "S") / "journal.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")
    also_kept = backup.journal_append(root, "S", {"op": "add"})

    assert [entry["id"] for entry in backup.journal_read(root, "S")] == [kept, also_kept]


# --- read_many parity -----------------------------------------------------------


def test_read_many_transfers_the_same_bytes_on_both_backends(kindle, tmp_path):
    items = [BOOK, SDR, THUMB, "My Clippings.txt"]

    mass_dir = tmp_path / "mass"
    massstorage.MassStorageBackend(kindle.mount).read_many(
        [(path, mass_dir / path) for path in items]
    )

    mtp_dir = tmp_path / "mtp"
    runner = TreeRunner(kindle.mount)
    mtp_backend(kindle.mount, runner).read_many([(path, mtp_dir / path) for path in items])

    for path in items:
        expected = (kindle.mount / path).read_bytes()
        assert (mass_dir / path).read_bytes() == expected
        assert (mtp_dir / path).read_bytes() == expected

    # Both create the destination's parents; neither needed one made for it.
    assert (mass_dir / SDR).parent.is_dir()
    assert (mtp_dir / SDR).parent.is_dir()
    # And the whole batch went over the wire in ONE invocation.
    assert len(runner.calls) == 1
    assert [op["op"] for op in runner.calls[0]] == ["get"] * len(items)


def test_read_many_leaves_no_file_behind_when_a_fetch_dies_part_way(kindle, tmp_path, monkeypatch):
    """The divergence the parity tests could not see: `FileNotFoundError` is raised
    before `copyfile` creates anything, so only a MID-COPY failure shows whether a
    truncated local file is left at the final name. MTP's helper removes it; mass
    storage stages through `temp_path` so it does too."""
    half = tmp_path / "mass" / "out.azw3"

    def die_part_way(source, target):
        Path(target).write_bytes(b"half a bo")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(massstorage.shutil, "copyfile", die_part_way)
    with pytest.raises(OSError):
        massstorage.MassStorageBackend(kindle.mount).read_many([(BOOK, half)])
    monkeypatch.undo()

    assert not half.exists()
    assert list(half.parent.glob("*.partial")) == []

    mtp_half = tmp_path / "mtp" / "out.azw3"
    runner = TreeRunner(kindle.mount, fail_paths={BOOK})
    with pytest.raises(CalibreError):
        mtp_backend(kindle.mount, runner).read_many([(BOOK, mtp_half)])
    assert not mtp_half.exists()


def test_read_many_of_an_absent_path_raises_file_not_found_on_both_backends(kindle, tmp_path):
    missing = [("documents/en/Not There.azw3", tmp_path / "x.azw3")]

    with pytest.raises(FileNotFoundError):
        massstorage.MassStorageBackend(kindle.mount).read_many(missing)
    with pytest.raises(FileNotFoundError):
        mtp_backend(kindle.mount).read_many(missing)


def test_read_many_of_nothing_touches_the_device_on_neither_backend(kindle):
    runner = TreeRunner(kindle.mount)
    massstorage.MassStorageBackend(kindle.mount).read_many([])
    mtp_backend(kindle.mount, runner).read_many([])
    assert runner.calls == []


def test_a_backup_over_mtp_produces_the_same_manifest_as_over_mass_storage(kindle, tmp_path):
    over_mass = backup.snapshot(
        massstorage.MassStorageBackend(kindle.mount), root=tmp_path / "a", serial="S"
    )
    runner = TreeRunner(kindle.mount)
    over_mtp = backup.snapshot(mtp_backend(kindle.mount, runner), root=tmp_path / "b", serial="S")

    assert manifest_paths(over_mass.path) == manifest_paths(over_mtp.path)
    assert over_mass.bytes_copied == over_mtp.bytes_copied
    hashes = {e["path"]: e["sha256"] for e in manifest_of(over_mass.path)["files"]}
    assert {e["path"]: e["sha256"] for e in manifest_of(over_mtp.path)["files"]} == hashes
