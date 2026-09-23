"""Backups: the safety feature every writing command depends on.

Nothing here touches a real device. The mass-storage backend is driven against the
`fake_kindle` fixture, and the MTP backend against a fake runner that serves the same
tree — which is what makes the `read_many` parity tests possible at all.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from media_tools.core.events import ERROR_CODES, WARNING_CODES
from media_tools.integrations.calibre import CalibreError
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

    def __init__(self, mount: Path) -> None:
        self.mount = Path(mount)
        self.calls: list[list[dict]] = []

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
    assert second.bytes_linked == sum(
        entry["size"] for entry in manifest_of(second.path)["files"] if entry["linked"]
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
    assert second.bytes_copied == 0
    assert second.bytes_linked > 0
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
