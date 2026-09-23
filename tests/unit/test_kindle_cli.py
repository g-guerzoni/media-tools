"""CLI-level coverage for `ebook kindle status|scan|backup`.

Everything is driven against the `fake_kindle` fixture (or a hand-built MTP device, for
the MTP-only behaviour mass storage can't exercise) through an injected
`device_finder`/`backend_factory` pair — the seam `kindle.cli.resolve_device` exists
for (Task 5's own controller decision: one helper, with the backend swappable for a
test). Nothing here touches real hardware; the two subprocess tests rely on there being
no actual Kindle attached in this environment, which is also what the offline suite
requires everywhere else.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

from media_tools.cli import build_parser
from media_tools.core.events import EXIT_DEPENDENCY, EXIT_FAILED, EXIT_INTERRUPTED, EXIT_OK
from media_tools.core.paths import temp_path
from media_tools.tasks.ebook.kindle import cli as kindle_cli
from media_tools.tasks.ebook.kindle import massstorage
from media_tools.tasks.ebook.kindle.backend import DeviceFile, DeviceWriteProtected
from media_tools.tasks.ebook.kindle.detect import Device, DeviceBusy, DeviceNotFound

EN_PATH = "documents/en/A Book - An Author.azw3"
EN_SDR_DIR = "documents/en/A Book - An Author.sdr"
PT_PATH = "documents/pt/Um Livro - Um Autor.azw3"
EN_ID = "ENBOOK0000000001"
PT_ID = "PTBOOK0000000001"

# Deliberately contradicts EN_PATH ("documents/en/A Book - An Author.azw3"): a naive
# filename parser would produce "A Book"/"An Author"/"en" from that path, so a
# regression to filename-derived metadata would still pass an assertion that only
# checked "the title isn't the bare filename stem". These values share nothing with the
# path, so `scan` reporting them is only possible by actually reading EXTH.
EN_TITLE = "Not The Filename At All"
EN_AUTHOR = "Someone Else Entirely"
EN_LANGUAGE = "de"


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
        mobi_bytes(book_id=EN_ID, title=EN_TITLE, author=EN_AUTHOR, language=EN_LANGUAGE)
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


def _mtp_device(serial: str = "MTPTESTSERIAL01") -> Device:
    return Device(serial=serial, product_id=0x9981, mode="mtp", mount=None)


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
    """A backend whose listing always fails, to prove `scan`/`status`/`backup` never
    treat a FAILED listing as an empty (successful) device. `backup.snapshot()` wraps
    whatever this raises into `BackupFailed`, which is what makes this fixture do
    double duty for the `backup_failed` exit-code test too."""

    def list_files(self, prefix: str = ""):
        raise FileNotFoundError("the Kindle is no longer mounted")

    def free_space(self) -> int:
        return 0

    def close(self) -> None:
        pass


class _RaisingBackend:
    """A backend every relevant method raises `error` from — for pinning
    `_error_code_for`'s mapping without needing a real device in that state."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def free_space(self):
        raise self._error

    def list_files(self, prefix: str = ""):
        raise self._error

    def close(self) -> None:
        pass


class _CloseRaisesBackend:
    """Wraps a real `MassStorageBackend` but makes `close()` fail, to prove a backend
    cleanup failure never turns an already-successful (or already-failed) command into
    an uncaught `internal_error`."""

    def __init__(self, mount: Path) -> None:
        self._inner = massstorage.MassStorageBackend(mount)

    def list_files(self, prefix: str = ""):
        return self._inner.list_files(prefix)

    def free_space(self) -> int:
        return self._inner.free_space()

    def close(self) -> None:
        raise RuntimeError("close() blew up")


