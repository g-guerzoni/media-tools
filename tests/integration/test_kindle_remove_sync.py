"""`ebook kindle remove|sync|restore`: the three commands that can delete a book.

Driven against the shared `fake_kindle` fixture (`tests/conftest.py`) through the
injected `device_finder`/`backend_factory` seam `kindle.cli.resolve_device` exists
for — never real hardware, never the network. The whole point of this file is the
destructive half of the contract: nothing is deleted without `--yes`, a removal takes
the book's `.sdr` sidecar and its thumbnail with it, the never-removed areas really
are never removed, and a journalled removal can be undone.
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
from media_tools.tasks.ebook.kindle import backup as backup_module
from media_tools.tasks.ebook.kindle import cli as kindle_cli
from media_tools.tasks.ebook.kindle import massstorage, mtp
from media_tools.tasks.ebook.kindle.detect import Device

EN_ID = "ENGLISHBOOK00001"
PT_ID = "PORTUGUESEBOOK01"
KFX_ID = "PURCHASEDKFX0001"
NEW_ID = "BRANDNEWBOOK0001"

EN_PATH = "documents/en/A Book - An Author.azw3"
EN_SDR = "documents/en/A Book - An Author.sdr"
PT_PATH = "documents/pt/Um Livro - Um Autor.azw3"
KFX_PATH = "documents/A Purchased Book.kfx"
KFX_SDR = "documents/A Purchased Book.sdr"

# Deliberately unrelated to the filenames: a regression to filename-derived metadata
# (or to filename-based identity) could not produce these values.
EN_TITLE = "Not The Filename At All"
EN_AUTHOR = "Someone Else Entirely"


# --- fixtures built here, not checked in ---------------------------------------------


def mobi_bytes(
    *,
    book_id: str | None = None,
    title: str | None = None,
    author: str | None = None,
    language: str | None = None,
    cdetype: str = "EBOK",
) -> bytes:
    """A byte blob `tasks.ebook.exth.read_records` parses, carrying whichever of EXTH
    113 (book id)/501 (CDE type)/503 (title)/100 (author)/524 (language) is given.
    Mirrors `tests/unit/test_kindle_cli.py`'s own helper — the base `fake_kindle`
    fixture's planted books carry no records at all, and every command here reads a
    book's identity from its records rather than from its name."""
    entries: list[tuple[int, bytes]] = [(501, cdetype.encode())]
    if book_id is not None:
        entries.append((113, book_id.encode()))
    if title is not None:
        entries.append((503, title.encode("utf-8")))
    if author is not None:
        entries.append((100, author.encode("utf-8")))
    if language is not None:
        entries.append((524, language.encode("utf-8")))
    blob = b"".join(struct.pack(">II", tag, 8 + len(v)) + v for tag, v in entries)
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
    return bytes(palm) + record0 + b"text record"


def prepare_device(fake_kindle, *, purchased: bool = False, mode: str = "mass_storage") -> Device:
    """The shared fixture's two books replaced by real MOBI bytes, plus the `.sdr`
    sidecar and the thumbnail a removal has to take with the book. With `purchased`,
    a `*.kfx` and its `.sdr/assets` — the shape of a book bought from Amazon, which
    this tool never deletes."""
    mount = fake_kindle.mount
    (mount / EN_PATH).write_bytes(
        mobi_bytes(book_id=EN_ID, title=EN_TITLE, author=EN_AUTHOR, language="en")
    )
    sdr = mount / EN_SDR
    sdr.mkdir(exist_ok=True)
    (sdr / "position.mbp").write_bytes(b"reading position")
    (mount / "system" / "thumbnails" / f"thumbnail_{EN_ID}_EBOK_portrait.jpg").write_bytes(b"thumb")
    (mount / PT_PATH).write_bytes(
        mobi_bytes(book_id=PT_ID, title="Um Livro", author="Um Autor", language="pt")
    )
    if purchased:
        # Given a readable EXTH 113 id on purpose: a real KFX container is one this
        # project's parser cannot read at all, and an id-less device book is excluded
        # from `sync`'s extras one step BEFORE the protection rule is ever consulted —
        # which would leave that rule untested here for the wrong reason.
        (mount / KFX_PATH).write_bytes(mobi_bytes(book_id=KFX_ID, title="A Purchased Book"))
        assets = mount / KFX_SDR / "assets"
        assets.mkdir(parents=True)
        (assets / "resource.res").write_bytes(b"drm asset")
    return Device(
        serial=fake_kindle.serial,
        product_id=fake_kindle.product_id,
        mode=mode,
        mount=mount,
    )


def plant_library_batch(root: Path, name: str, books: list[tuple[str, Path | str, str]]) -> None:
    """A minimal `ebook build` batch `run.json`: `(book id, output path, language)`
    per surviving item, which is all `--batch`/`--compare` ever read back out of one."""
    batch_dir = root / name
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "run.json").write_text(
        json.dumps(
            {
                "v": 1,
                "items": [
                    {
                        "id": index,
                        "status": "done",
                        "data": {"book_id": book_id, "language": language, "output": str(output)},
                    }
                    for index, (book_id, output, language) in enumerate(books, start=1)
                ],
            }
        ),
        encoding="utf-8",
    )


def _mass_storage_factory(device, *, cache_dir):
    return massstorage.MassStorageBackend(device.mount)


def _events(capsys) -> list[dict]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _remove_args(root: Path, *extra: str):
    return build_parser().parse_args(
        ["ebook", "kindle", "remove", *extra, "--json", "-o", str(root)]
    )


def _sync_args(root: Path, *extra: str):
    return build_parser().parse_args(["ebook", "kindle", "sync", *extra, "--json", "-o", str(root)])


def _restore_args(root: Path, *extra: str):
    return build_parser().parse_args(
        ["ebook", "kindle", "restore", *extra, "--json", "-o", str(root)]
    )


# --- backends that misbehave in exactly one way ---------------------------------------


class _BrokenListingBackend:
    """Every listing fails, so the mandatory pre-write backup fails — while `remove`
    would have worked fine. Proves the backup really is a precondition."""

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


