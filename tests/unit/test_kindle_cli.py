"""CLI-level coverage for `ebook kindle status|scan|backup`.

Everything is driven against the `fake_kindle` fixture through an injected
`device_finder`/`backend_factory` pair — the seam `kindle.cli.resolve_device` exists
for (Task 5's own controller decision: one helper, with the backend swappable for a
test). Nothing here touches real hardware; the one subprocess test relies on there
being no actual Kindle attached in this environment, which is also what the offline
suite requires everywhere else.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

from media_tools.cli import build_parser
from media_tools.core.events import EXIT_DEPENDENCY, EXIT_OK
from media_tools.core.paths import temp_path
from media_tools.tasks.ebook.kindle import cli as kindle_cli
from media_tools.tasks.ebook.kindle import massstorage
from media_tools.tasks.ebook.kindle.detect import DeviceNotFound

EN_PATH = "documents/en/A Book - An Author.azw3"
EN_SDR_DIR = "documents/en/A Book - An Author.sdr"
PT_PATH = "documents/pt/Um Livro - Um Autor.azw3"
EN_ID = "ENBOOK0000000001"
PT_ID = "PTBOOK0000000001"


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
    Mirrors `test_kindle_backup.py`'s own `mobi_bytes` helper, extended with the three
    fields `scan` actually reads — the base `fake_kindle` fixture's planted books carry
    none of this, so a scan test needs its own real MOBI bytes to read titles from."""
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


def kindle_device(fake_kindle):
    """The shared `fake_kindle` fixture, with its two planted books replaced by real
    MOBI bytes (title/author/language/book id) so `scan` has something honest to read,
    plus an `.sdr` sidecar file and a matching thumbnail for the English book."""
    mount = fake_kindle.mount
    (mount / EN_PATH).write_bytes(
        mobi_bytes(book_id=EN_ID, title="A Book", author="An Author", language="en")
    )
    sdr_dir = mount / EN_SDR_DIR
    sdr_dir.mkdir(exist_ok=True)
    (sdr_dir / "book.mbp").write_bytes(b"reading position")
    (mount / "system" / "thumbnails" / f"thumbnail_{EN_ID}_EBOK_portrait.jpg").write_bytes(b"thumb")
    (mount / PT_PATH).write_bytes(
        mobi_bytes(book_id=PT_ID, title="Um Livro", author="Um Autor", language="pt")
    )
    return fake_kindle


def _mass_storage_factory(device, *, cache_dir):
    return massstorage.MassStorageBackend(device.mount)


def _events(capsys) -> list[dict]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _write_library_batch(root: Path, name: str, items: list[dict]) -> None:
    batch_dir = root / name
    batch_dir.mkdir(parents=True)
    run_data = {
        "v": 1,
        "task": "ebook",
        "engine_options": {},
        "batch": name,
        "output_dir": str(batch_dir),
        "status": "done",
        "owner": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "inputs": [],
        "counts": {
            "total": len(items),
            "done": len(items),
            "skipped": 0,
            "failed": 0,
            "pending": 0,
        },
        "items": items,
    }
    (batch_dir / "run.json").write_text(json.dumps(run_data), encoding="utf-8")


def _library_item(item_id: int, *, book_id: str, title: str, author: str, language: str) -> dict:
    return {
        "id": item_id,
        "input": f"/library/{title}.epub",
        "status": "done",
        "reason": None,
        "outputs": [{"path": f"{language}/{title}.azw3", "bytes": 1}],
        "bytes_in": 1,
        "elapsed_s": None,
        "warnings": [],
        "data": {
            "book_id": book_id,
            "title": title,
            "author": author,
            "language": language,
            "output": f"/library/{language}/{title}.azw3",
        },
    }


class _BrokenListingBackend:
    """A backend whose listing always fails, to prove `scan`/`status` never treat a
    FAILED listing as an empty (successful) device."""

    def list_files(self, prefix: str = ""):
        raise FileNotFoundError("the Kindle is no longer mounted")

    def free_space(self) -> int:
        return 0

    def close(self) -> None:
        pass


# --- argument wiring ----------------------------------------------------------------