class _FakeMtpBackend:
    """A minimal stand-in for `MtpBackend`: enough of `DeviceBackend`'s surface for
    `scan`'s MTP branch (`list_files`, `read_many`) plus `free_space`/`close`, backed by
    an in-memory `{path: bytes}` map. Records every `read_many` call so a test can
    assert the single-batch-fetch contract, and can be told to fail specific paths to
    exercise the "one unfetchable book" recovery `_materialize_for_scan` provides."""

    def __init__(self, files: dict[str, bytes], *, fail_paths: frozenset[str] = frozenset()):
        self._files = dict(files)
        self._fail_paths = set(fail_paths)
        self.read_many_calls: list[list[tuple[str, Path]]] = []

    def list_files(self, prefix: str = ""):
        return [
            DeviceFile(path=path, size=len(data), mtime=1_700_000_000.0)
            for path, data in self._files.items()
        ]

    def read_many(self, items: list[tuple[str, Path]]) -> None:
        self.read_many_calls.append(list(items))
        first_failure = None
        for path, dest in items:
            if path in self._fail_paths:
                first_failure = first_failure or path
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self._files[path])
        if first_failure is not None:
            # Mirrors `MtpBackend.read_many`'s own contract: raises what `read` would
            # raise for the FIRST failed pair. A real MTP invocation is one round trip
            # for the whole batch, so everything that COULD succeed already has by the
            # time this raises — matched here by writing every non-failing file above
            # before raising for the first failure found.
            raise FileNotFoundError(f"{first_failure} not found on the device")

    def free_space(self) -> int:
        return 123_456

    def close(self) -> None:
        pass


def _set_file_bytes(backend: _FakeMtpBackend, path: str, data: bytes) -> None:
    backend._files[path] = data  # noqa: SLF001 - test-only access to the fake's own state


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
    # A stable literal, not `type(backend).__name__` — renaming the Python class must
    # never change this JSON.
    assert device_data["backend"] == "mass_storage"
    assert device_data["serial"] == "G000TESTSERIAL"
    assert device_data["free_space"] is not None
    assert result["data"]["backup"]["last"] is None
    assert result["data"]["backup"]["abandoned_partials"] == []
    assert result["data"]["backup"]["header_cache_bytes"] == 0


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