class _MtpLikeBackend:
    """Mass storage underneath, but with MTP's one real gap: Calibre 9.15 has no
    delete-by-name, and its cached device tree omits `*.sdr` folders and everything
    under `system/`, so a `rm` of either raises `MtpPathNotInCachedTree`. Everything
    else — listing, reading, writing, deleting an ordinary book — behaves normally."""

    def __init__(self, mount: Path) -> None:
        self._inner = massstorage.MassStorageBackend(mount)

    def list_files(self, prefix: str = ""):
        return self._inner.list_files(prefix)

    def read(self, path: str, dest: Path) -> None:
        self._inner.read(path, dest)

    def read_many(self, items) -> None:
        self._inner.read_many(items)

    def write(self, local: Path, path: str) -> None:
        self._inner.write(local, path)

    def remove(self, path: str) -> None:
        if ".sdr/" in path or path.startswith("system/"):
            raise mtp.MtpPathNotInCachedTree(
                f"{path!r} is not in the cached device tree (simulated MTP)"
            )
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


def test_remove_sync_and_restore_are_registered_with_their_flags():
    remove = build_parser().parse_args(
        ["ebook", "kindle", "remove", "documents/en/Book.azw3", "--match", "x", "--asin", "A1"]
    )
    assert remove.kindle_command == "remove"
    assert remove.books == ["documents/en/Book.azw3"]
    assert remove.match == "x"
    assert remove.asin == "A1"
    assert remove.yes is False

    sync = build_parser().parse_args(
        ["ebook", "kindle", "sync", "--batch", "library", "--delete-extras", "--yes"]
    )
    assert sync.kindle_command == "sync"
    assert sync.batch == "library"
    assert sync.delete_extras is True
    assert sync.yes is True

    restore = build_parser().parse_args(["ebook", "kindle", "restore", "2026-01-01T000000Z"])
    assert restore.kindle_command == "restore"
    assert restore.snapshot == "2026-01-01T000000Z"
    assert restore.op is None
    # Restore writes over files the user still has, so it is gated exactly like
    # `remove`: without --yes it plans.
    assert restore.yes is False
    assert restore.force is False


def test_remove_with_no_selector_at_all_is_a_usage_error(tmp_path, fake_kindle):
    """A bare `remove --yes` must never mean "remove everything": with nothing named,
    nothing matched and no id given, the command refuses before it even looks at the
    device."""
    args = _remove_args(tmp_path / "media", "--yes")
    with pytest.raises(UsageError) as caught:
        kindle_cli.run_remove(
            args, device_finder=lambda: fake_kindle, backend_factory=_mass_storage_factory
        )
    assert caught.value.exit_code == EXIT_USAGE