def test_kindle_is_registered_as_an_ebook_subcommand_with_its_own_dest():
    args = build_parser().parse_args(["ebook", "kindle", "status", "--json"])
    assert args.ebook_command == "kindle"
    assert args.kindle_command == "status"


def test_ebook_run_dispatches_kindle_to_kindle_cli(monkeypatch, tmp_path, capsys):
    from media_tools.tasks import ebook as ebook_task

    monkeypatch.setattr(
        kindle_cli.detect,
        "find_device",
        lambda: (_ for _ in ()).throw(DeviceNotFound("no kindle for this test")),
    )
    args = build_parser().parse_args(
        ["ebook", "kindle", "status", "--json", "-o", str(tmp_path / "media")]
    )
    exit_code = ebook_task.run(args)
    assert exit_code == EXIT_DEPENDENCY
    events = _events(capsys)
    assert events[-1]["type"] == "result"
    assert events[-1]["exit_code"] == EXIT_DEPENDENCY


# --- status -------------------------------------------------------------------------


def test_status_reports_mass_storage_and_the_serial(fake_kindle, tmp_path, capsys):
    kindle = kindle_device(fake_kindle)
    args = build_parser().parse_args(
        ["ebook", "kindle", "status", "--json", "-o", str(tmp_path / "media")]
    )
    exit_code = kindle_cli.run_status(
        args, device_finder=lambda: kindle, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_OK

    events = _events(capsys)
    assert [e["type"] for e in events[:1]] == ["start"]
    result = events[-1]
    assert result["type"] == "result"
    assert result["ok"] is True
    device_data = result["data"]["device"]
    assert device_data["mode"] == "mass_storage"
    assert device_data["backend"] == "MassStorageBackend"
    assert device_data["serial"] == "G000TESTSERIAL"
    assert device_data["free_space"] is not None
    assert result["data"]["backup"]["last"] is None
    assert result["data"]["backup"]["abandoned_partials"] == []


def test_status_surfaces_an_abandoned_partial_snapshot(fake_kindle, tmp_path, capsys):
    kindle = kindle_device(fake_kindle)
    root = tmp_path / "media"
    backups_dir = root / "_kindle" / kindle.serial / "backups"
    backups_dir.mkdir(parents=True)
    abandoned = temp_path(backups_dir / "20260101T000000Z")
    abandoned.mkdir(parents=True)
    (abandoned / "manifest.json").write_text("{}", encoding="utf-8")

    args = build_parser().parse_args(["ebook", "kindle", "status", "--json", "-o", str(root)])
    exit_code = kindle_cli.run_status(
        args, device_finder=lambda: kindle, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_OK

    result = _events(capsys)[-1]
    assert str(abandoned) in result["data"]["backup"]["abandoned_partials"]


def test_no_device_exits_3_with_device_not_found_and_exactly_one_result(tmp_path, capsys):
    args = build_parser().parse_args(
        ["ebook", "kindle", "status", "--json", "-o", str(tmp_path / "media")]
    )

    def finder():
        raise DeviceNotFound("no kindle connected")

    exit_code = kindle_cli.run_status(
        args, device_finder=finder, backend_factory=kindle_cli.default_backend_factory
    )
    assert exit_code == EXIT_DEPENDENCY

    events = _events(capsys)
    results = [e for e in events if e["type"] == "result"]
    assert len(results) == 1
    assert results[0]["ok"] is False
    assert results[0]["exit_code"] == EXIT_DEPENDENCY
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["code"] == "device_not_found"


# --- scan ---------------------------------------------------------------------------


def test_scan_lists_the_planted_books_with_their_exth_derived_titles(fake_kindle, tmp_path, capsys):
    kindle = kindle_device(fake_kindle)
    args = build_parser().parse_args(
        ["ebook", "kindle", "scan", "--json", "-o", str(tmp_path / "media")]
    )
    exit_code = kindle_cli.run_scan(
        args, device_finder=lambda: kindle, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_OK

    events = _events(capsys)
    item_events = [e for e in events if e["type"] == "item"]
    assert len(item_events) == 2
    assert all(e["warnings"] == [] for e in item_events)  # both books carry a book id

    result = events[-1]
    books = {b["path"]: b for b in result["data"]["books"]}
    en = books[EN_PATH]
    assert en["title"] == "A Book"
    assert en["author"] == "An Author"
    assert en["language"] == "en"
    assert en["book_id"] == EN_ID
    assert en["has_sdr"] is True
    assert en["has_thumbnail"] is True

    pt = books[PT_PATH]
    assert pt["title"] == "Um Livro"
    assert pt["author"] == "Um Autor"
    assert pt["language"] == "pt"
    assert pt["book_id"] == PT_ID
    assert pt["has_sdr"] is False
    assert pt["has_thumbnail"] is False

    # Never read from the filename: the fixture's book *files* are named after
    # different people ("A Book - An Author") than nothing in their EXTH claims.
    assert en["title"] != Path(EN_PATH).stem


def test_scan_compare_classifies_device_only_library_only_and_both(fake_kindle, tmp_path, capsys):
    kindle = kindle_device(fake_kindle)
    root = tmp_path / "media"
    _write_library_batch(
        root,
        "library",
        [
            _library_item(1, book_id=EN_ID, title="A Book", author="An Author", language="en"),
            _library_item(
                2,
                book_id="LIBONLYBOOK00001",
                title="Only In The Library",
                author="Someone Else",
                language="en",
            ),
        ],
    )
    args = build_parser().parse_args(
        ["ebook", "kindle", "scan", "--compare", "library", "--json", "-o", str(root)]
    )
    exit_code = kindle_cli.run_scan(
        args, device_finder=lambda: kindle, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_OK

    compare = _events(capsys)[-1]["data"]["compare"]
    assert compare["batch"] == "library"
    assert {b["book_id"] for b in compare["both"]} == {EN_ID}
    assert {b["book_id"] for b in compare["device_only"]} == {PT_ID}
    assert {b["book_id"] for b in compare["library_only"]} == {"LIBONLYBOOK00001"}


def test_a_failed_listing_does_not_become_an_empty_scan(fake_kindle, tmp_path, capsys):
    kindle = kindle_device(fake_kindle)
    args = build_parser().parse_args(
        ["ebook", "kindle", "scan", "--json", "-o", str(tmp_path / "media")]
    )
    exit_code = kindle_cli.run_scan(
        args,
        device_finder=lambda: kindle,
        backend_factory=lambda device, *, cache_dir: _BrokenListingBackend(),
    )
    assert exit_code == EXIT_DEPENDENCY

    events = _events(capsys)
    results = [e for e in events if e["type"] == "result"]
    assert len(results) == 1
    assert results[0]["ok"] is False
    assert "data" not in results[0]
    item_events = [e for e in events if e["type"] == "item"]
    assert item_events == []
    errors = [e for e in events if e["type"] == "error"]
    assert errors[0]["code"] == "device_not_found"


# --- backup -------------------------------------------------------------------------


def test_backup_creates_a_snapshot_under_kindle_backups_and_reports_it(
    fake_kindle, tmp_path, capsys
):
    kindle = kindle_device(fake_kindle)
    root = tmp_path / "media"
    args = build_parser().parse_args(["ebook", "kindle", "backup", "--json", "-o", str(root)])
    exit_code = kindle_cli.run_backup(
        args, device_finder=lambda: kindle, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_OK

    events = _events(capsys)
    result = events[-1]
    assert result["ok"] is True
    snapshot_path = Path(result["data"]["snapshot"]["path"])
    assert snapshot_path.is_dir()
    assert snapshot_path.parent == root / "_kindle" / kindle.serial / "backups"
    assert (snapshot_path / "manifest.json").is_file()
    assert result["data"]["snapshot"]["files"] > 0

    item_events = [e for e in events if e["type"] == "item"]
    assert len(item_events) == 1
    assert item_events[0]["status"] == "done"
    assert item_events[0]["outputs"] == [str(snapshot_path / "manifest.json")]


# --- end-to-end (no real device in this environment) ---------------------------------


def test_ebook_kindle_status_cli_with_no_device_exits_3():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "media_tools",
            "ebook",
            "kindle",
            "status",
            "--json",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == EXIT_DEPENDENCY
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    result_events = [e for e in events if e["type"] == "result"]
    assert len(result_events) == 1
    assert result_events[0]["exit_code"] == EXIT_DEPENDENCY
    assert any(e.get("code") == "device_not_found" for e in events if e["type"] == "error")