def test_status_reports_held_by_for_mtp_when_calibre_gui_is_running(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(kindle_cli.mtp, "calibre_gui_is_running", lambda: True)
    device = _mtp_device()
    backend = _FakeMtpBackend(files={})
    args = build_parser().parse_args(
        ["ebook", "kindle", "status", "--json", "-o", str(tmp_path / "media")]
    )
    exit_code = kindle_cli.run_status(
        args, device_finder=lambda: device, backend_factory=lambda d, *, cache_dir: backend
    )
    assert exit_code == EXIT_OK
    result = _events(capsys)[-1]
    assert result["data"]["device"]["held_by"] == "calibre_gui"
    # A mass-storage device has no single-holder protocol, so it is never "held".
    assert result["data"]["device"]["mode"] == "mtp"


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


# --- error-code mapping and lifecycle -------------------------------------------------


def test_device_busy_maps_to_device_busy_and_exits_3(tmp_path, capsys):
    args = build_parser().parse_args(
        ["ebook", "kindle", "status", "--json", "-o", str(tmp_path / "media")]
    )
    exit_code = kindle_cli.run_status(
        args,
        device_finder=_mtp_device,
        backend_factory=lambda d, *, cache_dir: _RaisingBackend(DeviceBusy("someone else has it")),
    )
    assert exit_code == EXIT_DEPENDENCY
    events = _events(capsys)
    assert any(e.get("code") == "device_busy" for e in events if e["type"] == "error")


def test_device_write_protected_maps_to_device_write_protected_and_exits_3(tmp_path, capsys):
    args = build_parser().parse_args(
        ["ebook", "kindle", "status", "--json", "-o", str(tmp_path / "media")]
    )
    exit_code = kindle_cli.run_status(
        args,
        device_finder=_mtp_device,
        backend_factory=lambda d, *, cache_dir: _RaisingBackend(
            DeviceWriteProtected("the Kindle refused the write as read-only.")
        ),
    )
    assert exit_code == EXIT_DEPENDENCY
    events = _events(capsys)
    assert any(e.get("code") == "device_write_protected" for e in events if e["type"] == "error")


def test_a_permission_error_maps_to_device_not_found_not_an_uncaught_crash(
    fake_kindle, tmp_path, capsys
):
    """`_run` used to catch only `(RuntimeError, FileNotFoundError)`; a yanked volume
    raises `PermissionError`/`OSError(EIO)`/`OSError(ENODEV)` just as often, and any of
    those used to escape uncaught. Widened to `(RuntimeError, OSError)`."""
    kindle = kindle_device(fake_kindle)
    args = build_parser().parse_args(
        ["ebook", "kindle", "status", "--json", "-o", str(tmp_path / "media")]
    )
    exit_code = kindle_cli.run_status(
        args,
        device_finder=lambda: kindle,
        backend_factory=lambda d, *, cache_dir: _RaisingBackend(
            PermissionError("Operation not permitted")
        ),
    )
    assert exit_code == EXIT_DEPENDENCY
    events = _events(capsys)
    assert any(e.get("code") == "device_not_found" for e in events if e["type"] == "error")


def test_keyboard_interrupt_exits_130_with_interrupted(tmp_path, capsys):
    args = build_parser().parse_args(
        ["ebook", "kindle", "status", "--json", "-o", str(tmp_path / "media")]
    )

    def finder():
        raise KeyboardInterrupt

    exit_code = kindle_cli.run_status(
        args, device_finder=finder, backend_factory=kindle_cli.default_backend_factory
    )
    assert exit_code == EXIT_INTERRUPTED
    events = _events(capsys)
    assert events[-1]["type"] == "result"
    assert events[-1]["exit_code"] == EXIT_INTERRUPTED
    assert any(e.get("code") == "interrupted" for e in events if e["type"] == "error")


def test_a_close_failure_does_not_turn_a_successful_status_into_internal_error(
    fake_kindle, tmp_path, capsys
):
    kindle = kindle_device(fake_kindle)
    args = build_parser().parse_args(
        ["ebook", "kindle", "status", "--json", "-o", str(tmp_path / "media")]
    )
    exit_code = kindle_cli.run_status(
        args,
        device_finder=lambda: kindle,
        backend_factory=lambda d, *, cache_dir: _CloseRaisesBackend(d.mount),
    )
    assert exit_code == EXIT_OK
    result = _events(capsys)[-1]
    assert result["ok"] is True


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
    assert en["title"] == EN_TITLE
    assert en["author"] == EN_AUTHOR
    assert en["language"] == EN_LANGUAGE
    assert en["book_id"] == EN_ID
    assert en["has_sdr"] is True
    assert en["has_thumbnail"] is True
    # Never read from the filename: EN_PATH's own directory/name claims "en"/"A
    # Book"/"An Author" — every one of the planted EXTH values above contradicts it, so
    # a regression to filename-derived metadata would fail these, not silently pass.
    assert en["title"] != "A Book"
    assert en["author"] != "An Author"
    assert en["language"] != "en"

    pt = books[PT_PATH]
    assert pt["title"] == "Um Livro"
    assert pt["author"] == "Um Autor"
    assert pt["language"] == "pt"
    assert pt["book_id"] == PT_ID
    assert pt["has_sdr"] is False
    assert pt["has_thumbnail"] is False


def test_scan_warns_book_id_missing_for_a_book_with_no_exth_113(fake_kindle, tmp_path, capsys):
    kindle = kindle_device(fake_kindle)
    (kindle.mount / PT_PATH).write_bytes(
        mobi_bytes(title="Um Livro", author="Um Autor", language="pt")  # no book_id
    )
    args = build_parser().parse_args(
        ["ebook", "kindle", "scan", "--json", "-o", str(tmp_path / "media")]
    )
    exit_code = kindle_cli.run_scan(
        args, device_finder=lambda: kindle, backend_factory=_mass_storage_factory
    )
    assert exit_code == EXIT_OK

    item_events = {e["input"]: e for e in _events(capsys) if e["type"] == "item"}
    assert item_events[PT_PATH]["warnings"] == ["book_id_missing"]
    assert item_events[EN_PATH]["warnings"] == []


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
    # `library_only` entries describe an absolute HOST path under "output", not "path"
    # — `data.books[]`'s own "path" is a device-relative POSIX path, and reusing the
    # same key name for two different kinds of value in one payload is the trap.
    (only,) = compare["library_only"]
    assert only["output"] == "/library/en/Only In The Library.azw3"
    assert "path" not in only


def test_compare_with_a_reserved_batch_name_exits_2_not_internal_error(tmp_path):
    """`--compare _kindle` — the exact directory name this feature introduces, so a
    user will genuinely try it — used to raise `BatchNameError` uncaught, which
    escaped to `cli.main`'s catch-all as `internal_error`/exit 1 instead of the plain
    usage error (exit 2) every other `sanitize_batch` caller in this codebase gives."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "media_tools",
            "ebook",
            "kindle",
            "scan",
            "--compare",
            "_kindle",
            "--json",
            "-o",
            str(tmp_path / "media"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(e["type"] == "error" and e.get("code") == "usage" for e in events)
    assert not any(e.get("code") == "internal_error" for e in events)
    # The global fix (item 11): `result` follows even a pre-`start` `UsageError`.
    assert any(e["type"] == "result" for e in events)


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


# --- scan over MTP --------------------------------------------------------------------


def test_scan_over_mtp_survives_one_unfetchable_book(tmp_path, capsys):
    """A book deleted/renamed on the device between `list_files()` and the fetch makes
    `read_many` raise (all-or-nothing, per its own contract) — that must not abort the
    whole scan, the exact divergence from mass storage the backend contract exists to
    prevent."""
    device = _mtp_device()
    good = "documents/en/Good Book.azw3"
    gone = "documents/en/Gone Book.azw3"
    backend = _FakeMtpBackend(
        files={
            good: mobi_bytes(
                book_id="GOODBOOK00000001", title="Good Book", author="A", language="en"
            ),
            gone: b"irrelevant padding, this fetch is made to fail",
        },
        fail_paths=frozenset({gone}),
    )
    args = build_parser().parse_args(
        ["ebook", "kindle", "scan", "--json", "-o", str(tmp_path / "media")]
    )
    exit_code = kindle_cli.run_scan(
        args, device_finder=lambda: device, backend_factory=lambda d, *, cache_dir: backend
    )
    assert exit_code == EXIT_OK  # the whole scan must NOT abort over one bad book

    events = _events(capsys)
    books = {b["path"]: b for b in events[-1]["data"]["books"]}
    assert books[good]["title"] == "Good Book"
    assert books[gone]["title"] is None
    assert books[gone]["book_id"] is None

    item_events = {e["input"]: e for e in events if e["type"] == "item"}
    assert item_events[good]["warnings"] == []
    assert item_events[gone]["warnings"] == ["book_id_missing"]


def test_scan_over_mtp_fetches_every_new_book_in_one_read_many_call(tmp_path, capsys):
    device = _mtp_device()
    files = {
        f"documents/en/Book {i}.azw3": mobi_bytes(
            book_id=f"BOOK{i:012d}", title=f"Book {i}", author="A", language="en"
        )
        for i in range(5)
    }
    backend = _FakeMtpBackend(files=files)
    args = build_parser().parse_args(
        ["ebook", "kindle", "scan", "--json", "-o", str(tmp_path / "media")]
    )
    exit_code = kindle_cli.run_scan(
        args, device_finder=lambda: device, backend_factory=lambda d, *, cache_dir: backend
    )
    assert exit_code == EXIT_OK
    assert len(backend.read_many_calls) == 1
    assert len(backend.read_many_calls[0]) == 5


def test_scan_over_mtp_reuses_the_cache_then_invalidates_and_prunes_it(tmp_path, capsys):
    device = _mtp_device()
    root = tmp_path / "media"
    path = "documents/en/Book.azw3"
    backend = _FakeMtpBackend(
        files={
            path: mobi_bytes(book_id="VERSIONONE0001", title="Version 1", author="A", language="en")
        }
    )
    args = build_parser().parse_args(["ebook", "kindle", "scan", "--json", "-o", str(root)])
    header_cache_dir = root / "_kindle" / device.serial / ".cache" / "headers"

    def cached_files() -> set[str]:
        return {p.name for p in header_cache_dir.glob("*") if p.name != "index.json"}

    kindle_cli.run_scan(
        args, device_finder=lambda: device, backend_factory=lambda d, *, cache_dir: backend
    )
    _events(capsys)
    assert len(backend.read_many_calls) == 1
    first_cache = cached_files()
    assert len(first_cache) == 1

    # Same size/mtime on a second scan -> the cache is reused, no new fetch.
    kindle_cli.run_scan(
        args, device_finder=lambda: device, backend_factory=lambda d, *, cache_dir: backend
    )
    _events(capsys)
    assert len(backend.read_many_calls) == 1

    # The book changes size on the device -> a new cache key -> a re-fetch, and the
    # SUPERSEDED cached copy is pruned rather than accumulating forever in a directory
    # that (despite its "headers" name) stores whole books.
    _set_file_bytes(
        backend,
        path,
        mobi_bytes(book_id="VERSIONTWO0001", title="Version 2", author="A", language="en")
        + b"padding to change the size",
    )
    kindle_cli.run_scan(
        args, device_finder=lambda: device, backend_factory=lambda d, *, cache_dir: backend
    )
    third_events = _events(capsys)
    assert len(backend.read_many_calls) == 2
    second_cache = cached_files()
    assert len(second_cache) == 1
    assert second_cache != first_cache
    assert third_events[-1]["data"]["books"][0]["title"] == "Version 2"

    # `status` reports the header cache's size alongside the abandoned `.partial` dirs.
    status_args = build_parser().parse_args(
        ["ebook", "kindle", "status", "--json", "-o", str(root)]
    )
    kindle_cli.run_status(
        status_args, device_finder=lambda: device, backend_factory=lambda d, *, cache_dir: backend
    )
    status_result = _events(capsys)[-1]
    assert status_result["data"]["backup"]["header_cache_bytes"] > 0


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
    # Same shape `status`'s `data.backup.last` uses (`_snapshot_summary`, shared).
    assert result["data"]["snapshot"]["manifest"] == str(snapshot_path / "manifest.json")
    assert result["data"]["snapshot"]["created_at"] is not None

    item_events = [e for e in events if e["type"] == "item"]
    assert len(item_events) == 1
    assert item_events[0]["status"] == "done"
    assert item_events[0]["outputs"] == [str(snapshot_path / "manifest.json")]


def test_backup_and_status_report_the_same_snapshot_shape(fake_kindle, tmp_path, capsys):
    kindle = kindle_device(fake_kindle)
    root = tmp_path / "media"
    backup_args = build_parser().parse_args(
        ["ebook", "kindle", "backup", "--json", "-o", str(root)]
    )
    kindle_cli.run_backup(
        backup_args, device_finder=lambda: kindle, backend_factory=_mass_storage_factory
    )
    backup_snapshot = _events(capsys)[-1]["data"]["snapshot"]

    status_args = build_parser().parse_args(
        ["ebook", "kindle", "status", "--json", "-o", str(root)]
    )
    kindle_cli.run_status(
        status_args, device_finder=lambda: kindle, backend_factory=_mass_storage_factory
    )
    status_snapshot = _events(capsys)[-1]["data"]["backup"]["last"]

    assert set(backup_snapshot) == set(status_snapshot)
    assert backup_snapshot == status_snapshot


def test_backup_failure_maps_to_backup_failed_and_exits_1_not_3(fake_kindle, tmp_path, capsys):
    """The snapshot IS the work `backup` was asked to do, so a failure inside it is
    "at least one item failed" (exit 1), not "missing dependency or configuration"
    (exit 3) — unlike `device_not_found`/`device_busy`, which keep exit 3."""
    kindle = kindle_device(fake_kindle)
    args = build_parser().parse_args(
        ["ebook", "kindle", "backup", "--json", "-o", str(tmp_path / "media")]
    )
    exit_code = kindle_cli.run_backup(
        args,
        device_finder=lambda: kindle,
        backend_factory=lambda d, *, cache_dir: _BrokenListingBackend(),
    )
    assert exit_code == EXIT_FAILED
    events = _events(capsys)
    assert events[-1]["exit_code"] == EXIT_FAILED
    assert any(e.get("code") == "backup_failed" for e in events if e["type"] == "error")


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