def test_the_cli_turns_a_remove_with_no_selector_into_exit_2_with_an_error_and_a_result(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "media_tools",
            "ebook",
            "kindle",
            "remove",
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


def test_sync_without_a_batch_is_a_usage_error(tmp_path, fake_kindle):
    args = _sync_args(tmp_path / "media")
    with pytest.raises(UsageError):
        kindle_cli.run_sync(
            args, device_finder=lambda: fake_kindle, backend_factory=_mass_storage_factory
        )


# --- remove plans only, unless --yes -----------------------------------------------------


def test_remove_without_yes_lists_the_matches_writes_nothing_and_exits_zero(
    fake_kindle, tmp_path, capsys
):
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    mount = device.mount

    args = _remove_args(root, "--match", EN_AUTHOR)
    exit_code = kindle_cli.run_remove(
        args, device_finder=lambda: device, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_OK

    events = _events(capsys)
    start = events[0]
    assert start["type"] == "start"
    # No `--yes`, so no backup stage: this run cannot write anything.
    assert start["stages"] == ["detect", "plan"]
    assert [e["stage"] for e in events if e["type"] == "stage"] == ["detect", "plan"]

    items = [e for e in events if e["type"] == "item"]
    assert len(items) == 1
    assert items[0]["input"] == EN_PATH
    assert items[0]["status"] == "pending"

    result = events[-1]
    assert result["type"] == "result"
    assert result["ok"] is True
    assert result["counts"] == {"total": 1, "done": 0, "skipped": 0, "failed": 0, "pending": 1}
    assert result["pending"] == [EN_PATH]
    # The plan names the sidecar and the thumbnail that WOULD go with the book.
    planned = result["data"]["books"][0]
    assert planned["device_path"] == EN_PATH
    assert f"{EN_SDR}/position.mbp" in planned["would_remove"]
    assert f"system/thumbnails/thumbnail_{EN_ID}_EBOK_portrait.jpg" in planned["would_remove"]
    assert result["data"]["snapshot"] is None
    assert result["data"]["operation"] is None

    # NOTHING happened: no book gone, no backup taken, no journal entry.
    assert (mount / EN_PATH).is_file()
    assert (mount / EN_SDR / "position.mbp").is_file()
    assert (mount / "system" / "thumbnails" / f"thumbnail_{EN_ID}_EBOK_portrait.jpg").is_file()
    assert not (root / "_kindle" / device.serial / "backups").exists()
    assert backup_module.journal_read(root, device.serial) == []


def test_remove_with_yes_takes_the_book_its_sdr_and_its_thumbnail_after_a_backup(
    fake_kindle, tmp_path, capsys
):
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    mount = device.mount

    args = _remove_args(root, "--match", EN_AUTHOR, "--yes")
    exit_code = kindle_cli.run_remove(
        args, device_finder=lambda: device, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_OK

    thumb = mount / "system" / "thumbnails" / f"thumbnail_{EN_ID}_EBOK_portrait.jpg"
    assert not (mount / EN_PATH).exists()
    assert not (mount / EN_SDR / "position.mbp").exists()
    # ...and the emptied `.sdr` folder itself, not just the files inside it.
    assert not (mount / EN_SDR).exists()
    assert not thumb.exists()
    # The other book is untouched — only what matched was removed.
    assert (mount / PT_PATH).is_file()
    assert (mount / "system" / "thumbnails" / "cover.jpg").is_file()

    events = _events(capsys)
    assert events[0]["stages"] == ["detect", "backup", "plan", "remove"]
    assert [e["stage"] for e in events if e["type"] == "stage"] == [
        "detect",
        "backup",
        "plan",
        "remove",
    ]

    item = next(e for e in events if e["type"] == "item")
    assert item["status"] == "done"
    assert item["input"] == EN_PATH

    result = events[-1]
    assert result["counts"] == {"total": 1, "done": 1, "skipped": 0, "failed": 0, "pending": 0}
    removed = set(result["data"]["removed"])
    assert removed == {
        EN_PATH,
        f"{EN_SDR}/position.mbp",
        f"system/thumbnails/thumbnail_{EN_ID}_EBOK_portrait.jpg",
    }

    # The mandatory pre-write backup really ran, and holds the book it then deleted.
    snapshot_dir = backup_module.latest(root, device.serial)
    assert snapshot_dir is not None
    manifest = json.loads((snapshot_dir / backup_module.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert EN_PATH in {entry["path"] for entry in manifest["files"]}
    assert result["data"]["snapshot"]["snapshot"] == snapshot_dir.name

    # ...and the operation is journalled, so it can be undone.
    entries = backup_module.journal_read(root, device.serial)
    assert len(entries) == 1
    assert entries[0]["op"] == "remove"
    assert entries[0]["id"] == result["data"]["operation"]
    assert set(entries[0]["paths"]) == removed


def test_remove_matches_a_book_by_its_own_title_not_only_its_path(fake_kindle, tmp_path, capsys):
    """Ruling R36's rule, inherited from `thumbnails`: the device path OR the EXTH
    title OR the author, case-insensitively. The device filename contains none of
    this book's real title."""
    device = prepare_device(fake_kindle)
    args = _remove_args(tmp_path / "media", "--match", EN_TITLE.lower())
    assert (
        kindle_cli.run_remove(
            args, device_finder=lambda: device, backend_factory=_mass_storage_factory
        )
        == EXIT_OK
    )
    items = [e for e in _events(capsys) if e["type"] == "item"]
    assert [item["input"] for item in items] == [EN_PATH]


def test_remove_by_asin_selects_only_that_book_id(fake_kindle, tmp_path, capsys):
    device = prepare_device(fake_kindle)
    args = _remove_args(tmp_path / "media", "--asin", PT_ID.lower())
    assert (
        kindle_cli.run_remove(
            args, device_finder=lambda: device, backend_factory=_mass_storage_factory
        )
        == EXIT_OK
    )
    items = [e for e in _events(capsys) if e["type"] == "item"]
    assert [item["input"] for item in items] == [PT_PATH]


def test_a_named_book_that_is_not_on_the_device_is_reported_not_invented(
    fake_kindle, tmp_path, capsys
):
    device = prepare_device(fake_kindle)
    args = _remove_args(tmp_path / "media", "documents/en/Nothing Like It.azw3", "--yes")
    exit_code = kindle_cli.run_remove(
        args, device_finder=lambda: device, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_FAILED
    item = next(e for e in _events(capsys) if e["type"] == "item")
    assert item["status"] == "failed"
    assert item["reason"] == "source_missing"


# --- what is never removed ---------------------------------------------------------------


def test_a_match_that_would_hit_a_purchased_kfx_is_refused(fake_kindle, tmp_path, capsys):
    device = prepare_device(fake_kindle, purchased=True)
    root = tmp_path / "media"
    mount = device.mount

    args = _remove_args(root, "--match", "purchased", "--yes")
    exit_code = kindle_cli.run_remove(
        args, device_finder=lambda: device, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_FAILED

    events = _events(capsys)
    item = next(e for e in events if e["type"] == "item")
    assert item["input"] == KFX_PATH
    assert item["status"] == "failed"
    assert item["detail"].startswith(f"{kindle_cli.DETAIL_PROTECTED}: ")

    # The book, and the DRM assets beside it, are still there.
    assert (mount / KFX_PATH).is_file()
    assert (mount / KFX_SDR / "assets" / "resource.res").is_file()
    result = events[-1]
    assert result["data"]["removed"] == []


def test_a_sidecar_two_books_share_is_kept_for_the_one_that_stays(fake_kindle, tmp_path, capsys):
    """The device pairs a book with its `.sdr` by stem, so `Book.azw3` and
    `Book.mobi` in one folder read the SAME reading position. Removing one must not
    take the other's."""
    device = prepare_device(fake_kindle)
    mount = device.mount
    sibling = mount / "documents" / "en" / "A Book - An Author.mobi"
    sibling.write_bytes(mobi_bytes(book_id="SIBLINGBOOK00001", title="A Sibling", language="en"))

    args = _remove_args(tmp_path / "media", EN_PATH, "--yes")
    assert (
        kindle_cli.run_remove(
            args, device_finder=lambda: device, backend_factory=_mass_storage_factory
        )
        == EXIT_OK
    )

    assert not (mount / EN_PATH).exists()
    assert sibling.is_file()
    # The shared reading position stayed with the book that is still there...
    assert (mount / EN_SDR / "position.mbp").is_file()
    events = _events(capsys)
    item = next(e for e in events if e["type"] == "item")
    # ...and that is not a failure, so no warning: it was never this removal's to take.
    assert item["status"] == "done"
    assert item["warnings"] == []
    book = events[-1]["data"]["books"][0]
    assert book["kept"] == [f"{EN_SDR}/position.mbp"]
    assert book["shared_with"] == ["documents/en/A Book - An Author.mobi"]


def test_over_mtp_every_kfx_is_refused_because_its_assets_cannot_be_seen(
    fake_kindle, tmp_path, capsys
):
    """The marker that tells a purchased KFX from a sideloaded one lives in its
    `.sdr`, and MTP's cached tree has no `.sdr` folders in it at all — so "no marker"
    there means "could not look", never "sideloaded"."""
    device = prepare_device(fake_kindle, purchased=True, mode="mtp")
    args = _remove_args(tmp_path / "media", KFX_PATH, "--yes")
    exit_code = kindle_cli.run_remove(
        args,
        device_finder=lambda: device,
        backend_factory=lambda d, *, cache_dir: _MtpLikeBackend(d.mount),
    )
    assert exit_code == EXIT_FAILED
    item = next(e for e in _events(capsys) if e["type"] == "item")
    assert item["detail"].startswith(f"{kindle_cli.DETAIL_PROTECTED}: ")
    assert (device.mount / KFX_PATH).is_file()


def test_a_named_path_under_system_or_audible_is_refused_before_anything_is_deleted(
    fake_kindle, tmp_path, capsys
):
    device = prepare_device(fake_kindle)
    mount = device.mount
    (mount / "audible" / "Audiobook.aax").write_bytes(b"audiobook")

    args = _remove_args(
        tmp_path / "media", "system/wifi/wifi.cfg", "audible/Audiobook.aax", "--yes"
    )
    exit_code = kindle_cli.run_remove(
        args, device_finder=lambda: device, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_FAILED

    items = [e for e in _events(capsys) if e["type"] == "item"]
    assert {item["input"] for item in items} == {"system/wifi/wifi.cfg", "audible/Audiobook.aax"}
    assert all(item["status"] == "failed" for item in items)
    assert all(item["detail"].startswith(f"{kindle_cli.DETAIL_PROTECTED}: ") for item in items)

    assert (mount / "system" / "wifi" / "wifi.cfg").is_file()
    assert (mount / "audible" / "Audiobook.aax").is_file()


# --- MTP cannot delete a sidecar ----------------------------------------------------------


def test_over_mtp_a_sidecar_that_cannot_be_deleted_warns_and_the_book_is_still_removed(
    fake_kindle, tmp_path, capsys
):
    device = prepare_device(fake_kindle, mode="mtp")
    root = tmp_path / "media"
    mount = device.mount

    args = _remove_args(root, "--match", EN_AUTHOR, "--yes")
    exit_code = kindle_cli.run_remove(
        args,
        device_finder=lambda: device,
        backend_factory=lambda d, *, cache_dir: _MtpLikeBackend(d.mount),
    )
    # A sidecar the backend cannot reach is a WARNING, not a failure: the book is
    # gone either way, and reporting otherwise would be a lie the user finds later.
    assert exit_code == EXIT_OK

    events = _events(capsys)
    item = next(e for e in events if e["type"] == "item")
    assert item["status"] == "done"
    assert item["warnings"] == ["sidecar_not_removed"]

    assert not (mount / EN_PATH).exists()
    # ...and the truth is reported rather than papered over.
    assert (mount / EN_SDR / "position.mbp").is_file()
    result = events[-1]
    book = result["data"]["books"][0]
    assert book["removed"] == [EN_PATH]
    assert f"{EN_SDR}/position.mbp" in book["not_removed"]


# --- the backup is a precondition, not a step ----------------------------------------------


def test_a_failed_mandatory_backup_aborts_the_remove_and_deletes_nothing(
    fake_kindle, tmp_path, capsys
):
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    mount = device.mount

    args = _remove_args(root, "--match", EN_AUTHOR, "--yes")
    exit_code = kindle_cli.run_remove(
        args,
        device_finder=lambda: device,
        backend_factory=lambda d, *, cache_dir: _BrokenListingBackend(d.mount),
    )
    assert exit_code == EXIT_DEPENDENCY

    events = _events(capsys)
    results = [e for e in events if e["type"] == "result"]
    assert len(results) == 1
    assert results[0]["ok"] is False
    assert results[0]["counts"] == {"total": 0, "done": 0, "skipped": 0, "failed": 0, "pending": 0}
    assert any(e["code"] == "backup_failed" for e in events if e["type"] == "error")
    assert [e for e in events if e["type"] == "item"] == []

    assert (mount / EN_PATH).is_file()
    assert (mount / EN_SDR / "position.mbp").is_file()
    assert backup_module.journal_read(root, device.serial) == []


# --- sync ------------------------------------------------------------------------------------


def test_sync_adds_what_the_library_has_and_leaves_extras_alone(fake_kindle, tmp_path, capsys):
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    mount = device.mount

    new_book = tmp_path / "library" / "A Brand New Book.azw3"
    new_book.parent.mkdir(parents=True)
    new_book.write_bytes(
        mobi_bytes(book_id=NEW_ID, title="A Brand New Book", author="An Author", language="en")
    )
    # The device already holds EN_ID (under a name of its own); the library names it
    # too, so it is neither added again nor ever an extra.
    plant_library_batch(root, "library", [(NEW_ID, new_book, "en"), (EN_ID, new_book, "en")])

    args = _sync_args(root, "--batch", "library")
    exit_code = kindle_cli.run_sync(
        args, device_finder=lambda: device, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_OK

    # The missing book landed...
    assert (mount / "documents" / "en" / "A Brand New Book.azw3").read_bytes() == (
        new_book.read_bytes()
    )
    # ...and the device book the library does not have is untouched.
    assert (mount / PT_PATH).is_file()

    events = _events(capsys)
    result = events[-1]
    # The extra is REPORTED (so a user can see what `--delete-extras` would take)
    # without being planned for removal.
    assert [extra["device_path"] for extra in result["data"]["extras"]] == [PT_PATH]
    assert result["data"]["removed"] == []
    assert result["data"]["remove_operation"] is None
    assert all(e["status"] != "pending" for e in events if e["type"] == "item")


def test_sync_with_delete_extras_and_yes_removes_them_after_a_backup(fake_kindle, tmp_path, capsys):
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    mount = device.mount

    new_book = tmp_path / "library" / "A Brand New Book.azw3"
    new_book.parent.mkdir(parents=True)
    new_book.write_bytes(
        mobi_bytes(book_id=NEW_ID, title="A Brand New Book", author="An Author", language="en")
    )
    plant_library_batch(root, "library", [(NEW_ID, new_book, "en"), (EN_ID, new_book, "en")])

    args = _sync_args(root, "--batch", "library", "--delete-extras", "--yes")
    exit_code = kindle_cli.run_sync(
        args, device_finder=lambda: device, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_OK

    assert (mount / "documents" / "en" / "A Brand New Book.azw3").is_file()
    assert not (mount / PT_PATH).exists()
    # The library's own books are never "extras", however they are named on the device.
    assert (mount / EN_PATH).is_file()

    events = _events(capsys)
    assert "remove" in events[0]["stages"]
    result = events[-1]
    assert result["data"]["removed"] == [PT_PATH]

    snapshot_dir = backup_module.latest(root, device.serial)
    assert snapshot_dir is not None
    manifest = json.loads((snapshot_dir / backup_module.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert PT_PATH in {entry["path"] for entry in manifest["files"]}

    ops = {entry["op"]: entry for entry in backup_module.journal_read(root, device.serial)}
    assert set(ops) == {"add", "remove"}
    assert ops["remove"]["paths"] == [PT_PATH]


def test_sync_with_delete_extras_but_no_yes_plans_the_removal_and_deletes_nothing(
    fake_kindle, tmp_path, capsys
):
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    new_book = tmp_path / "library" / "A Brand New Book.azw3"
    new_book.parent.mkdir(parents=True)
    new_book.write_bytes(
        mobi_bytes(book_id=NEW_ID, title="A Brand New Book", author="An Author", language="en")
    )
    plant_library_batch(root, "library", [(NEW_ID, new_book, "en"), (EN_ID, new_book, "en")])

    args = _sync_args(root, "--batch", "library", "--delete-extras")
    exit_code = kindle_cli.run_sync(
        args, device_finder=lambda: device, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_OK

    assert (device.mount / PT_PATH).is_file()
    events = _events(capsys)
    pending = [e for e in events if e["type"] == "item" and e["status"] == "pending"]
    assert [e["input"] for e in pending] == [PT_PATH]
    assert events[-1]["data"]["removed"] == []


def test_sync_dry_run_takes_no_backup_and_touches_nothing(fake_kindle, tmp_path, capsys):
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    new_book = tmp_path / "library" / "A Brand New Book.azw3"
    new_book.parent.mkdir(parents=True)
    new_book.write_bytes(mobi_bytes(book_id=NEW_ID, title="A Brand New Book", language="en"))
    plant_library_batch(root, "library", [(NEW_ID, new_book, "en"), (EN_ID, new_book, "en")])

    args = _sync_args(root, "--batch", "library", "--delete-extras", "--yes", "--dry-run")
    exit_code = kindle_cli.run_sync(
        args, device_finder=lambda: device, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_OK

    assert not (device.mount / "documents" / "en" / "A Brand New Book.azw3").exists()
    assert (device.mount / PT_PATH).is_file()
    events = _events(capsys)
    assert events[0]["stages"] == ["detect", "plan"]
    assert {e["input"] for e in events if e["type"] == "item" and e["status"] == "pending"} == {
        str(new_book),
        PT_PATH,
    }
    assert events[-1]["data"]["snapshot"] is None
    assert not (root / "_kindle" / device.serial / "backups").exists()


def test_sync_never_removes_a_path_its_own_plan_targets(fake_kindle, tmp_path, capsys):
    """A device file sitting exactly where a library book would land is refused by
    the add half as an `output_collision` — so the remove half must leave it alone
    too, or the run would take the user's copy and put nothing in its place."""
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    target = device.mount / "documents" / "en" / "A Brand New Book.azw3"
    target.write_bytes(
        mobi_bytes(book_id="SOMEONEELSES0001", title="Someone Else's", language="en")
    )

    new_book = tmp_path / "library" / "A Brand New Book.azw3"
    new_book.parent.mkdir(parents=True)
    new_book.write_bytes(mobi_bytes(book_id=NEW_ID, title="A Brand New Book", language="en"))
    plant_library_batch(
        root,
        "library",
        [(NEW_ID, new_book, "en"), (EN_ID, new_book, "en"), (PT_ID, new_book, "pt")],
    )

    args = _sync_args(root, "--batch", "library", "--delete-extras", "--yes")
    exit_code = kindle_cli.run_sync(
        args, device_finder=lambda: device, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_FAILED  # the collision, reported as such

    assert target.is_file()
    result = _events(capsys)[-1]
    assert result["data"]["removed"] == []
    assert [extra["device_path"] for extra in result["data"]["extras"]] == []


def test_sync_never_treats_an_id_less_device_book_as_an_extra(fake_kindle, tmp_path, capsys):
    """A book whose EXTH 113 cannot be read cannot be proven absent from the library,
    so `--delete-extras` must not delete it."""
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    (device.mount / "documents" / "en" / "Anonymous.azw3").write_bytes(mobi_bytes(title="No Id"))

    new_book = tmp_path / "library" / "A Brand New Book.azw3"
    new_book.parent.mkdir(parents=True)
    new_book.write_bytes(mobi_bytes(book_id=NEW_ID, title="A Brand New Book", language="en"))
    plant_library_batch(
        root,
        "library",
        [(NEW_ID, new_book, "en"), (EN_ID, new_book, "en"), (PT_ID, new_book, "pt")],
    )

    args = _sync_args(root, "--batch", "library", "--delete-extras", "--yes")
    assert (
        kindle_cli.run_sync(
            args, device_finder=lambda: device, backend_factory=_mass_storage_factory
        )
        == EXIT_OK
    )
    assert (device.mount / "documents" / "en" / "Anonymous.azw3").is_file()
    assert _events(capsys)[-1]["data"]["removed"] == []


# --- restore -----------------------------------------------------------------------------


def test_restore_op_puts_back_exactly_what_that_operation_removed(fake_kindle, tmp_path, capsys):
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    mount = device.mount
    book_before = (mount / EN_PATH).read_bytes()
    sidecar_before = (mount / EN_SDR / "position.mbp").read_bytes()

    removed = kindle_cli.run_remove(
        _remove_args(root, "--match", EN_AUTHOR, "--yes"),
        device_finder=lambda: device,
        backend_factory=_mass_storage_factory,
    )
    assert removed == EXIT_OK
    operation_id = _events(capsys)[-1]["data"]["operation"]
    assert not (mount / EN_PATH).exists()

    # The other book is deleted by hand AFTER the operation, so a restore that puts
    # back more than that one operation touched would be caught here.
    (mount / PT_PATH).unlink()

    exit_code = kindle_cli.run_restore(
        _restore_args(root, "--op", operation_id, "--yes"),
        device_finder=lambda: device,
        backend_factory=_mass_storage_factory,
    )
    assert exit_code == EXIT_OK

    assert (mount / EN_PATH).read_bytes() == book_before
    assert (mount / EN_SDR / "position.mbp").read_bytes() == sidecar_before
    assert (mount / "system" / "thumbnails" / f"thumbnail_{EN_ID}_EBOK_portrait.jpg").is_file()
    # Exactly that operation, and nothing else.
    assert not (mount / PT_PATH).exists()

    events = _events(capsys)
    result = events[-1]
    assert set(result["outputs"]) == {
        EN_PATH,
        f"{EN_SDR}/position.mbp",
        f"system/thumbnails/thumbnail_{EN_ID}_EBOK_portrait.jpg",
    }
    assert result["counts"]["done"] == 3


def test_restore_without_yes_reports_the_plan_and_writes_nothing(fake_kindle, tmp_path, capsys):
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    mount = device.mount

    kindle_cli.run_remove(
        _remove_args(root, "--match", EN_AUTHOR, "--yes"),
        device_finder=lambda: device,
        backend_factory=_mass_storage_factory,
    )
    operation_id = _events(capsys)[-1]["data"]["operation"]

    exit_code = kindle_cli.run_restore(
        _restore_args(root, "--op", operation_id),
        device_finder=lambda: device,
        backend_factory=_mass_storage_factory,
    )
    assert exit_code == EXIT_OK
    assert not (mount / EN_PATH).exists()

    events = _events(capsys)
    # No --yes, so no backup stage either: this run cannot write anything.
    assert events[0]["stages"] == ["detect", "restore"]
    statuses = {e["status"] for e in events if e["type"] == "item"}
    assert statuses == {"pending"}
    # Hashing every selected file is what a restore does before it writes anything,
    # even here — so it reports progress rather than going silent.
    assert any(e["type"] == "progress" and e["stage"] == "verify" for e in events)
    assert not any(e["type"] == "progress" and e["stage"] == "restore" for e in events)


def test_restore_of_a_whole_snapshot_puts_every_backed_up_file_back(fake_kindle, tmp_path, capsys):
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    mount = device.mount

    kindle_cli.run_remove(
        _remove_args(root, "--match", EN_AUTHOR, "--yes"),
        device_finder=lambda: device,
        backend_factory=_mass_storage_factory,
    )
    snapshot_name = _events(capsys)[-1]["data"]["snapshot"]["snapshot"]
    (mount / PT_PATH).unlink()

    exit_code = kindle_cli.run_restore(
        _restore_args(root, snapshot_name, "--yes"),
        device_finder=lambda: device,
        backend_factory=_mass_storage_factory,
    )
    assert exit_code == EXIT_OK
    assert (mount / EN_PATH).is_file()
    assert (mount / PT_PATH).is_file()


def test_restore_of_an_add_operation_puts_nothing_back_and_says_so(fake_kindle, tmp_path, capsys):
    """`restore` only ever WRITES files back; it never deletes. An `add` put files
    ON the device, so the snapshot that protected it holds none of them — the honest
    answer is "nothing to put back", not a silent success."""
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    new_book = tmp_path / "books" / "A Brand New Book.azw3"
    new_book.parent.mkdir(parents=True)
    new_book.write_bytes(mobi_bytes(book_id=NEW_ID, title="A Brand New Book", language="en"))

    add_args = build_parser().parse_args(
        ["ebook", "kindle", "add", str(new_book), "--json", "-o", str(root)]
    )
    assert (
        kindle_cli.run_add(
            add_args, device_finder=lambda: device, backend_factory=_mass_storage_factory
        )
        == EXIT_OK
    )
    operation_id = _events(capsys)[-1]["data"]["operation"]

    exit_code = kindle_cli.run_restore(
        _restore_args(root, "--op", operation_id, "--yes"),
        device_finder=lambda: device,
        backend_factory=_mass_storage_factory,
    )
    assert exit_code == EXIT_OK
    # Still on the device: restoring an add is not a removal.
    assert (device.mount / "documents" / "en" / "A Brand New Book.azw3").is_file()

    result = _events(capsys)[-1]
    assert result["data"]["restore"]["not_in_snapshot"] == [
        "documents/en/A Brand New Book.azw3",
    ]
    assert result["outputs"] == []


def test_restore_with_an_unknown_operation_id_fails_without_touching_the_device(
    fake_kindle, tmp_path, capsys
):
    device = prepare_device(fake_kindle)
    exit_code = kindle_cli.run_restore(
        _restore_args(tmp_path / "media", "--op", "nope-00000000"),
        device_finder=lambda: device,
        backend_factory=_mass_storage_factory,
    )
    assert exit_code == EXIT_USAGE
    events = _events(capsys)
    assert events[-1]["type"] == "result"
    assert any(e["type"] == "error" for e in events)
    assert (device.mount / EN_PATH).is_file()


# --- the two defects the review caught ----------------------------------------------------


class _RefusingBackend:
    """Mass storage underneath, except that `remove` of one named path always fails —
    the way a real device refuses one file and accepts the next."""

    def __init__(self, mount: Path, *, refuse: str) -> None:
        self._inner = massstorage.MassStorageBackend(mount)
        self._refuse = refuse

    def list_files(self, prefix: str = ""):
        return self._inner.list_files(prefix)

    def read(self, path: str, dest: Path) -> None:
        self._inner.read(path, dest)

    def read_many(self, items) -> None:
        self._inner.read_many(items)

    def write(self, local: Path, path: str) -> None:
        self._inner.write(local, path)

    def remove(self, path: str) -> None:
        if path == self._refuse:
            raise OSError(f"the device refused to delete {path} (simulated)")
        self._inner.remove(path)

    def exists(self, path: str) -> bool:
        return self._inner.exists(path)

    def free_space(self) -> int:
        return self._inner.free_space()

    def eject(self) -> None:
        self._inner.eject()

    def close(self) -> None:
        self._inner.close()


class _InterruptingBackend:
    """Deletes the first book and then takes a Ctrl+C, so the journal guard around the
    delete loop is exercised for real."""

    def __init__(self, mount: Path) -> None:
        self._inner = massstorage.MassStorageBackend(mount)
        self._books_removed = 0

    def list_files(self, prefix: str = ""):
        return self._inner.list_files(prefix)

    def read(self, path: str, dest: Path) -> None:
        self._inner.read(path, dest)

    def read_many(self, items) -> None:
        self._inner.read_many(items)

    def write(self, local: Path, path: str) -> None:
        self._inner.write(local, path)

    def remove(self, path: str) -> None:
        if path.endswith((".azw3", ".mobi", ".kfx")):
            if self._books_removed >= 1:
                raise KeyboardInterrupt
            self._books_removed += 1
        self._inner.remove(path)

    def exists(self, path: str) -> bool:
        return self._inner.exists(path)

    def free_space(self) -> int:
        return self._inner.free_space()

    def eject(self) -> None:
        self._inner.eject()

    def close(self) -> None:
        self._inner.close()


def test_a_sideloaded_conversion_never_takes_a_refused_purchases_drm_assets(
    fake_kindle, tmp_path, capsys
):
    """The defect this closes: `A Purchased Book.kfx` (refused) and `A Purchased
    Book.azw3` (a sideloaded conversion) share one `.sdr`. If the refused book counts
    as "being removed", the conversion sees an unshared sidecar and takes the
    purchase's `assets/` with it — leaving a book that can never be read again."""
    device = prepare_device(fake_kindle, purchased=True)
    mount = device.mount
    conversion = mount / "documents" / "A Purchased Book.azw3"
    conversion.write_bytes(mobi_bytes(book_id="CONVERSION000001", title="A Purchased Book"))

    args = _remove_args(tmp_path / "media", "--match", "purchased", "--yes")
    exit_code = kindle_cli.run_remove(
        args, device_finder=lambda: device, backend_factory=_mass_storage_factory
    )
    # The refused purchase still makes the run exit 1 — but the conversion went.
    assert exit_code == EXIT_FAILED
    assert not conversion.exists()
    assert (mount / KFX_PATH).is_file()
    assert (mount / KFX_SDR / "assets" / "resource.res").is_file()

    result = _events(capsys)[-1]
    assert f"{KFX_SDR}/assets/resource.res" not in result["data"]["removed"]
    row = next(b for b in result["data"]["books"] if b["device_path"].endswith(".azw3"))
    assert row["status"] == "done"
    assert row["shared_with"] == [KFX_PATH]


def test_a_sidecar_is_kept_when_a_co_selected_book_fails_to_leave(fake_kindle, tmp_path, capsys):
    """Both books were planned for removal, so the last of them takes their shared
    `.sdr` — unless the device refuses one at runtime, which turns it back into the
    shared case."""
    device = prepare_device(fake_kindle)
    mount = device.mount
    sibling = mount / "documents" / "en" / "A Book - An Author.mobi"
    sibling.write_bytes(mobi_bytes(book_id="SIBLINGBOOK00001", title=EN_TITLE, author=EN_AUTHOR))

    args = _remove_args(tmp_path / "media", "--match", EN_AUTHOR, "--yes")
    exit_code = kindle_cli.run_remove(
        args,
        device_finder=lambda: device,
        backend_factory=lambda d, *, cache_dir: _RefusingBackend(d.mount, refuse=EN_PATH),
    )
    assert exit_code == EXIT_FAILED

    assert (mount / EN_PATH).is_file()  # refused
    assert not sibling.exists()  # removed
    # The reading position belongs to the book that is still there.
    assert (mount / EN_SDR / "position.mbp").is_file()
    result = _events(capsys)[-1]
    row = next(b for b in result["data"]["books"] if b["device_path"].endswith(".mobi"))
    assert row["kept"] == [f"{EN_SDR}/position.mbp"]
    assert row["shared_with"] == [EN_PATH]


def test_a_book_outside_the_backed_up_area_is_never_selected_and_is_refused_by_name(
    fake_kindle, tmp_path, capsys
):
    """The defect this closes: a book in a folder of the user's own making is not in
    the snapshot the run just took, so deleting it could never be undone — and
    `restore --op` would have reported success while the book stayed gone."""
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    outside = device.mount / "Books" / "Novel.azw3"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(mobi_bytes(book_id="OUTSIDEBOOK00001", title="Novel", author="An Author"))

    # A --match never reaches it at all.
    assert (
        kindle_cli.run_remove(
            _remove_args(root, "--match", "novel", "--yes"),
            device_finder=lambda: device,
            backend_factory=_mass_storage_factory,
        )
        == EXIT_OK
    )
    assert [e for e in _events(capsys) if e["type"] == "item"] == []
    assert outside.is_file()

    # Naming it explicitly says why, rather than silently doing nothing.
    exit_code = kindle_cli.run_remove(
        _remove_args(root, "Books/Novel.azw3", "--yes"),
        device_finder=lambda: device,
        backend_factory=_mass_storage_factory,
    )
    assert exit_code == EXIT_FAILED
    item = next(e for e in _events(capsys) if e["type"] == "item")
    assert item["detail"].startswith(f"{kindle_cli.DETAIL_PROTECTED}: ")
    assert outside.is_file()


def test_sync_never_treats_a_book_outside_the_backed_up_area_as_an_extra(
    fake_kindle, tmp_path, capsys
):
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    outside = device.mount / "Books" / "Novel.azw3"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(mobi_bytes(book_id="OUTSIDEBOOK00001", title="Novel"))

    new_book = tmp_path / "library" / "A Brand New Book.azw3"
    new_book.parent.mkdir(parents=True)
    new_book.write_bytes(mobi_bytes(book_id=NEW_ID, title="A Brand New Book", language="en"))
    plant_library_batch(
        root,
        "library",
        [(NEW_ID, new_book, "en"), (EN_ID, new_book, "en"), (PT_ID, new_book, "pt")],
    )

    assert (
        kindle_cli.run_sync(
            _sync_args(root, "--batch", "library", "--delete-extras", "--yes"),
            device_finder=lambda: device,
            backend_factory=_mass_storage_factory,
        )
        == EXIT_OK
    )
    assert outside.is_file()
    assert _events(capsys)[-1]["data"]["removed"] == []


def test_a_protected_extra_is_skipped_by_sync_not_failed(fake_kindle, tmp_path, capsys):
    """A mirror that can never exit 0 teaches everyone to ignore exit 1 on the one
    command that deletes books. A purchased KFX is a permanent structural exclusion
    from a mirror, not a failed attempt."""
    device = prepare_device(fake_kindle, purchased=True)
    root = tmp_path / "media"
    new_book = tmp_path / "library" / "A Brand New Book.azw3"
    new_book.parent.mkdir(parents=True)
    new_book.write_bytes(mobi_bytes(book_id=NEW_ID, title="A Brand New Book", language="en"))
    plant_library_batch(
        root,
        "library",
        [(NEW_ID, new_book, "en"), (EN_ID, new_book, "en"), (PT_ID, new_book, "pt")],
    )

    exit_code = kindle_cli.run_sync(
        _sync_args(root, "--batch", "library", "--delete-extras", "--yes"),
        device_finder=lambda: device,
        backend_factory=_mass_storage_factory,
    )
    assert exit_code == EXIT_OK

    assert (device.mount / KFX_PATH).is_file()
    events = _events(capsys)
    item = next(e for e in events if e["type"] == "item" and e["input"] == KFX_PATH)
    assert item["status"] == "skipped"
    assert item["reason"] == "unsupported_input"
    assert item["detail"].startswith(f"{kindle_cli.DETAIL_PROTECTED}: ")
    assert events[-1]["counts"]["failed"] == 0


def test_delete_extras_is_refused_for_a_batch_that_never_finished(fake_kindle, tmp_path):
    """An interrupted build still writes a `run.json`; every book it never placed is
    missing from its ids, and would be read as "the library does not have this"."""
    root = tmp_path / "media"
    new_book = tmp_path / "library" / "A Brand New Book.azw3"
    new_book.parent.mkdir(parents=True)
    new_book.write_bytes(mobi_bytes(book_id=NEW_ID, title="A Brand New Book", language="en"))
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
                        "data": {"book_id": NEW_ID, "language": "en", "output": str(new_book)},
                    },
                    {"id": 2, "status": "pending", "data": {}},
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(UsageError):
        kindle_cli.run_sync(
            _sync_args(root, "--batch", "library", "--delete-extras", "--yes"),
            device_finder=lambda: fake_kindle,
            backend_factory=_mass_storage_factory,
        )
    # Without --delete-extras the same batch is fine: adding is not deleting.
    assert (
        kindle_cli.run_sync(
            _sync_args(root, "--batch", "library"),
            device_finder=lambda: prepare_device(fake_kindle),
            backend_factory=_mass_storage_factory,
        )
        == EXIT_OK
    )


def test_asin_removes_every_copy_carrying_that_id(fake_kindle, tmp_path, capsys):
    device = prepare_device(fake_kindle)
    mount = device.mount
    second = mount / "documents" / "en" / "Another Copy.azw3"
    second.write_bytes(mobi_bytes(book_id=EN_ID, title=EN_TITLE, author=EN_AUTHOR))

    exit_code = kindle_cli.run_remove(
        _remove_args(tmp_path / "media", "--asin", EN_ID, "--yes"),
        device_finder=lambda: device,
        backend_factory=_mass_storage_factory,
    )
    assert exit_code == EXIT_OK
    assert not (mount / EN_PATH).exists()
    assert not second.exists()
    items = [e for e in _events(capsys) if e["type"] == "item"]
    assert {e["input"] for e in items} == {EN_PATH, "documents/en/Another Copy.azw3"}


def test_an_interrupted_removal_still_journals_what_it_already_took(fake_kindle, tmp_path, capsys):
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"

    exit_code = kindle_cli.run_remove(
        _remove_args(root, "--match", "documents/", "--yes"),
        device_finder=lambda: device,
        backend_factory=lambda d, *, cache_dir: _InterruptingBackend(d.mount),
    )
    assert exit_code == 130

    entries = backup_module.journal_read(root, device.serial)
    assert len(entries) == 1
    assert entries[0]["op"] == "remove"
    # Exactly what really went, and nothing the run never got to.
    assert entries[0]["paths"]
    for path in entries[0]["paths"]:
        assert not (device.mount / path).exists()


def test_restore_refuses_a_snapshot_taken_from_another_kindle(fake_kindle, tmp_path, capsys):
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    kindle_cli.run_remove(
        _remove_args(root, "--match", EN_AUTHOR, "--yes"),
        device_finder=lambda: device,
        backend_factory=_mass_storage_factory,
    )
    snapshot_dir = _events(capsys)[-1]["data"]["snapshot"]["path"]

    other = Device(
        serial="G000OTHERSERIAL",
        product_id=device.product_id,
        mode="mass_storage",
        mount=device.mount,
    )
    exit_code = kindle_cli.run_restore(
        _restore_args(root, snapshot_dir, "--yes"),
        device_finder=lambda: other,
        backend_factory=_mass_storage_factory,
    )
    assert exit_code == EXIT_USAGE
    assert not (device.mount / EN_PATH).exists()

    # ...and --force is how a user says they really meant it.
    assert (
        kindle_cli.run_restore(
            _restore_args(root, snapshot_dir, "--yes", "--force"),
            device_finder=lambda: other,
            backend_factory=_mass_storage_factory,
        )
        == EXIT_OK
    )
    assert (device.mount / EN_PATH).is_file()


def test_sync_keeps_a_thumbnail_another_surviving_book_still_uses(fake_kindle, tmp_path, capsys):
    """`sync` knows every device book's id, so it can tell that a cover belongs to a
    book it is NOT removing — here one outside the backed-up area, which can never be
    an extra — and leaves it alone."""
    device = prepare_device(fake_kindle)
    root = tmp_path / "media"
    mount = device.mount
    shared_id = "TWOCOPIESONEID01"
    keeper = mount / "Books" / "Novel.azw3"
    keeper.parent.mkdir(parents=True)
    keeper.write_bytes(mobi_bytes(book_id=shared_id, title="Novel"))
    (mount / "documents" / "en" / "Novel Copy.azw3").write_bytes(
        mobi_bytes(book_id=shared_id, title="Novel")
    )
    thumb = mount / "system" / "thumbnails" / f"thumbnail_{shared_id}_EBOK_portrait.jpg"
    thumb.write_bytes(b"thumb")

    new_book = tmp_path / "library" / "A Brand New Book.azw3"
    new_book.parent.mkdir(parents=True)
    new_book.write_bytes(mobi_bytes(book_id=NEW_ID, title="A Brand New Book", language="en"))
    plant_library_batch(
        root,
        "library",
        [(NEW_ID, new_book, "en"), (EN_ID, new_book, "en"), (PT_ID, new_book, "pt")],
    )

    assert (
        kindle_cli.run_sync(
            _sync_args(root, "--batch", "library", "--delete-extras", "--yes"),
            device_finder=lambda: device,
            backend_factory=_mass_storage_factory,
        )
        == EXIT_OK
    )

    assert not (mount / "documents" / "en" / "Novel Copy.azw3").exists()
    assert keeper.is_file()
    assert thumb.is_file()
    row = next(
        r
        for r in _events(capsys)[-1]["data"]["removals"]
        if r["device_path"].endswith("Novel Copy.azw3")
    )
    assert row["kept"] == [f"system/thumbnails/thumbnail_{shared_id}_EBOK_portrait.jpg"]
    assert row["shared_with"] == ["Books/Novel.azw3"]
