"""`media-tools ebook kindle status|scan|backup`: the first three Kindle commands that
are safe to run against a real device — none of them writes to it (`backup` writes only
to the host) — and the one place every later command (`add`, `remove`, `sync`,
`thumbnails`, `eject`, `restore`) resolves a device through.

**Device resolution lives in exactly one helper, `resolve_device`.** It does detection
(`device_finder`, defaulting to `detect.find_device`) and backend construction
(`backend_factory`, defaulting to `default_backend_factory`) and nothing else — both
parameters are the seam a test drives a fake device through instead of touching real
hardware. `_run`, below, is what every command in this module calls `resolve_device`
through, and it is also where `DeviceNotFound`/`DeviceBusy`/`DeviceWriteProtected` (and
the backend-specific exceptions `list_files`/`read`/`free_space` may raise instead — see
`_error_code_for`) turn into the matching exit-3 `error` code.

**Hold ONE backend instance across a backup and whatever operation follows it, and
never re-detect between them.** `resolve_device` is called exactly once per command
here, and its backend is threaded through the rest of that command by hand rather than
re-resolved. This matters more than it looks: the one genuinely dangerous ordering left
in this whole design is an unmount (or an MTP session drop) during a backup followed by
a remount/reconnect before the destructive step that was supposed to follow it — a
second `resolve_device` call in between could silently hand a caller a DIFFERENT
device than the one it just backed up. A future command that backs up and then writes
(`add`, `remove`, `sync`) must keep this same shape: one `resolve_device` call, one
backend, passed explicitly into both steps.

**`scan` reads a book's title/author/language from its EXTH records (113/503/100/524
via `tasks.ebook.exth`), never from its filename.** Over mass storage that means
reading the file straight off the mount; MTP has no local path at all, so each book is
materialised once into `<output root>/_kindle/<serial>/.cache/headers/`, keyed by
device path + size + mtime (`_cache_key`), and reused on every later scan — see
`_materialize_for_scan`. The whole batch goes through `read_many` in one round trip,
never a per-file loop (the same rule `backup.py` follows, for the same reason: a
per-file MTP call re-opens and re-scans the device every time).

**A listing's exception type is deliberately unspecified** (`backend.DeviceBackend`'s
own docstring): mass storage raises `FileNotFoundError` for a device that vanished
after detection, MTP raises `DeviceNotFound`/`DeviceBusy`/a `CalibreError` depending on
what its helper reported. `_error_code_for` is the one place that maps whichever of
those (all either `RuntimeError` or `FileNotFoundError`) `_run` catches to the closed
registry's matching code — never assuming `FileNotFoundError` is the only shape a
failure takes, and never treating an empty result as success (an empty listing is
resolved by the backends themselves, per their own docstrings; a FAILED listing raises
and is not swallowed here).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from media_tools.core.events import EXIT_DEPENDENCY, EXIT_INTERRUPTED, EXIT_OK, Reporter
from media_tools.core.paths import output_root, sanitize_batch
from media_tools.core.runner import empty_result
from media_tools.tasks.common import UsageError
from media_tools.tasks.ebook import exth
from media_tools.tasks.ebook.kindle import backup as backup_module
from media_tools.tasks.ebook.kindle import detect, massstorage, mtp
from media_tools.tasks.ebook.kindle.backend import DeviceBackend, DeviceFile, DeviceWriteProtected
from media_tools.tasks.ebook.kindle.detect import Device, DeviceBusy, DeviceNotFound

NAME = "kindle"
HELP = "Report on, scan or back up a connected Kindle."

# The formats `exth.read_records` can actually parse (MOBI-family containers).
BOOK_SUFFIXES = frozenset({".azw", ".azw3", ".azw8", ".kfx", ".mobi", ".prc", ".pdb"})
_THUMBNAIL_PREFIX = "system/thumbnails/"


# --- argument wiring --------------------------------------------------------------


def register_subparsers(kindle_parser) -> None:
    subparsers = kindle_parser.add_subparsers(dest="kindle_command", required=True)

    status_help = (
        "Report the connected Kindle's mode, serial, free space, last backup and "
        "whether anything appears to hold it."
    )
    status_parser = subparsers.add_parser("status", help=status_help, description=status_help)
    _add_kindle_flags(status_parser)

    scan_help = "List every book on the connected Kindle, read from its own EXTH records."
    scan_parser = subparsers.add_parser("scan", help=scan_help, description=scan_help)
    _add_kindle_flags(scan_parser)
    scan_parser.add_argument(
        "--compare",
        metavar="BATCH",
        default=None,
        help="Classify each book as device-only, library-only or both against an "
        "`ebook build`/`ebook scan` batch, comparing by book id.",
    )

    backup_help = (
        "Snapshot the connected Kindle's user content onto the host. Never writes to the device."
    )
    backup_parser = subparsers.add_parser("backup", help=backup_help, description=backup_help)
    _add_kindle_flags(backup_parser)
    backup_parser.add_argument(
        "--full",
        action="store_true",
        help="Re-transfer every file; ignore the previous snapshot's hard-link reuse.",
    )
    backup_parser.add_argument(
        "--verify-hashes",
        action="store_true",
        help="Recompute every reused file's hash instead of trusting the previous "
        "snapshot's recorded one (checks the BACKUP, not the device).",
    )


def _add_kindle_flags(parser) -> None:
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="Output root (default: MEDIA_TOOLS_OUT, the repo's media/, or ./media).",
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_mode", help="Emit JSON Lines events on stdout."
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Only errors and the summary.")


def run(args) -> int:
    if args.kindle_command == "status":
        return run_status(args)
    if args.kindle_command == "scan":
        return run_scan(args)
    if args.kindle_command == "backup":
        return run_backup(args)
    raise UsageError(f"unknown kindle subcommand: {args.kindle_command!r}")  # pragma: no cover


# --- the one device-resolution helper ----------------------------------------------


def default_backend_factory(device: Device, *, cache_dir: Path) -> DeviceBackend:
    if device.mode == "mass_storage":
        if device.mount is None:  # pragma: no cover - detect.find_device never does this
            raise DeviceNotFound("a mass-storage Kindle was detected with no mount point")
        return massstorage.MassStorageBackend(device.mount)
    return mtp.MtpBackend(device, cache_dir=cache_dir)


def resolve_device(
    root: Path,
    *,
    device_finder: Callable[[], Device],
    backend_factory: Callable[..., DeviceBackend],
) -> tuple[Device, DeviceBackend]:
    """Find the device and build ONE backend for it — the seam every Kindle command
    (this task's three, and every later one) resolves a device through. `device_finder`
    and `backend_factory` are what a test substitutes for real hardware; a caller that
    needs the backend for more than one step must hold the returned instance itself
    rather than calling this again (see this module's own docstring for why)."""
    device = device_finder()
    key = backup_module.device_key(device)
    cache_dir = backup_module.backup_root(root, key) / ".cache"
    backend = backend_factory(device, cache_dir=cache_dir)
    return device, backend


def _error_code_for(error: BaseException) -> str:
    """The one mapping from whatever a backend raised to the closed registry's matching
    code. Order matters: `DeviceBusy`/`DeviceWriteProtected`/`BackupFailed` are checked
    before the broader `(DeviceNotFound, FileNotFoundError)` pair so a more specific
    diagnosis is never shadowed by a looser one."""
    if isinstance(error, DeviceBusy):
        return "device_busy"
    if isinstance(error, DeviceWriteProtected):
        return "device_write_protected"
    if isinstance(error, backup_module.BackupFailed):
        return "backup_failed"
    if isinstance(error, (DeviceNotFound, FileNotFoundError)):
        return "device_not_found"
    # Anything else this subsystem raises (a bare `CalibreError`, e.g. calibre-debug
    # itself going missing) is not about the device's presence/availability — Calibre
    # failed to run at all, which is a dependency problem, not a device one.
    return "dependency_missing"


def _fail(reporter: Reporter, code: str, message: str) -> int:
    reporter.error(code=code, message=message)
    reporter.result(**empty_result(EXIT_DEPENDENCY))
    return EXIT_DEPENDENCY


def _run(
    args,
    *,
    kindle_command: str,
    stages: list[str],
    options: dict,
    device_finder: Callable[[], Device],
    backend_factory: Callable[..., DeviceBackend],
    body: Callable[[Reporter, Path, Device, DeviceBackend], dict],
) -> int:
    """The shared skeleton every kindle subcommand runs through: emit `start`, resolve
    ONE device+backend, run `body`, and turn whatever it raises into the right exit-3
    `error` — always followed by exactly one `result`, whether the run succeeded,
    failed, or was interrupted. `body` returns the kwargs `reporter.result()` needs
    beyond `ok`/`exit_code` (always `True`/`EXIT_OK` here; every failure path returns
    from the `except` clauses below instead)."""
    reporter = Reporter(json_mode=args.json_mode, quiet=args.quiet)
    root = output_root(args.output_dir)
    reporter.start(
        tool="ebook",
        batch=f"kindle-{kindle_command}",
        output_dir=root,
        stages=stages,
        items=0,  # unknown until the device answers; result.counts is authoritative
        options=options,
    )
    reporter.stage(stage=stages[0], index=1, count=len(stages))

    backend: DeviceBackend | None = None
    try:
        device, backend = resolve_device(
            root, device_finder=device_finder, backend_factory=backend_factory
        )
        result_kwargs = body(reporter, root, device, backend)
    except KeyboardInterrupt:
        reporter.error(code="interrupted", message="interrupted by user")
        reporter.result(**empty_result(EXIT_INTERRUPTED))
        return EXIT_INTERRUPTED
    except (RuntimeError, FileNotFoundError) as error:
        return _fail(reporter, _error_code_for(error), str(error))
    finally:
        if backend is not None:
            backend.close()

    reporter.result(ok=True, exit_code=EXIT_OK, **result_kwargs)
    return EXIT_OK


# --- status --------------------------------------------------------------------------


def run_status(
    args,
    *,
    device_finder: Callable[[], Device] | None = None,
    backend_factory: Callable[..., DeviceBackend] | None = None,
) -> int:
    def body(reporter: Reporter, root: Path, device: Device, backend: DeviceBackend) -> dict:
        free_space = backend.free_space()
        held_by = None
        if device.mode == "mtp" and mtp.calibre_gui_is_running():
            held_by = "calibre_gui"

        key = backup_module.device_key(device)
        last_dir = backup_module.latest(root, key)
        last_info = _snapshot_summary(last_dir) if last_dir is not None else None
        abandoned = _abandoned_partials(backup_module.backup_root(root, key) / "backups")

        data = {
            "device": {
                "mode": device.mode,
                "backend": type(backend).__name__,
                "model_hint": device.model_hint,
                "serial": device.serial,
                "free_space": free_space,
                "held_by": held_by,
            },
            "backup": {
                "last": last_info,
                "abandoned_partials": abandoned,
            },
        }
        return {
            "counts": {"total": 0, "done": 0, "skipped": 0, "failed": 0, "pending": 0},
            "failed": [],
            "pending": [],
            "outputs": [],
            "run_file": None,
            "data": data,
        }

    return _run(
        args,
        kindle_command="status",
        stages=["detect"],
        options={"kindle_command": "status"},
        device_finder=device_finder or detect.find_device,
        backend_factory=backend_factory or default_backend_factory,
        body=body,
    )


def _snapshot_summary(snapshot_dir: Path) -> dict | None:
    try:
        manifest = json.loads(
            (snapshot_dir / backup_module.MANIFEST_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
    sizes = manifest.get("bytes") if isinstance(manifest.get("bytes"), dict) else {}
    return {
        "snapshot": manifest.get("snapshot"),
        "path": str(snapshot_dir),
        "created_at": manifest.get("created_at"),
        "files": counts.get("files"),
        "bytes_copied": sizes.get("copied"),
        "bytes_linked": sizes.get("linked"),
    }


def _abandoned_partials(backups_dir: Path) -> list[str]:
    """Snapshot staging directories a killed/crashed backup left behind under their
    `.partial` name (`core.paths.temp_path`'s own naming) — nothing prunes these
    automatically (backup.py's own rule: this tool never deletes a user's backups, and
    an abandoned `.partial` was never promoted to one), so `status` is what lets a user
    notice and delete them by hand."""
    if not backups_dir.is_dir():
        return []
    return sorted(str(p) for p in backups_dir.glob(".*.partial") if p.is_dir())


# --- scan ------------------------------------------------------------------------


def run_scan(
    args,
    *,
    device_finder: Callable[[], Device] | None = None,
    backend_factory: Callable[..., DeviceBackend] | None = None,
) -> int:
    root = output_root(args.output_dir)
    compare_batch = None
    library_index: dict[str, dict] | None = None
    if args.compare:
        # Resolved and read BEFORE `start` — like every other task's own upfront
        # argument validation (e.g. `ebook build`'s inputs/--list check) — so a bad
        # --compare value is a plain usage error (no `start`, no `result`) rather than
        # a device-shaped failure.
        compare_batch = sanitize_batch(args.compare)
        library_index = _read_library_index(root, compare_batch)

    stages = ["detect", "scan"] + (["compare"] if compare_batch else [])
    options = {"kindle_command": "scan", "compare": compare_batch}

    def body(reporter: Reporter, root: Path, device: Device, backend: DeviceBackend) -> dict:
        reporter.stage(stage="scan", index=2, count=len(stages))
        all_entries = backend.list_files()
        all_paths = {entry.path for entry in all_entries}
        books = [
            entry
            for entry in all_entries
            if PurePosixPath(entry.path).suffix.lower() in BOOK_SUFFIXES
        ]

        if device.mode == "mass_storage":
            records_by_path = {
                book.path: exth.read_records(device.mount / book.path) for book in books
            }
        else:
            key = backup_module.device_key(device)
            cache_dir = backup_module.backup_root(root, key) / ".cache" / "headers"
            local_paths = _materialize_for_scan(backend, books, cache_dir)
            records_by_path = {
                book.path: exth.read_records(local_paths[book.path]) for book in books
            }

        result_books = []
        for item_id, book in enumerate(books, start=1):
            records = records_by_path[book.path]
            book_id = exth.record_text(records, exth.TAG_UUID)
            title = exth.record_text(records, exth.TAG_TITLE)
            author = exth.record_text(records, exth.TAG_AUTHOR)
            language = exth.record_text(records, exth.TAG_LANGUAGE)
            has_sdr = _has_sdr(book.path, all_paths)
            has_thumbnail = bool(book_id) and _has_thumbnail(book_id, all_paths)
            warnings = [] if book_id else ["book_id_missing"]

            reporter.item(
                id=item_id,
                status="done",
                input=book.path,
                outputs=[],
                bytes_in=book.size,
                reason=None,
                warnings=warnings,
            )
            result_books.append(
                {
                    "path": book.path,
                    "book_id": book_id,
                    "title": title,
                    "author": author,
                    "language": language,
                    "size": book.size,
                    "mtime": book.mtime,
                    "has_sdr": has_sdr,
                    "has_thumbnail": has_thumbnail,
                }
            )

        data = {"books": result_books}
        if library_index is not None:
            reporter.stage(stage="compare", index=3, count=len(stages))
            data["compare"] = _classify(result_books, library_index, batch=compare_batch)

        return {
            "counts": {
                "total": len(result_books),
                "done": len(result_books),
                "skipped": 0,
                "failed": 0,
                "pending": 0,
            },
            "failed": [],
            "pending": [],
            "outputs": [],
            "run_file": None,
            "data": data,
        }

    return _run(
        args,
        kindle_command="scan",
        stages=stages,
        options=options,
        device_finder=device_finder or detect.find_device,
        backend_factory=backend_factory or default_backend_factory,
        body=body,
    )


def _has_sdr(book_path: str, all_paths: set[str]) -> bool:
    stem = book_path[: -len(PurePosixPath(book_path).suffix)]
    prefix = f"{stem}.sdr/"
    return any(path.startswith(prefix) for path in all_paths)


def _has_thumbnail(book_id: str, all_paths: set[str]) -> bool:
    # TODO(task-6): match through `thumbnails.thumbnail_name(book_id, cdetype)` once
    # that lands, the same way `backup._companions_of` flags it. The id-inside-the-name
    # substring match is deliberate until then (CDE type and suffix vary by firmware).
    return any(
        path.startswith(_THUMBNAIL_PREFIX) and book_id in PurePosixPath(path).name
        for path in all_paths
    )


def _cache_key(path: str, size: int, mtime: float) -> str:
    digest = hashlib.sha256(f"{path}|{size}|{mtime}".encode()).hexdigest()[:24]
    return f"{digest}{PurePosixPath(path).suffix}"


def _materialize_for_scan(
    backend: DeviceBackend, books: list[DeviceFile], cache_dir: Path
) -> dict[str, Path]:
    """Local copies of every book, keyed by device path — MTP has no local path at all,
    so this is what lets `scan` read EXTH off it. Keyed by device path + size + mtime
    (`_cache_key`): an unchanged book is never re-pulled on a later scan, and every
    book that IS missing from the cache is fetched in ONE `read_many` batch rather than
    a per-file loop, which is unusable on a real library (`backup.py`'s own rule)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, Path] = {}
    to_fetch: list[tuple[str, Path]] = []
    for book in books:
        local = cache_dir / _cache_key(book.path, book.size, book.mtime)
        mapping[book.path] = local
        if not local.is_file():
            to_fetch.append((book.path, local))
    if to_fetch:
        backend.read_many(to_fetch)
    return mapping


def _read_library_index(root: Path, batch_name: str) -> dict[str, dict]:
    """book id -> {title, author, language, path} for every surviving (`status ==
    "done"`) item in an `ebook build`/`ebook scan` batch's `run.json`, read directly
    rather than through `RunState.open` — which would lock a batch this command has no
    business touching."""
    run_file = root / batch_name / "run.json"
    try:
        data = json.loads(run_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise UsageError(
            f"cannot read batch {batch_name!r} for --compare ({error}); run "
            "`media-tools ebook build` (or `ebook scan`) first, or check -o/--output-dir",
        ) from error
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise UsageError(f"batch {batch_name!r} has no readable run.json items for --compare")

    index: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("status") != "done":
            continue
        book_data = item.get("data") if isinstance(item.get("data"), dict) else {}
        book_id = book_data.get("book_id")
        if not book_id:
            continue
        index[book_id] = {
            "title": book_data.get("title"),
            "author": book_data.get("author"),
            "language": book_data.get("language"),
            "path": book_data.get("output"),
        }
    return index


def _classify(device_books: list[dict], library_index: dict[str, dict], *, batch: str) -> dict:
    device_by_id = {book["book_id"]: book for book in device_books if book.get("book_id")}
    device_ids = set(device_by_id)
    library_ids = set(library_index)
    return {
        "batch": batch,
        "device_only": [device_by_id[book_id] for book_id in sorted(device_ids - library_ids)],
        "library_only": [
            {"book_id": book_id, **library_index[book_id]}
            for book_id in sorted(library_ids - device_ids)
        ],
        "both": [
            {
                "book_id": book_id,
                "device": device_by_id[book_id],
                "library": library_index[book_id],
            }
            for book_id in sorted(device_ids & library_ids)
        ],
    }


# --- backup ------------------------------------------------------------------------


def run_backup(
    args,
    *,
    device_finder: Callable[[], Device] | None = None,
    backend_factory: Callable[..., DeviceBackend] | None = None,
) -> int:
    stages = ["detect", "backup"]
    options = {
        "kindle_command": "backup",
        "full": bool(args.full),
        "verify_hashes": bool(args.verify_hashes),
    }

    def body(reporter: Reporter, root: Path, device: Device, backend: DeviceBackend) -> dict:
        reporter.stage(stage="backup", index=2, count=len(stages))
        key = backup_module.device_key(device)

        def on_progress(done: int, total: int, phase: str) -> None:
            reporter.progress(
                stage=phase,
                index=done,
                count=total,
                path=key,
                percent=(100.0 * done / total) if total else 100.0,
            )

        def on_warning(code: str, message: str) -> None:
            reporter.warning(code=code, message=message)

        # A single backend instance, resolved once by `_run` above and threaded
        # through unchanged — see this module's own docstring for why re-detecting
        # between a backup and a later step is the one ordering this design forecloses.
        snap = backup_module.snapshot(
            backend,
            root=root,
            serial=key,
            full=args.full,
            verify_hashes=args.verify_hashes,
            on_progress=on_progress,
            on_warning=on_warning,
        )

        reporter.item(
            id=1,
            status="done",
            input=f"kindle:{key}",
            outputs=[str(snap.manifest)],
            bytes_in=snap.bytes_copied,
            bytes_out=snap.bytes_copied + snap.bytes_linked,
            reason=None,
            warnings=list(snap.warnings),
        )
        return {
            "counts": {"total": 1, "done": 1, "skipped": 0, "failed": 0, "pending": 0},
            "failed": [],
            "pending": [],
            "outputs": [str(snap.manifest)],
            "run_file": None,
            "data": {
                "snapshot": {
                    "path": str(snap.path),
                    "files": snap.files,
                    "bytes_copied": snap.bytes_copied,
                    "bytes_linked": snap.bytes_linked,
                    "manifest": str(snap.manifest),
                }
            },
        }

    return _run(
        args,
        kindle_command="backup",
        stages=stages,
        options=options,
        device_finder=device_finder or detect.find_device,
        backend_factory=backend_factory or default_backend_factory,
        body=body,
    )
