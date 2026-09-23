"""`media-tools ebook kindle status|scan|backup|thumbnails|add`, and the one place
every later command (`remove`, `sync`, `eject`, `restore`) resolves a device through.

`status`/`scan` never write anywhere; `backup` writes only to the host. `thumbnails`
and `add` write to the DEVICE itself — see their own docstrings, `run_thumbnails` and
`run_add` below, for what that means for the mandatory pre-write backup and per-book
failure handling. Do not read this module's name or this docstring's history as a
promise that nothing here writes: that was true through Task 5 and is no longer true.

**Device resolution lives in exactly one helper, `resolve_device`.** It does detection
(`device_finder`, defaulting to `detect.find_device`) and backend construction
(`backend_factory`, defaulting to `default_backend_factory`) and nothing else — both
parameters are the seam a test drives a fake device through instead of touching real
hardware. `_run`, below, is what every command in this module calls `resolve_device`
through, and it is also where `DeviceNotFound`/`DeviceBusy`/`DeviceWriteProtected` (and
the backend-specific exceptions `list_files`/`read`/`free_space` may raise instead — see
`_error_code_for`) turn into the matching `error` code. `_run` catches
`(RuntimeError, OSError)`, not a narrower pair: every documented device-path exception
is a `RuntimeError`, and the real unplug modes (`PermissionError`, `OSError(EIO)`,
`OSError(ENODEV)`, not just `FileNotFoundError`) are all `OSError`. Anything outside
that (an `AttributeError`/`KeyError`/etc., a genuine bug in this module) is deliberately
left to escape to `cli.py`'s own catch-all, which already gives it an honest
`internal_error`/exit 1 instead of a dishonest device-shaped code.

**Every mapped code exits 3, except `backup_failed`, which exits 1 AND reports
`counts.failed: 1`.** In the `backup` command the snapshot IS the work being asked for,
so a mid-transfer failure or a hash mismatch is "at least one item failed", not
"missing dependency or configuration" — `ok: false` with `counts.failed: 0` would
otherwise misreport a run that demonstrably did fail, which is why `run_backup`'s own
`body` catches `backup_module.BackupFailed` ITSELF (`counts: {"total": 1, "failed":
1, ...}`, a real `failed` entry, an `item` event) rather than letting it reach `_run`'s
generic `_fail`/`empty_result` path, which can only express "nothing was attempted" —
right for `device_not_found`/`device_busy`, wrong for a backup that really did run and
really did fail. This does NOT generalise to a WRITE command whose MANDATORY pre-write
backup fails — `run_thumbnails` and `run_add` here, and every future one
(`remove`/`sync`, Task 8): there the backup is a precondition and nothing the user
actually asked for was attempted, so that failure should keep exit 3 AND `_fail`'s
all-zero counts — see `_EXIT_FOR_CODE`'s own comment, and `_mandatory_backup` below
(the one place this shape is built, shared by every write command rather than
hand-copied by each).

**Hold ONE backend instance across a backup and whatever operation follows it, and
never re-detect between them.** `resolve_device` is called exactly once per command
here, and its backend is threaded through the rest of that command by hand rather than
re-resolved. This matters more than it looks: the one genuinely dangerous ordering left
in this whole design is an unmount (or an MTP session drop) during a backup followed by
a remount/reconnect before the destructive step that was supposed to follow it — a
second `resolve_device` call in between could silently hand a caller a DIFFERENT
device than the one it just backed up. `thumbnails` and `add` already keep this shape,
and a future command that backs up and then writes (`remove`, `sync`) must keep it
too: one `resolve_device` call, one backend, passed explicitly into both steps.
`backend.close()` itself runs AFTER the final `result` is emitted (or immediately
before a failure one), never in a `finally` ahead of it — a `close()` that raises
must never turn an otherwise-successful run into an uncaught `internal_error`.

**`scan` reads a book's title/author/language from its EXTH records (113/503/100/524
via `tasks.ebook.exth`), never from its filename.** Over mass storage that means
reading the file straight off the mount; MTP has no local path at all, so each book is
materialised once into `<output root>/_kindle/<serial>/.cache/headers/`, keyed by
device path + size + mtime (`_cache_key`), and reused on every later scan — see
`_materialize_for_scan`, which also prunes a path's OLD cached copy once a newer one
replaces it, so an edited book does not leave a forgotten full copy behind forever. The
prune (and the small on-disk index, `index.json`, that makes it possible) is entirely
best-effort: an `OSError` writing it, or a malformed/malicious entry read back from it
(validated by `_is_safe_cache_key` before it can drive a `Path.unlink()` call), is
swallowed rather than failing a scan that already succeeded, or reported as if the
DEVICE were the problem. The whole batch goes through `read_many` in one round trip,
never a per-file loop (the same rule `backup.py` follows, for the same reason: a
per-file MTP call re-opens and re-scans the device every time) — but `read_many` raises
all-or-nothing on the first op that fails, so `_materialize_for_scan` catches that and
treats whatever did not transfer as simply absent, rather than letting one book
deleted/renamed mid-scan abort the whole command (`exth.read_records` on a missing path
already returns `{}`, exactly matching mass storage's own behaviour for a vanished
book).

**A listing's exception type is deliberately unspecified** (`backend.DeviceBackend`'s
own docstring): mass storage raises `FileNotFoundError` for a device that vanished
after detection, MTP raises `DeviceNotFound`/`DeviceBusy`/a `CalibreError` depending on
what its helper reported. `_error_code_for` is the one place that maps whichever of
those `_run` catches to the closed registry's matching code — never assuming
`FileNotFoundError` is the only shape a failure takes, and never treating an empty
result as success (an empty listing is resolved by the backends themselves, per their
own docstrings; a FAILED listing raises and is not swallowed here).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from media_tools.core.events import (
    EXIT_DEPENDENCY,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    Reporter,
)
from media_tools.core.paths import BatchNameError, output_root, sanitize_batch
from media_tools.core.runner import empty_result
from media_tools.core.sizes import format_size
from media_tools.tasks.common import UsageError
from media_tools.tasks.ebook import exth
from media_tools.tasks.ebook.kindle import backup as backup_module
from media_tools.tasks.ebook.kindle import detect, massstorage, mtp, thumbnails
from media_tools.tasks.ebook.kindle.backend import (
    DeviceBackend,
    DeviceFile,
    DeviceWriteProtected,
    sanitize_device_name,
)
from media_tools.tasks.ebook.kindle.detect import Device, DeviceBusy, DeviceNotFound
from media_tools.tasks.ebook.normalize import IGNORED_LANGUAGE_TAGS

NAME = "kindle"
HELP = "Report on, scan, back up, add books to, or install cover thumbnails on a connected Kindle."

# The formats `exth.read_records` can actually parse (MOBI-family containers).
BOOK_SUFFIXES = frozenset({".azw", ".azw3", ".azw8", ".kfx", ".mobi", ".prc", ".pdb"})
# What `add` accepts as a SOURCE: the MOBI family above plus the two other formats a
# Kindle reads directly. An `.epub`/`.pdf` carries no EXTH 113 id, so it can never be
# recognised as already on the device and always reports `book_id_missing` — see
# `run_add`'s own docstring.
ADDABLE_SUFFIXES = BOOK_SUFFIXES | {".epub", ".pdf"}
_CACHE_INDEX_NAME = "index.json"

DOCUMENTS_DIR = "documents"
# Where a book with no usable language code lands. Deliberately a folder rather than
# `documents/` itself: ONE rule ("every book this tool adds lives in
# `documents/<lang>/`") is easier to reason about — and for Task 8's `restore --op` to
# undo — than two, and `ebook build` already shelves a language-less book under its own
# named folder rather than at the batch root.
UNKNOWN_LANGUAGE = "unknown"
# The FULL device path budget per backend, from spec 8.8's FAT32/MTP constraints.
# `backend.sanitize_device_name` caps only the NAME component, leaving the whole-path
# budget to whichever caller joins a directory onto that name (Ruling R3) — `add` is
# the first command in this subsystem that does, so this is where it lives.
MAX_DEVICE_PATH = {"mass_storage": 250, "mtp": 230}
# Never let the directory prefix eat the entire budget: `truncate_name` already floors
# its own `keep` at 1, but a name reduced to one character plus a hash marker is a
# collision waiting to happen, so a sane floor is applied before it is asked.
_MIN_NAME_BUDGET = 32
# A language becomes a DIRECTORY COMPONENT on the device and can come from a
# sideloaded book's own EXTH 524 — data this project did not produce. It is therefore
# matched against a conservative shape and REJECTED (falling back to
# `UNKNOWN_LANGUAGE`) rather than sanitized into a different-but-plausible folder name.
_LANGUAGE_CODE = re.compile(r"[a-z]{2,3}")
# `add`'s own per-book EXTH 113 index, next to the MTP header cache under
# `_kindle/<serial>/.cache/` and invalidated the same way (device path + size +
# mtime). It exists because `exth.read_records` needs a file's HEADER but reads the
# whole file to reach it, so collecting the device's ids the naive way costs a full
# pass over the library — off USB, or over MTP into the header cache — every time a
# single book is added. Scanning a library is `scan`'s job, not `add`'s.
_BOOK_ID_INDEX_NAME = "book-ids.json"

# --- `detail`'s prefix vocabulary ---------------------------------------------------
#
# The `reason` registry is closed and this task adds nothing to it, so five genuinely
# different causes share one `engine_error`. `detail` is what tells them apart — and
# an agent must be able to BRANCH on it, not substring-match English prose, so every
# `detail` this module produces is `"<term>: <human sentence>"` with the term drawn
# from exactly this list. `_PlannedBook.fail`/`.skip` join the two halves, so the
# shape cannot drift one call site at a time.
DETAIL_EXISTS = "exists"
DETAIL_SOURCE_MISSING = "source_missing"
DETAIL_OUTPUT_COLLISION = "output_collision"
DETAIL_OUT_OF_SPACE = "out_of_space"
DETAIL_WRITE_REFUSED = "write_refused"
DETAIL_SHORT_WRITE = "short_write"
DETAIL_VERIFY_FAILED = "verify_failed"
DETAIL_TERMS = frozenset(
    {
        DETAIL_EXISTS,
        DETAIL_SOURCE_MISSING,
        DETAIL_OUTPUT_COLLISION,
        DETAIL_OUT_OF_SPACE,
        DETAIL_WRITE_REFUSED,
        DETAIL_SHORT_WRITE,
        DETAIL_VERIFY_FAILED,
    }
)


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

    thumbnails_help = (
        "Install a cover thumbnail for every device book missing one. Takes a backup "
        "first, like every other command that writes to the device."
    )
    thumbnails_parser = subparsers.add_parser(
        "thumbnails", help=thumbnails_help, description=thumbnails_help
    )
    _add_kindle_flags(thumbnails_parser)
    thumbnails_parser.add_argument(
        "--force",
        action="store_true",
        help="Reinstall a thumbnail even for a book that already has one on the device.",
    )
    thumbnails_parser.add_argument(
        "--match",
        metavar="TEXT",
        default=None,
        help="Only consider books whose device path OR own title/author contains "
        "TEXT (case-insensitive) — e.g. to retry the handful that reported "
        "no_cover without re-running the backup and every other book.",
    )
    thumbnails_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report which books would be installed/skipped without taking a backup "
        "or writing to the device. Over MTP this still reads each matched book "
        "into the local header cache, the same way scan does.",
    )

    add_help = (
        "Copy books onto the connected Kindle, after a backup. A book already on the "
        "device (matched by its own EXTH 113 id, never by filename) is skipped."
    )
    add_parser = subparsers.add_parser("add", help=add_help, description=add_help)
    _add_kindle_flags(add_parser)
    add_parser.add_argument(
        "books",
        nargs="*",
        type=Path,
        metavar="BOOK",
        help="Books to copy (files, or folders scanned recursively for them). "
        "Mutually exclusive with --batch.",
    )
    add_parser.add_argument(
        "--batch",
        metavar="NAME",
        default=None,
        help="Take every book an `ebook build` batch placed, with the language that "
        "run resolved for each, instead of naming them on the command line.",
    )
    add_parser.add_argument(
        "--lang",
        metavar="XX",
        default=None,
        help="Put every book under documents/XX/ instead of its own language "
        "(a two- or three-letter code).",
    )
    add_parser.add_argument(
        "--match",
        metavar="TEXT",
        default=None,
        help="Only add books whose target device path OR own title/author contains "
        "TEXT (case-insensitive) — the same rule `thumbnails --match` uses.",
    )
    add_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report which books would be copied, skipped or refused without taking "
        "a backup or writing anything to the device.",
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
    if args.kindle_command == "thumbnails":
        return run_thumbnails(args)
    if args.kindle_command == "add":
        return run_add(args)
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
    before the broader `(DeviceNotFound, OSError)` pair so a more specific diagnosis is
    never shadowed by a looser one. `OSError` (not just `FileNotFoundError`) is what
    catches the other real unplug modes — `PermissionError`, `OSError(EIO)`,
    `OSError(ENODEV)` — that `free_space()` (a bare `shutil.disk_usage`) and
    `read`/`read_many` (copying bytes off a device that just went away) raise at least
    as often as a plain "not found"."""
    if isinstance(error, DeviceBusy):
        return "device_busy"
    if isinstance(error, DeviceWriteProtected):
        return "device_write_protected"
    if isinstance(error, backup_module.BackupFailed):
        return "backup_failed"
    if isinstance(error, (DeviceNotFound, OSError)):
        return "device_not_found"
    # Anything else this subsystem raises (a bare `CalibreError`, e.g. calibre-debug
    # itself going missing) is not about the device's presence/availability — Calibre
    # failed to run at all, which is a dependency problem, not a device one.
    return "dependency_missing"


# Every mapped code exits 3 (EXIT_DEPENDENCY) EXCEPT `backup_failed` — see this
# module's own docstring for why exit 1 is the honest code for `backup` specifically.
# A WRITE command that treats a mandatory pre-write backup as a precondition
# (`thumbnails` and `add` today, Task 8's `remove`/`sync` next) must NOT reuse this
# table for that failure: nothing the user asked for was attempted there, which is
# exit 3's meaning, not exit 1's — `_mandatory_backup` maps that case separately,
# rather than assuming every `backup_failed` means "exit 1".
#
# This table (via `_fail`, below) is the GENERIC path: `empty_result`'s all-zero counts,
# for a failure before any work was attempted. `run_backup`'s own body catches
# `BackupFailed` itself instead of letting it reach here, precisely because a snapshot
# failure is NOT that case — the backup WAS attempted and DID fail, so its `result`
# needs `counts.failed: 1`, not zero, which `_fail`/`empty_result` cannot express. The
# `"backup_failed"` entry below only matters if `BackupFailed` somehow escapes from
# EARLIER than that (e.g. `resolve_device` itself) — realistically unreachable, since
# `backup.device_key` never returns an empty key, but kept as an honest fallback rather
# than assumed impossible.
_EXIT_FOR_CODE = {
    "device_not_found": EXIT_DEPENDENCY,
    "device_busy": EXIT_DEPENDENCY,
    "device_write_protected": EXIT_DEPENDENCY,
    "dependency_missing": EXIT_DEPENDENCY,
    "backup_failed": EXIT_FAILED,
}


def _fail(reporter: Reporter, code: str, message: str) -> int:
    exit_code = _EXIT_FOR_CODE.get(code, EXIT_DEPENDENCY)
    reporter.error(code=code, message=message)
    reporter.result(**empty_result(exit_code))
    return exit_code


def _close_quietly(backend: DeviceBackend | None) -> None:
    """`close()` releases whatever the backend holds (mostly an MTP session's cached
    listing). A failure there must never turn an otherwise-settled `result` — success
    or failure, already built and about to be (or just) emitted — into an uncaught
    exception that `cli.py`'s catch-all would report as a misleading `internal_error`
    instead. Called AFTER the success `result`, or right before a failure one; never in
    a `finally` ahead of either."""
    if backend is None:
        return
    with contextlib.suppress(Exception):
        backend.close()


def _run(
    args,
    *,
    kindle_command: str,
    stages: list[str],
    options: dict,
    device_finder: Callable[[], Device],
    backend_factory: Callable[..., DeviceBackend],
    body: Callable[[Reporter, Path, Device, DeviceBackend], dict],
    items: int = 0,
) -> int:
    """The shared skeleton every kindle subcommand runs through: emit `start`, resolve
    ONE device+backend, run `body`, and turn whatever it raises into the right `error`
    — always followed by exactly one `result`, whether the run succeeded, failed, or
    was interrupted. `body` returns the kwargs `reporter.result()` needs; `ok` and
    `exit_code` default to `True`/`EXIT_OK` (the common case: `body` completed and has
    nothing to apologise for) but `body` may override both — `run_backup` does, when
    the ONE thing it was asked to do (the snapshot) itself fails, so the `result` can
    report `counts.failed: 1` instead of the all-zero shape `_fail` gives a failure
    that stopped the run before any work was attempted at all."""
    reporter = Reporter(json_mode=args.json_mode, quiet=args.quiet)
    root = output_root(args.output_dir)
    reporter.start(
        tool="ebook",
        batch=f"kindle-{kindle_command}",
        output_dir=root,
        stages=stages,
        # 0 for every command whose item count only the DEVICE can answer
        # (`status`/`scan`/`backup`/`thumbnails`); `add` knows its own sources before
        # it ever looks at a device and passes the real number. `result.counts` stays
        # authoritative either way — this is the up-front estimate, not the outcome.
        items=items,
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
        _close_quietly(backend)
        reporter.error(code="interrupted", message="interrupted by user")
        reporter.result(**empty_result(EXIT_INTERRUPTED))
        return EXIT_INTERRUPTED
    except (RuntimeError, OSError) as error:
        _close_quietly(backend)
        return _fail(reporter, _error_code_for(error), str(error))

    ok = result_kwargs.pop("ok", True)
    exit_code = result_kwargs.pop("exit_code", EXIT_OK)
    reporter.result(ok=ok, exit_code=exit_code, **result_kwargs)
    _close_quietly(backend)
    return exit_code


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
        header_cache_dir = backup_module.backup_root(root, key) / ".cache" / "headers"
        header_cache_bytes = _cache_size(header_cache_dir)

        data = {
            "device": {
                "mode": device.mode,
                "backend": _backend_label(backend),
                "model_hint": device.model_hint,
                "serial": device.serial,
                "free_space": free_space,
                "held_by": held_by,
            },
            "backup": {
                "last": last_info,
                "abandoned_partials": abandoned,
                "header_cache_bytes": header_cache_bytes,
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


# Maps a backend CLASS to a stable JSON literal for `data.device.backend` —
# `type(backend).__name__` would leak a Python class name into a public JSON contract,
# so renaming that class would silently break every consumer of this field. A backend
# this module does not recognise (a test double, or a future third backend not yet
# added here) reports `"unknown"` rather than crashing.
_BACKEND_LABELS: dict[type, str] = {
    massstorage.MassStorageBackend: "mass_storage",
    mtp.MtpBackend: "mtp",
}


def _backend_label(backend: DeviceBackend) -> str:
    return _BACKEND_LABELS.get(type(backend), "unknown")


def _snapshot_summary(
    snapshot_dir: Path, *, fallback: backup_module.Snapshot | None = None
) -> dict | None:
    """The ONE shape both `status`'s `data.backup.last` and `backup`'s `data.snapshot`
    use, so an agent parsing either field never has to handle two different shapes for
    the same kind of object. Built from the manifest ON DISK by preference — `backup`
    calls this with the snapshot it just took (`snap.path`), `status` with whatever
    `backup.latest()` finds — never by hand-assembling a second, subtly different dict
    from a live `Snapshot` object's own fields.

    `fallback`, when given (only `run_backup` has one to give — `status` never does,
    since it only ever has a `Path` from `backup.latest()`, not a live object), is used
    if the manifest cannot be read back. `backup.snapshot()` just fsynced it, so this
    should not happen, but it is cheap insurance against `data.snapshot` reporting
    `null` on a backup that, from the device's perspective, fully succeeded.
    """
    try:
        manifest = json.loads(
            (snapshot_dir / backup_module.MANIFEST_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        manifest = None
    if not isinstance(manifest, dict):
        if fallback is None:
            return None
        return {
            "snapshot": fallback.path.name,
            "path": str(fallback.path),
            "manifest": str(fallback.manifest),
            "created_at": None,
            "files": fallback.files,
            "bytes_copied": fallback.bytes_copied,
            "bytes_linked": fallback.bytes_linked,
        }
    counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
    sizes = manifest.get("bytes") if isinstance(manifest.get("bytes"), dict) else {}
    return {
        "snapshot": manifest.get("snapshot"),
        "path": str(snapshot_dir),
        "manifest": str(snapshot_dir / backup_module.MANIFEST_NAME),
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


def _cache_size(cache_dir: Path) -> int:
    """Total bytes of materialised MTP book copies under `.cache/headers/` — reported
    by `status` alongside the abandoned `.partial` snapshots, since this directory
    (despite its name) stores whole books and is only ever pruned incrementally, per
    device path, by `_materialize_for_scan` — never wholesale. Excludes the index file
    itself (`_CACHE_INDEX_NAME`), which is bookkeeping, not a cached book."""
    if not cache_dir.is_dir():
        return 0
    return sum(
        entry.stat().st_size
        for entry in cache_dir.rglob("*")
        if entry.is_file() and entry.name != _CACHE_INDEX_NAME
    )


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
        # --compare value is a plain usage error rather than a device-shaped failure.
        # `sanitize_batch` raises `BatchNameError` (a `ValueError`) for a reserved name
        # (e.g. `_kindle`, the exact directory this feature introduces — a user WILL
        # try it), a leading dot, or one that is empty/illegal after cleanup; every
        # other caller in this codebase (`build.py`, `tasks/common.py`,
        # `tasks/download/__init__.py`) wraps it into `UsageError` the same way.
        try:
            compare_batch = sanitize_batch(args.compare)
        except BatchNameError as error:
            raise UsageError(str(error)) from error
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
            cdetype = exth.record_text(records, exth.TAG_CDETYPE) or thumbnails.DEFAULT_CDETYPE
            has_sdr = _has_sdr(book.path, all_paths)
            has_thumbnail = bool(book_id) and _has_thumbnail(book_id, cdetype, all_paths)
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


def _has_thumbnail(book_id: str, cdetype: str, all_paths: set[str]) -> bool:
    """The EXACT name `thumbnails.thumbnail_name` builds, not a substring match
    against every path under `system/thumbnails/` — the same fix
    `backup._companions_of` applies to `restore`'s own pairing, for the same reason:
    an id-inside-the-name match could hit a DIFFERENT book's thumbnail that happened
    to share a substring."""
    return f"{thumbnails.THUMBNAIL_DIR}{thumbnails.thumbnail_name(book_id, cdetype)}" in all_paths


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
    a per-file loop, which is unusable on a real library (`backup.py`'s own rule).

    Two failure/lifecycle modes this handles, both load-bearing:

    - A book deleted or renamed on the device between `list_files()` and this fetch
      makes `read_many` raise (it is all-or-nothing per its own contract). That must
      not abort the whole scan — caught below, and whatever DID transfer is simply used;
      whatever did not is not at `local`, which `exth.read_records` already treats as
      "no records" (never re-raising is what keeps `scan` matching mass storage's own
      graceful behaviour for a vanished path).
    - An edited book gets a NEW cache key (its size/mtime changed), which would
      otherwise leave the OLD cached copy sitting here forever — this directory is a
      cache of whole books, not headers. A small on-disk index (`_CACHE_INDEX_NAME`)
      remembers each path's last cache key so the superseded file can be deleted once
      the new one is confirmed present.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    index_path = cache_dir / _CACHE_INDEX_NAME
    index = _read_cache_index(index_path)

    mapping: dict[str, Path] = {}
    to_fetch: list[tuple[str, Path]] = []
    for book in books:
        local = cache_dir / _cache_key(book.path, book.size, book.mtime)
        mapping[book.path] = local
        if not local.is_file():
            to_fetch.append((book.path, local))

    if to_fetch:
        # See this function's own docstring: one unfetchable book must not abort the
        # whole scan. Whatever succeeded is already on disk under `mapping`; whatever
        # did not simply is not there.
        with contextlib.suppress(RuntimeError, OSError):
            backend.read_many(to_fetch)

    index_changed = False
    for book in books:
        local = mapping[book.path]
        if not local.is_file():
            continue  # this book's fetch failed; leave any previous entry alone
        key = local.name
        previous_key = index.get(book.path)
        if previous_key and previous_key != key:
            stale = cache_dir / previous_key
            # `OSError` here (a read-only output root, a full disk, a permissions
            # change under `_kindle/`) must never fail a scan that already
            # successfully listed and fetched every book — every book was already
            # accounted for above; this is host-side cache bookkeeping, not a device
            # operation, and must not be reported as one (it would otherwise reach
            # `_run`'s widened `except (RuntimeError, OSError)` and be misdiagnosed as
            # the device having gone away, AFTER everything already succeeded).
            with contextlib.suppress(OSError):
                if stale.is_file():
                    stale.unlink(missing_ok=True)
        if previous_key != key:
            index[book.path] = key
            index_changed = True
    if index_changed:
        _write_cache_index(index_path, index)

    return mapping


def _is_safe_cache_key(value: object) -> bool:
    """A cache key must be a bare filename directly under `cache_dir` — never a path
    with a separator, and never empty or a directory-traversal token. `index.json` is
    this module's own state, so the likelihood of it holding anything else is low, but
    a value read back from it drives a `Path.unlink()` call above, and a delete driven
    by file contents deserves validating on principle rather than trusting it blindly."""
    return (
        isinstance(value, str)
        and value not in ("", ".", "..")
        and "/" not in value
        and "\\" not in value
    )


def _read_cache_index(index_path: Path) -> dict[str, str]:
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # An entry that fails `_is_safe_cache_key` is dropped rather than kept-but-unused:
    # the next successful materialise of that path simply treats it as never-cached
    # (no prune, since `index.get(book.path)` then reads as absent) and overwrites it
    # with a good value — self-healing, not just self-protecting.
    return {
        path: key for path, key in data.items() if isinstance(path, str) and _is_safe_cache_key(key)
    }


def _write_cache_index(index_path: Path, index: dict[str, str]) -> None:
    """Best-effort, deliberately NOT `core.paths.fsync_replace`'s temp+fsync+rename:
    this is a low-stakes cache, and a torn write here only means `_read_cache_index`
    falls back to `{}` next time, so the prune quietly stops working until the index
    can be written again — exactly the unbounded-growth failure mode this index exists
    to prevent, returning quietly rather than as a crash. `OSError` (a full disk, a
    read-only output root, ...) is swallowed for the same reason the prune above
    swallows it: this must never fail a scan that already succeeded."""
    with contextlib.suppress(OSError):
        index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")


def _read_library_items(root: Path, batch_name: str, *, flag: str) -> list[dict]:
    """Every item in an `ebook build`/`ebook scan` batch's `run.json`, read directly
    rather than through `RunState.open` — which would lock a batch these commands have
    no business touching. `flag` names the option that asked for the batch (`--compare`
    for `scan`, `--batch` for `add`) so the error message points at the right one."""
    run_file = root / batch_name / "run.json"
    try:
        data = json.loads(run_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise UsageError(
            f"cannot read batch {batch_name!r} for {flag} ({error}); run "
            "`media-tools ebook build` (or `ebook scan`) first, or check -o/--output-dir",
        ) from error
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise UsageError(f"batch {batch_name!r} has no readable run.json items for {flag}")
    return items


def _read_library_index(root: Path, batch_name: str) -> dict[str, dict]:
    """book id -> {title, author, language, output} for every surviving (`status ==
    "done"`) item in an `ebook build`/`ebook scan` batch's `run.json`."""
    index: dict[str, dict] = {}
    for item in _read_library_items(root, batch_name, flag="--compare"):
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
            # Named "output", not "path": `data.books[]`'s own "path" is a
            # device-relative POSIX path, while this is an absolute HOST path (where
            # `ebook build`/`ebook scan` placed the file) — the same key name for two
            # different kinds of value in one payload is exactly the trap an agent
            # writing one parser for this JSON would fall into.
            "output": book_data.get("output"),
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


def _take_backup(
    reporter: Reporter,
    root: Path,
    backend: DeviceBackend,
    key: str,
    *,
    full: bool = False,
    verify_hashes: bool = False,
) -> backup_module.Snapshot:
    """Take the backup every write command takes before touching the device, with
    progress/warning wired to `reporter` the same way every caller needs it.
    Extracted so the callback wiring is not hand-copied by every write command —
    `run_backup`, `run_thumbnails` and `run_add` are the three call sites today, and
    Task 8's `remove`/`sync` are meant to be the fourth and fifth rather than a fourth
    and fifth copy of it.

    Raises `backup_module.BackupFailed` on failure, UNTOUCHED, so each caller reacts
    to it its own way: `run_backup` reports it as `counts.failed: 1`/exit 1, since
    there the snapshot IS the work being asked for; every WRITE command reports it as
    a precondition that was never met/exit 3, via `_mandatory_backup` below. This
    function itself does not know or care which — it only takes the snapshot.
    """

    def on_progress(done: int, total: int, phase: str) -> None:
        reporter.progress(
            stage=phase,
            index=done,
            count=total,
            path=f"kindle:{key}",  # self-describing, not a bare serial
            percent=(100.0 * done / total) if total else 100.0,
        )

    def on_warning(code: str, message: str) -> None:
        reporter.warning(code=code, message=message)

    return backup_module.snapshot(
        backend,
        root=root,
        serial=key,
        full=full,
        verify_hashes=verify_hashes,
        on_progress=on_progress,
        on_warning=on_warning,
    )


def _mandatory_backup(
    reporter: Reporter, root: Path, backend: DeviceBackend, key: str
) -> tuple[dict | None, backup_module.Snapshot | None]:
    """For a WRITE command (not `backup` itself): take `_take_backup`'s snapshot and,
    on failure, return the `body`-shaped all-zero/exit-3 failure dict — the backup is
    a PRECONDITION here, never met, so nothing the user actually asked for was
    attempted (Ruling R27; see this module's own docstring on `backup_failed`).
    Returns `(None, snapshot)` on success, so the caller can both continue into its
    own write step AND report the protecting snapshot in its own `result.data`.
    """
    try:
        snap = _take_backup(reporter, root, backend, key)
    except backup_module.BackupFailed as error:
        reporter.error(code="backup_failed", message=str(error))
        return {
            "ok": False,
            "exit_code": EXIT_DEPENDENCY,
            "counts": {"total": 0, "done": 0, "skipped": 0, "failed": 0, "pending": 0},
            "failed": [],
            "pending": [],
            "outputs": [],
            "run_file": None,
            "data": {},
        }, None
    return None, snap


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

        # A single backend instance, resolved once by `_run` above and threaded
        # through unchanged — see this module's own docstring for why re-detecting
        # between a backup and a later step is the one ordering this design forecloses.
        try:
            snap = _take_backup(
                reporter,
                root,
                backend,
                key,
                full=args.full,
                verify_hashes=args.verify_hashes,
            )
        except backup_module.BackupFailed as error:
            # Caught HERE, not left to `_run`'s outer `except (RuntimeError, OSError)`:
            # the snapshot IS the work `backup` was asked to do, so this failure needs
            # `counts.failed: 1` (an item demonstrably failed), which only this body —
            # the one place that knows there is exactly one "item" — can report. `_fail`
            # would report all-zero counts, which is right for "nothing was attempted"
            # (device not found/busy/...) but wrong here.
            reporter.error(code="backup_failed", message=str(error))
            reporter.item(
                id=1,
                status="failed",
                input=f"kindle:{key}",
                outputs=[],
                bytes_in=None,
                reason="engine_error",
                warnings=[],
            )
            return {
                "ok": False,
                "exit_code": EXIT_FAILED,
                "counts": {"total": 1, "done": 0, "skipped": 0, "failed": 1, "pending": 0},
                # `detail` is `None`, never absent: every `failed` entry this
                # subsystem emits carries the key, so an agent reading one gets a
                # missing VALUE rather than a missing KEY (the same rule the
                # `--dry-run` `data` shapes follow).
                "failed": [
                    {"id": 1, "input": f"kindle:{key}", "reason": "engine_error", "detail": None}
                ],
                "pending": [],
                "outputs": [],
                "run_file": None,
                "data": {},
            }

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
            # `_snapshot_summary`, not a second hand-built dict: this must be the exact
            # same shape `status`'s `data.backup.last` reports for the same kind of
            # object (see that function's own docstring). `fallback=snap` is what keeps
            # `data.snapshot` from being `null` on an otherwise fully successful backup
            # in the implausible case the manifest it just wrote cannot be read back.
            "data": {"snapshot": _snapshot_summary(snap.path, fallback=snap)},
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


# --- thumbnails ----------------------------------------------------------------------


def run_thumbnails(
    args,
    *,
    device_finder: Callable[[], Device] | None = None,
    backend_factory: Callable[..., DeviceBackend] | None = None,
) -> int:
    """Install a cover thumbnail for every device book that lacks one (or, with
    `--force`, for every book regardless; `--match TEXT` narrows to books whose
    device path OR own EXTH title/author contains TEXT, case-insensitively — a
    device's filenames are often opaque, so a user typing an author name must not
    silently match nothing. This is the way to retry the handful of books that
    reported `no_cover` without re-running the backup and every other book).

    Writing a thumbnail is a device write, so it takes the SAME mandatory backup
    every write command takes (`_mandatory_backup`), through the SAME backend
    instance `_run` already resolved (see this module's own docstring on holding one
    backend across a backup and the operation that follows it) — unless `--dry-run`,
    which takes NO backup and writes nothing to the DEVICE, only reporting what a
    real run would do. **Over MTP, `--dry-run` still populates the local per-book
    header cache** (`_materialize_for_scan`, the same read `scan` already does) —
    reading each matched book's EXTH id/title/author needs a local copy of it
    regardless of whether anything is ever written to the device. A failure in the
    (real) backup is a precondition that was never met — nothing about the actual
    write was attempted — so it keeps exit 3 and `_mandatory_backup`'s
    all-zero-counts shape rather than `backup`'s own exit-1 "an item demonstrably
    failed" shape (Ruling R27). The snapshot that protected the run is reported in
    `result.data.snapshot` (the same shape `backup`/`status` use), and a run that
    actually changed a thumbnail is journalled (`backup_module.journal_append`) so a
    future `restore --op` has something to undo.

    Per book, after the backup:
    - Already carrying its exact thumbnail name (`_has_thumbnail`) and not
      `--force`: `skipped`/`exists`, never handed to `thumbnails.install` at all.
    - No EXTH 113 id at all: `skipped`/`no_cover` with the `book_id_missing`
      warning — the one book that can never get a thumbnail, named as such rather
      than folded into the same `no_cover` silence as "had an id, found no cover".
    - Installed: `done`.
    - The device accepted the write and silently dropped it (Colorsoft and newer, by
      design): `skipped`/`device_rejected` with the `device_rejected_thumbnail`
      warning — never `failed`, since the user did nothing wrong and nothing is
      broken.
    - A GENUINE device fault mid-write (full disk, a yanked cable,
      `DeviceWriteProtected`, an MTP `CalibreError`) — `thumbnails.install` reports
      this as `"failed"`, isolated per book: `failed`/`engine_error`, which DOES make
      the run's own `ok` false / exit 1, the same as any other task's failed item.
    - Had an id and no cover anywhere (cache miss and no embedded image): `skipped`/
      `no_cover`.
    """
    dry_run = bool(args.dry_run)
    stages = ["detect", "thumbnails"] if dry_run else ["detect", "backup", "thumbnails"]
    options = {
        "kindle_command": "thumbnails",
        "force": bool(args.force),
        "match": args.match,
        "dry_run": dry_run,
    }

    def body(reporter: Reporter, root: Path, device: Device, backend: DeviceBackend) -> dict:
        key = backup_module.device_key(device)
        snap: backup_module.Snapshot | None = None

        if not dry_run:
            reporter.stage(stage="backup", index=2, count=len(stages))
            failure, snap = _mandatory_backup(reporter, root, backend, key)
            if failure is not None:
                return failure

        reporter.stage(stage="thumbnails", index=len(stages), count=len(stages))
        all_entries = backend.list_files()
        all_paths = {entry.path for entry in all_entries}
        book_entries = [
            entry
            for entry in all_entries
            if PurePosixPath(entry.path).suffix.lower() in BOOK_SUFFIXES
        ]
        # `--match` is deliberately NOT applied here, even though every book's
        # device path is already known at this point. It also matches a book's own
        # EXTH title/author (below), which are not read until every book's records
        # are — a device's filenames are often opaque, and a user typing an author
        # name should not silently match nothing just because it isn't IN the path.
        # Pre-filtering by path alone here would be cheaper (fewer books to
        # materialise over MTP), but it would silently break title/author matching
        # for every `--match` call — do not "optimise" this back without also
        # reading `TAG_TITLE`/`TAG_AUTHOR` before it.

        local_paths: dict[str, Path] = {}
        if device.mode == "mass_storage":
            records_by_path = {
                book.path: exth.read_records(device.mount / book.path) for book in book_entries
            }
        else:
            cache_dir = backup_module.backup_root(root, key) / ".cache" / "headers"
            local_paths = _materialize_for_scan(backend, book_entries, cache_dir)
            records_by_path = {
                book.path: exth.read_records(local_paths[book.path]) for book in book_entries
            }

        # One (book, already_covered) pair per MATCHED device book, decided up
        # front: a book already carrying its exact thumbnail name is never handed
        # to `thumbnails.install` at all (unless `--force`), but it still gets its
        # own `item` event below — silently dropping it would hide it from an agent
        # reading the event stream, the same reason `compress`/`convert` report an
        # `exists` skip rather than omitting the item entirely. `local_path`, when
        # this book was already materialised for MTP (above), is threaded through
        # so `thumbnails.install` never fetches the same book a second time.
        needle = args.match.lower() if args.match else None
        plan: list[tuple[thumbnails.Book, bool]] = []
        to_install: list[thumbnails.Book] = []
        for entry in book_entries:
            records = records_by_path[entry.path]
            book_id = exth.record_text(records, exth.TAG_UUID) or ""
            cdetype = exth.record_text(records, exth.TAG_CDETYPE) or thumbnails.DEFAULT_CDETYPE
            if needle is not None:
                title = exth.record_text(records, exth.TAG_TITLE) or ""
                author = exth.record_text(records, exth.TAG_AUTHOR) or ""
                haystacks = (entry.path, title, author)
                if not any(needle in haystack.lower() for haystack in haystacks):
                    continue
            book = thumbnails.Book(
                device_path=entry.path,
                book_id=book_id,
                cdetype=cdetype,
                local_path=local_paths.get(entry.path),
            )
            covered = (
                bool(book_id) and not args.force and _has_thumbnail(book_id, cdetype, all_paths)
            )
            plan.append((book, covered))
            if not covered:
                to_install.append(book)

        counts = {"total": 0, "done": 0, "skipped": 0, "failed": 0, "pending": 0}
        pending: list[str] = []

        if dry_run:
            # No cache/extraction resolution is attempted (that is real, if
            # read-only, work) — a book that is not already covered and DOES carry
            # an id is reported `pending`: an agent knows it WOULD be attempted, not
            # what its outcome would be.
            for item_id, (book, covered) in enumerate(plan, start=1):
                if covered:
                    item_status, reason, warnings = "skipped", "exists", []
                elif not book.book_id:
                    item_status, reason, warnings = "skipped", "no_cover", ["book_id_missing"]
                else:
                    item_status, reason, warnings = "pending", None, []
                    pending.append(book.device_path)
                counts["total"] += 1
                counts[item_status] += 1
                reporter.item(
                    id=item_id,
                    status=item_status,
                    input=book.device_path,
                    outputs=[],
                    bytes_in=None,
                    reason=reason,
                    warnings=warnings,
                )
            return {
                "counts": counts,
                "failed": [],
                "pending": pending,
                "outputs": [],
                "run_file": None,
                # Same two keys the real run's `data` carries (`snapshot` explicitly
                # `None` here, never omitted) — an agent parsing either shape gets a
                # missing VALUE, never a missing KEY.
                "data": {"thumbnails": {}, "snapshot": None},
            }

        def on_install_progress(done: int, total: int) -> None:
            reporter.progress(
                stage="thumbnails",
                index=done,
                count=total,
                path=f"kindle:{key}",
                percent=(100.0 * done / total) if total else 100.0,
            )

        cover_cache_dir = root / ".cache"
        statuses = thumbnails.install(
            backend, to_install, cache_dir=cover_cache_dir, on_progress=on_install_progress
        )

        outputs: list[str] = []
        failed: list[dict] = []
        for item_id, (book, covered) in enumerate(plan, start=1):
            item_outputs: list[str] = []
            if covered:
                item_status, reason, warnings = "skipped", "exists", []
            else:
                status = statuses.get(book.book_id or book.device_path, "no_cover")
                if status == "installed":
                    item_status, reason, warnings = "done", None, []
                    thumb_path = (
                        f"{thumbnails.THUMBNAIL_DIR}"
                        f"{thumbnails.thumbnail_name(book.book_id, book.cdetype)}"
                    )
                    item_outputs = [thumb_path]
                    outputs.append(thumb_path)
                elif status == "rejected":
                    item_status = "skipped"
                    reason = "device_rejected"
                    warnings = ["device_rejected_thumbnail"]
                elif status == "failed":
                    item_status, reason, warnings = "failed", "engine_error", []
                    # `detail` is `None`, never absent — see `run_backup` above.
                    # `thumbnails.install` collapses every genuine device/host fault
                    # into one `"failed"` status, so there is nothing narrower to
                    # say here; the key is present so one parser reads both commands.
                    failed.append(
                        {
                            "id": item_id,
                            "input": book.device_path,
                            "reason": "engine_error",
                            "detail": None,
                        }
                    )
                else:  # "no_cover"
                    item_status, reason = "skipped", "no_cover"
                    warnings = ["book_id_missing"] if not book.book_id else []
            counts["total"] += 1
            counts[item_status] += 1
            reporter.item(
                id=item_id,
                status=item_status,
                input=book.device_path,
                outputs=item_outputs,
                bytes_in=None,
                reason=reason,
                warnings=warnings,
            )

        if outputs:
            if snap is None:
                # Unreachable in practice: `outputs` is only ever populated below
                # this point, which is only reached when `dry_run` is False, which
                # is exactly when the `if not dry_run:` block above ran
                # `_mandatory_backup` and returned early on failure — so a
                # successful arrival here always carries a real snapshot. Raised
                # explicitly (not a bare `assert`, which `python -O` strips)
                # because trusting that chain silently would turn a violated
                # invariant into a confusing `AttributeError` on `None.path`
                # instead of a clear error naming what actually broke.
                raise RuntimeError(
                    "internal error: a thumbnails run wrote to the device with no "
                    "protecting snapshot on record"
                )
            # An undo pointer for a run that actually changed a thumbnail (including
            # a `--force` run overwriting one already there) — the bytes are inside
            # the backup `snap` just took, but nothing recorded WHICH operation put
            # them there until now.
            backup_module.journal_append(
                root, key, {"op": "thumbnails", "paths": outputs, "snapshot": snap.path.name}
            )

        ok = not failed
        return {
            "ok": ok,
            "exit_code": EXIT_OK if ok else EXIT_FAILED,
            "counts": counts,
            "failed": failed,
            "pending": [],
            "outputs": outputs,
            "run_file": None,
            "data": {
                "thumbnails": statuses,
                "snapshot": _snapshot_summary(snap.path, fallback=snap) if snap else None,
            },
        }

    return _run(
        args,
        kindle_command="thumbnails",
        stages=stages,
        options=options,
        device_finder=device_finder or detect.find_device,
        backend_factory=backend_factory or default_backend_factory,
        body=body,
    )


# --- add -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _SourceBook:
    """One book on the HOST that a run was asked to put on the device. `language` is
    the verdict an `ebook build` batch already recorded for it (`--batch`), which
    outranks the book's own EXTH 524 tag but loses to an explicit `--lang`; it is
    `None` for a book named directly on the command line."""

    path: Path
    language: str | None = None

    def key(self) -> str:
        """How this source is identified in the journal: its absolute path, resolved,
        so the same file named once relatively and once absolutely is one source. A
        path that no longer exists still resolves (`strict=False`), which matters
        because a vanished source must still be recognisable in an old entry."""
        return str(self.path.resolve())


@dataclass
class _PlannedBook:
    """One matched source and everything decided about it as the run progresses.

    `status` starts as `"pending"` ("no verdict yet") and is settled by exactly one
    `fail()`/`skip()`/`succeed()` call. Nothing is reported until the verify stage has
    run, because a book is not `done` until the device has been asked to confirm that
    its bytes really landed (see `run_add`'s own docstring) — which is also why a book
    that reaches the end still `"pending"` would be a bug in this module rather than a
    real outcome, and is reported as `pending` rather than quietly recoloured.
    (`--dry-run` is the one place `pending` IS a real outcome: there it means "this is
    what a real run would copy".)
    """

    id: int
    source: _SourceBook
    device_path: str
    book_id: str
    cdetype: str
    title: str
    author: str
    language: str
    size: int
    status: str = "pending"
    reason: str | None = None
    # `"<term>: <human sentence>"`, the term from `DETAIL_TERMS`. It exists because
    # the closed registry has one `engine_error` covering five distinct causes, and an
    # agent has to be able to branch on which one without substring-matching English.
    # Built only through `fail()`/`skip()` below, never assigned directly, so the shape
    # cannot drift one call site at a time.
    detail: str | None = None
    thumbnail: str | None = None
    warnings: list[str] = field(default_factory=list)

    def fail(self, reason: str, term: str, message: str) -> None:
        self.status, self.reason, self.detail = "failed", reason, _detail(term, message)

    def skip(self, reason: str, term: str, message: str) -> None:
        self.status, self.reason, self.detail = "skipped", reason, _detail(term, message)

    def succeed(self) -> None:
        self.status, self.reason, self.detail = "done", None, None

    def as_dict(self) -> dict:
        return {
            "source": str(self.source.path),
            "device_path": self.device_path,
            "book_id": self.book_id or None,
            "title": self.title or None,
            "author": self.author or None,
            "language": self.language,
            "size": self.size,
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
            "thumbnail": self.thumbnail,
        }

    def provenance(self, digest: str) -> dict:
        """What the journal records about a book this run put on the device, so a
        LATER run can recognise it without a filename comparison — see
        `_previous_placements`. `size` is the SOURCE's size (what was sent), not what
        landed, which is exactly what makes a short write fail the conjunction on the
        next run and get copied again."""
        return {
            "device_path": self.device_path,
            "source": self.source.key(),
            "size": self.size,
            "book_id": self.book_id or None,
            "sha256": digest,
        }


def _detail(term: str, message: str) -> str:
    """`"<term>: <message>"`, with the term checked against the closed-ish vocabulary
    the same way `Reporter` checks a `reason` — a typo'd prefix is worse than no
    prefix, because an agent branching on it would silently stop matching."""
    if term not in DETAIL_TERMS:
        raise KeyError(f"unknown detail term: {term!r}")
    return f"{term}: {message}"


def _is_language_code(value: str | None) -> bool:
    """A plain two- or three-letter code that is not one of the ISO 639-2 placeholders
    (`und`/`mul`/`zxx`).

    The PLACEHOLDER LIST is shared with `normalize._tag_language`
    (`IGNORED_LANGUAGE_TAGS`) so a library and a device never disagree about what is
    not a language. The SHAPE rule is deliberately NOT shared: `normalize` requires
    exactly two characters, because it is choosing the `<dc:language>` value it will
    write back into a book, while this is choosing a folder name on a device and a
    three-letter code a user typed (or a book carries) is a perfectly good folder.
    """
    code = (value or "").strip().lower()
    return bool(_LANGUAGE_CODE.fullmatch(code)) and code not in IGNORED_LANGUAGE_TAGS


def _validated_language_override(value: str | None) -> str | None:
    """`--lang`, or None. Unlike a language read off a book — which silently falls back
    to `UNKNOWN_LANGUAGE` when it is not usable — a value the USER typed is rejected
    outright, because silently shelving their books somewhere else is worse than
    telling them the code was not understood."""
    if value is None:
        return None
    code = value.strip().lower()
    if not _is_language_code(code):
        raise UsageError(
            f"--lang {value!r} is not a usable two- or three-letter language code "
            "(e.g. en, pt, es) — that value would become a folder name on the device."
        )
    return code


def _language_for(*candidates: str | None) -> str:
    """The first usable candidate, else `UNKNOWN_LANGUAGE`. Callers pass them in
    precedence order: `--lang`, then the `--batch` run's own verdict, then the book's
    embedded EXTH 524 tag."""
    for value in candidates:
        code = (value or "").strip().lower()
        if _is_language_code(code):
            return code
    return UNKNOWN_LANGUAGE


def _device_path_for(name: str, language: str, *, max_path: int) -> str:
    """`documents/<language>/<FAT32-safe name>`, with the WHOLE path inside `max_path`.

    `backend.sanitize_device_name` strips the characters FAT32 rejects and caps the
    NAME component; capping the full path is deliberately left to whoever joins a
    directory onto it (Ruling R3), which is here. The directory is fixed and short, so
    the remaining budget goes entirely to the name — shortened by
    `core.paths.truncate_name`, which preserves the extension and inserts a content
    hash rather than blindly slicing (two long names that share a prefix must not
    collapse onto one device path).

    A prefix long enough to leave less than `_MIN_NAME_BUDGET` raises instead of
    quietly overrunning the limit this function's whole contract is about. Unreachable
    from `run_add`, whose language is two or three letters or `UNKNOWN_LANGUAGE` — but
    this helper is exactly the kind of thing Task 8's `sync` will reuse with a
    different directory, and a contract that fails loudly is the point of stating one.
    """
    directory = f"{DOCUMENTS_DIR}/{language}/"
    budget = max_path - len(directory)
    if budget < _MIN_NAME_BUDGET:
        raise ValueError(
            f"{directory!r} leaves {budget} characters of a {max_path}-character device "
            f"path budget, under the {_MIN_NAME_BUDGET} a name needs; shorten the "
            "directory rather than overrunning the device's own path limit"
        )
    return f"{directory}{sanitize_device_name(name, max_path=budget)}"


def _sources_from_paths(paths: list[Path]) -> list[_SourceBook]:
    """Books named on the command line. A folder is scanned recursively (the same
    default `ebook build` uses for its own sources); a file is taken as named, and one
    whose extension no Kindle reads is a usage error rather than a silently skipped
    item — the user pointed at it explicitly, so a mistake deserves saying so."""
    found: list[_SourceBook] = []
    seen: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            candidates = sorted(
                child
                for child in path.rglob("*")
                if child.is_file() and child.suffix.lower() in ADDABLE_SUFFIXES
            )
        elif path.is_file():
            if path.suffix.lower() not in ADDABLE_SUFFIXES:
                raise UsageError(
                    f"{path} is not a format a Kindle reads "
                    f"({', '.join(sorted(ADDABLE_SUFFIXES))}); convert it first with "
                    "`media-tools convert --to azw3`."
                )
            candidates = [path]
        else:
            raise UsageError(f"no such file or directory: {path}")
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue  # the same file named twice, or reached through two folders
            seen.add(resolved)
            found.append(_SourceBook(path=candidate))
    return found


def _sources_from_batch(root: Path, name: str) -> list[_SourceBook]:
    """Every book an `ebook build` run placed, with the language it resolved for each.

    Filtered by `ADDABLE_SUFFIXES`, exactly as a FOLDER given on the command line is:
    a batch is a scan result, not a file the user pointed at, so an output in some
    other format is skipped rather than turned into a usage error. This filter is also
    load-bearing for `_device_path_for`'s budget arithmetic, which assumes a short,
    known extension.

    An item whose recorded output has since been deleted is KEPT rather than filtered
    out here, so it is reported as its own `source_missing` item instead of quietly
    shrinking the plan — the same reason `ebook build` reports that case rather than
    dropping it.
    """
    try:
        batch = sanitize_batch(name)
    except BatchNameError as error:
        raise UsageError(str(error)) from error

    sources: list[_SourceBook] = []
    seen: set[str] = set()
    for item in _read_library_items(root, batch, flag="--batch"):
        if not isinstance(item, dict) or item.get("status") != "done":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        output = data.get("output")
        if not isinstance(output, str) or not output or output in seen:
            continue
        if Path(output).suffix.lower() not in ADDABLE_SUFFIXES:
            continue
        seen.add(output)
        language = data.get("language")
        sources.append(
            _SourceBook(
                path=Path(output),
                language=language if isinstance(language, str) else None,
            )
        )
    return sources


def _resolve_add_sources(args, root: Path) -> list[_SourceBook]:
    """The books this run was asked to add, resolved BEFORE `start` — like every other
    task's upfront argument validation — so a bad `--batch`/`--lang`/path is a plain
    usage error (exit 2) rather than a device-shaped failure after a backup already
    ran."""
    if args.batch and args.books:
        raise UsageError(
            "pass books or --batch, not both: positional BOOKs are host paths, "
            "--batch NAME takes every book an `ebook build` run placed."
        )
    if not args.batch and not args.books:
        raise UsageError(
            "nothing to add: name one or more books, or pass --batch NAME to take "
            "them from an `ebook build` batch."
        )
    sources = (
        _sources_from_batch(root, args.batch) if args.batch else _sources_from_paths(args.books)
    )
    if not sources:
        where = f"batch {args.batch!r}" if args.batch else "the paths given"
        raise UsageError(f"no books found in {where}", code="no_input_matched")
    return sources


# --- what is already on the device, cheaply -------------------------------------------


def _book_id_key(book: DeviceFile) -> str:
    return f"{book.size}|{book.mtime}"


def _device_book_ids(
    root: Path, device: Device, backend: DeviceBackend, books: list[DeviceFile], key: str
) -> dict[str, str]:
    """`{device path: its EXTH 113 id}` for every book on the device, through a
    PERSISTENT index so `add` never re-reads a library it has already read.

    `exth.read_records` needs a book's header but has to read the whole file to reach
    it, so collecting these ids the obvious way costs a full pass over the library off
    USB — and over MTP a full fetch of it into the header cache — every single time one
    book is added. Scanning a library is `scan`'s job. The index
    (`_BOOK_ID_INDEX_NAME`, beside the MTP header cache under
    `_kindle/<serial>/.cache/`) is keyed by device path + size + mtime, exactly like
    that cache, so an edited or replaced book is re-read and an unchanged one never is;
    a path that has left the device drops out on the next write.

    Best-effort, like every other cache in that directory: an unreadable or unwritable
    index costs one re-read next time, never a failed `add`, and never gets reported as
    if the DEVICE were the problem.
    """
    index_path = backup_module.backup_root(root, key) / ".cache" / _BOOK_ID_INDEX_NAME
    index = _read_book_id_index(index_path)

    ids: dict[str, str] = {}
    unknown: list[DeviceFile] = []
    for book in books:
        recorded = index.get(book.path)
        if recorded is not None and recorded[0] == _book_id_key(book):
            ids[book.path] = recorded[1]
        else:
            unknown.append(book)

    if unknown:
        if device.mode == "mass_storage":
            records = {book.path: exth.read_records(device.mount / book.path) for book in unknown}
        else:
            cache_dir = backup_module.backup_root(root, key) / ".cache" / "headers"
            local_paths = _materialize_for_scan(backend, unknown, cache_dir)
            records = {book.path: exth.read_records(local_paths[book.path]) for book in unknown}
        for book in unknown:
            ids[book.path] = exth.record_text(records[book.path], exth.TAG_UUID) or ""

    if unknown or set(index) != {book.path for book in books}:
        _write_book_id_index(
            index_path, {book.path: (_book_id_key(book), ids[book.path]) for book in books}
        )
    return ids


def _read_book_id_index(index_path: Path) -> dict[str, tuple[str, str]]:
    """`{device path: (size|mtime key, book id)}`. Unlike `_read_cache_index` next
    door, nothing read back from here ever drives a filesystem operation — it only
    decides whether a book's header is re-read — so the validation is plain type
    checking rather than `_is_safe_cache_key`'s traversal guard."""
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    found: dict[str, tuple[str, str]] = {}
    for path, entry in data.items():
        if not isinstance(path, str) or not isinstance(entry, dict):
            continue
        entry_key, book_id = entry.get("key"), entry.get("book_id")
        if isinstance(entry_key, str) and isinstance(book_id, str):
            found[path] = (entry_key, book_id)
    return found


def _write_book_id_index(index_path: Path, index: dict[str, tuple[str, str]]) -> None:
    """Best-effort, for the same reason `_write_cache_index` is: a torn or refused
    write only means the next run re-reads some headers."""
    payload = {
        path: {"key": entry_key, "book_id": book_id} for path, (entry_key, book_id) in index.items()
    }
    with contextlib.suppress(OSError):
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _previous_placements(root: Path, key: str) -> dict[str, list[tuple[str, int]]]:
    """`{resolved source path: [(device path, size it was sent at), ...]}`, read back
    out of this device's own journal.

    This is what lets a re-run recognise a source that carries NO EXTH 113 id — an
    `.epub`, a `.pdf`, a MOBI nobody wrote an id into. It is PROVENANCE, not filename
    matching: it answers "did this tool put this exact file here, and is it still
    there at the size it was sent", which is a different question from "do these two
    files have similar names". It can only ever recognise books this tool placed; a
    book sideloaded by Calibre or by hand is invisible to it, which is a real limit
    rather than a regression.
    """
    placements: dict[str, list[tuple[str, int]]] = {}
    for entry in backup_module.journal_read(root, key):
        if entry.get("op") != "add":
            continue
        for record in entry.get("books") or []:
            if not isinstance(record, dict):
                continue
            source, device_path, size = (
                record.get("source"),
                record.get("device_path"),
                record.get("size"),
            )
            if not isinstance(source, str) or not isinstance(device_path, str):
                continue
            if isinstance(size, bool) or not isinstance(size, int):
                continue
            placements.setdefault(source, []).append((device_path, size))
    return placements


def _placed_by_us(
    placements: dict[str, list[tuple[str, int]]], source: _SourceBook, sizes: dict[str, int]
) -> str | None:
    """The device path this tool already put `source` at, if the device still holds it
    AT THE RECORDED SIZE — otherwise None.

    The CONJUNCTION is the whole point. The journal alone is a memory of what was
    done, and a user who has since deleted the book from the device would never get it
    back; the listing alone is a filename comparison, which this subsystem does not
    do. Together they answer a question neither can: is the file this tool placed
    still there, unchanged. A book whose write was short (journalled at the size that
    was SENT, present on the device at fewer bytes) fails this check and is copied
    again, which is exactly right — see `_our_paths_for`, which is what stops that
    retry from then being refused as a collision with its own failed attempt.
    """
    for device_path, size in placements.get(source.key(), ()):
        if sizes.get(device_path) == size:
            return device_path
    return None


def _our_paths_for(placements: dict[str, list[tuple[str, int]]], source: _SourceBook) -> set[str]:
    """Every device path (casefolded) the journal says this tool put THIS source at,
    whatever is there now.

    A path in this set is not a collision when the same source is offered again: it
    holds this tool's own earlier attempt at this exact file, so re-sending it is the
    retry the verify stage exists to make possible, not a clobber of somebody else's
    book. Without this, a short write could never be fixed by re-running `add` — the
    size mismatch would correctly refuse to call it "already placed", and the occupied
    path would then refuse to replace it, leaving the user with a truncated book and no
    way forward but deleting it by hand.
    """
    return {device_path.casefold() for device_path, _size in placements.get(source.key(), ())}


# --- the command ------------------------------------------------------------------------


def _journal_add(root: Path, key: str, records: list[dict], snapshot_name: str) -> str | None:
    """Record everything this run put on the device, or nothing if it put nothing.

    `paths` is what Task 8's `restore --op ID` removes (books AND their thumbnails);
    `books` is the per-book provenance `_previous_placements` reads back, which is what
    lets a later run recognise an id-less source without ever comparing filenames.
    """
    if not records:
        return None
    paths = [record["device_path"] for record in records]
    paths += [record["thumbnail"] for record in records if record.get("thumbnail")]
    return backup_module.journal_append(
        root,
        key,
        {
            "op": "add",
            "paths": paths,
            "books": [{k: v for k, v in record.items() if k != "thumbnail"} for record in records],
            "snapshot": snapshot_name,
        },
    )


def _source_digest(path: Path) -> str:
    """The source's sha256 for the journal, or `""` when it cannot be read.

    Best-effort deliberately: this runs AFTER the bytes are already on the device, so
    letting a failed hash escape would cost the provenance record for a book that is
    demonstrably there — strictly worse than recording the placement without a digest.
    Nothing reads this field back today (`_previous_placements` matches on path and
    size); it is recorded because the copy has just read the bytes anyway, and a later
    integrity check would otherwise have no baseline.
    """
    with contextlib.suppress(OSError):
        return backup_module.sha256_of(path)
    return ""


def run_add(
    args,
    *,
    device_finder: Callable[[], Device] | None = None,
    backend_factory: Callable[..., DeviceBackend] | None = None,
) -> int:
    """Put books on the Kindle, in this order, every time: resolve the device, take
    the mandatory backup, plan, check free space, copy, install thumbnails, verify,
    journal. `--dry-run` stops after the plan, takes NO backup and writes nothing to
    the DEVICE — it still fills the host-side caches its read-only planning needs (the
    EXTH id index, and over MTP the per-book header cache), exactly as
    `thumbnails --dry-run` does.

    **The backup is a PRECONDITION, not a step.** A failure there aborts before a
    single byte is written and keeps exit 3 with `_mandatory_backup`'s all-zero counts
    (Ruling R27) — deliberately NOT the exit 1 / `counts.failed: 1` the `backup`
    command itself reports, because there the snapshot IS the work being asked for
    while here nothing the user actually asked for was ever attempted. The two are not
    in conflict, and neither should be "fixed" to match the other.

    **Identity, in two layers, neither of them a filename.** A book carrying an EXTH
    113 id is `skipped`/`exists` when the device already holds that id, wherever and
    under whatever name (`_device_book_ids`, through a persistent index so `add` does
    not re-read the library every run). A book with NO id — an `.epub`, a `.pdf`, a
    MOBI nobody wrote one into — is recognised by PROVENANCE instead: this device's own
    journal says this tool put this exact source at path P, AND the device still holds
    P at the size it was sent (`_placed_by_us`). The conjunction is what makes that
    safe; either half alone would be a memory or a name comparison.

    **Nothing is `done` until the device confirms it.** After the copy, the `verify`
    stage lists `documents/` once and compares each written file's size against the
    source's. A write the backend accepted that left nothing, or left the wrong number
    of bytes (the real MTP failure mode — there is no rename primitive there, per
    Ruling R12), is a `failed` item, not a done one. The short file is left where it
    is rather than deleted: the journal records it, so Task 8's `restore --op` can undo
    it as part of the same operation, and this command never removes anything from a
    device by itself.

    **The journal is written even when the run does not finish.** Everything from the
    first `write` to the journal call runs inside one guard: a Ctrl+C (which
    `MassStorageBackend.write` re-raises after removing its own temp file) or any other
    escape would otherwise leave books on the device with no record of how they got
    there, while `_run` reported all-zero counts and `outputs: []`. On the way out the
    journal is appended for whatever WAS written and the original exception continues
    on its own path, so the pinned ordering still holds on the happy path.

    **Per-book isolation otherwise.** A device that runs out of space fails only the
    books that no longer fit (keeping what already landed), a refused write fails that
    book alone, a source that vanished is `source_missing`, and two books that would
    land on one device path — or one landing where a different book already sits,
    compared case-insensitively as FAT32 compares — are `output_collision` rather than
    a silent overwrite.

    Thumbnails ride along per book (`thumbnails.install`, reusing the source file we
    already have locally rather than fetching the book back off the device). A
    thumbnail that could not be installed never fails the BOOK — the book is on the
    device either way — so its outcome is reported in `result.data.thumbnails`, with a
    `device_rejected_thumbnail` warning on the item when the device accepted the write
    and silently discarded it (Colorsoft and newer, by design).

    Every failure carries a `detail` of the form `"<term>: <sentence>"`, the term from
    `DETAIL_TERMS`, on the `item` event as well as in `result`. The registry has one
    `engine_error` for five distinct causes and this task adds no codes to it, so the
    term is what an agent branches on instead of the English half.
    """
    dry_run = bool(args.dry_run)
    root = output_root(args.output_dir)
    language_override = _validated_language_override(args.lang)
    sources = _resolve_add_sources(args, root)

    stages = (
        ["detect", "plan"]
        if dry_run
        else ["detect", "backup", "plan", "copy", "thumbnails", "verify"]
    )
    options = {
        "kindle_command": "add",
        "batch": args.batch,
        "lang": language_override,
        "match": args.match,
        "dry_run": dry_run,
    }

    def body(reporter: Reporter, root: Path, device: Device, backend: DeviceBackend) -> dict:
        key = backup_module.device_key(device)
        snap: backup_module.Snapshot | None = None

        if not dry_run:
            reporter.stage(stage="backup", index=2, count=len(stages))
            failure, snap = _mandatory_backup(reporter, root, backend, key)
            if failure is not None:
                return failure
            if snap is None:  # pragma: no cover - one or the other, never neither
                # Raised explicitly (not a bare `assert`, which `python -O` strips):
                # every write below depends on a real snapshot, and a violated
                # invariant should name itself rather than surface as `None.path`.
                raise RuntimeError(
                    "internal error: an add reached its write phase with no protecting "
                    "snapshot on record"
                )

        # --- plan ---------------------------------------------------------------
        reporter.stage(stage="plan", index=len(stages) if dry_run else 3, count=len(stages))
        all_entries = backend.list_files()
        device_sizes = {entry.path: entry.size for entry in all_entries}
        occupied = {path.casefold() for path in device_sizes}
        device_books = [
            entry
            for entry in all_entries
            if PurePosixPath(entry.path).suffix.lower() in BOOK_SUFFIXES
        ]
        device_ids = set(_device_book_ids(root, device, backend, device_books, key).values()) - {""}
        placements = _previous_placements(root, key)

        max_path = MAX_DEVICE_PATH.get(device.mode, MAX_DEVICE_PATH["mtp"])
        needle = args.match.lower() if args.match else None

        planned: list[_PlannedBook] = []
        queued: list[_PlannedBook] = []
        claimed: dict[str, str] = {}  # casefolded device path -> the source that took it
        for source in sources:
            records = exth.read_records(source.path)
            book_id = exth.record_text(records, exth.TAG_UUID) or ""
            title = exth.record_text(records, exth.TAG_TITLE) or ""
            author = exth.record_text(records, exth.TAG_AUTHOR) or ""
            cdetype = exth.record_text(records, exth.TAG_CDETYPE) or thumbnails.DEFAULT_CDETYPE
            language = _language_for(
                language_override, source.language, exth.record_text(records, exth.TAG_LANGUAGE)
            )
            device_path = _device_path_for(source.path.name, language, max_path=max_path)
            if needle is not None and not any(
                needle in haystack.lower() for haystack in (device_path, title, author)
            ):
                # The exact `--match` rule `thumbnails` established (Ruling R36): the
                # device path OR the book's own EXTH title OR its author, any hit
                # counting. A non-matching book gets no item at all — it was never
                # part of what this run was asked to do.
                continue

            book = _PlannedBook(
                id=len(planned) + 1,
                source=source,
                device_path=device_path,
                book_id=book_id,
                cdetype=cdetype,
                title=title,
                author=author,
                language=language,
                size=source.path.stat().st_size if source.path.is_file() else 0,
            )
            planned.append(book)
            if source.path.is_file() and not book_id:
                # Only for a book we could actually READ that carried no id — a source
                # no longer on the host has no records to be missing one, and saying
                # otherwise would blame the wrong thing.
                book.warnings.append("book_id_missing")

            placed_at = None if book_id else _placed_by_us(placements, source, device_sizes)
            ours = set() if book_id else _our_paths_for(placements, source)
            if not source.path.is_file():
                book.fail(
                    "source_missing",
                    DETAIL_SOURCE_MISSING,
                    f"{source.path} is no longer on the host",
                )
            elif book_id and book_id in device_ids:
                book.skip(
                    "exists", DETAIL_EXISTS, "already on the device (matched by its EXTH 113 id)"
                )
            elif placed_at is not None:
                book.skip(
                    "exists",
                    DETAIL_EXISTS,
                    f"this tool already put this file at {placed_at} and the device still "
                    "holds it at that size (it carries no EXTH 113 id to match on)",
                )
            elif device_path.casefold() in claimed:
                book.fail(
                    "output_collision",
                    DETAIL_OUTPUT_COLLISION,
                    f"{claimed[device_path.casefold()]} in this same run already resolves "
                    f"to {device_path}",
                )
            elif device_path.casefold() in occupied and device_path.casefold() not in ours:
                book.fail(
                    "output_collision",
                    DETAIL_OUTPUT_COLLISION,
                    f"{device_path} is already occupied on the device by a file that is "
                    "not this book; refusing to overwrite it",
                )
            else:
                claimed[device_path.casefold()] = str(source.path)
                queued.append(book)

        # Free space is checked against the RUNNING total, not once against the sum:
        # a device with room for some of the books should place those and fail only
        # the ones that genuinely no longer fit. `free_space()` RAISES rather than
        # answering 0 when it cannot tell (both backends), so "0 left" here always
        # means a genuinely full device.
        free_space = backend.free_space()
        remaining = free_space
        to_copy: list[_PlannedBook] = []
        for book in queued:
            if book.size > remaining:
                book.fail(
                    "engine_error",
                    DETAIL_OUT_OF_SPACE,
                    f"not enough free space on the device: {format_size(book.size)} needed, "
                    f"{format_size(remaining)} left",
                )
                continue
            remaining -= book.size
            to_copy.append(book)

        if dry_run:
            # Whatever survived planning WOULD be copied; `pending` is the honest
            # status for that, the same one `thumbnails --dry-run` uses for a book it
            # would attempt but cannot predict the outcome of. Everything else already
            # has a real verdict, because every check above is read-only.
            return _add_result(
                reporter,
                planned,
                thumbnail_statuses={},
                snapshot=None,
                operation_id=None,
                free_space=free_space,
                queued=queued,
            )

        written: list[dict] = []
        try:
            # --- copy -----------------------------------------------------------
            reporter.stage(stage="copy", index=4, count=len(stages))
            copied: list[_PlannedBook] = []
            for index, book in enumerate(to_copy, start=1):
                try:
                    backend.write(book.source.path, book.device_path)
                except (RuntimeError, OSError) as error:
                    # Per book, never fatal to the batch: a full disk, a yanked cable,
                    # a `DeviceWriteProtected`, an MTP `CalibreError` — whatever it
                    # was, the books after this one still deserve their turn. A
                    # `KeyboardInterrupt` is NOT caught here: it is not one book's
                    # problem, and the outer guard journals what already landed.
                    book.fail(
                        "engine_error",
                        DETAIL_WRITE_REFUSED,
                        f"the device refused the write: {error}",
                    )
                    continue
                written.append(book.provenance(_source_digest(book.source.path)))
                copied.append(book)
                reporter.progress(
                    stage="copy",
                    index=index,
                    count=len(to_copy),
                    path=book.device_path,
                    percent=100.0 * index / len(to_copy),
                )

            # --- thumbnails -------------------------------------------------------
            reporter.stage(stage="thumbnails", index=5, count=len(stages))

            def on_thumbnail_progress(done: int, total: int) -> None:
                reporter.progress(
                    stage="thumbnails",
                    index=done,
                    count=total,
                    path=f"kindle:{key}",
                    percent=(100.0 * done / total) if total else 100.0,
                )

            thumbnail_statuses = thumbnails.install(
                backend,
                [
                    thumbnails.Book(
                        device_path=book.device_path,
                        book_id=book.book_id,
                        cdetype=book.cdetype,
                        # The copy we just sent IS the book — never fetch it back off
                        # the device to read its embedded cover (one MTP round trip per
                        # book instead of two, and identical bytes either way).
                        local_path=book.source.path,
                    )
                    for book in copied
                ],
                cache_dir=root / ".cache",
                on_progress=on_thumbnail_progress,
            )
            for record, book in zip(written, copied, strict=True):
                status = thumbnail_statuses.get(book.book_id or book.device_path, "no_cover")
                if status == "installed":
                    book.thumbnail = (
                        f"{thumbnails.THUMBNAIL_DIR}"
                        f"{thumbnails.thumbnail_name(book.book_id, book.cdetype)}"
                    )
                    record["thumbnail"] = book.thumbnail
                elif status == "rejected":
                    book.warnings.append("device_rejected_thumbnail")
                # "no_cover"/"failed" carry no registered warning code of their own;
                # the book is on the device regardless, so the outcome is reported in
                # `result.data.thumbnails` rather than invented into the registry.

            # --- verify -----------------------------------------------------------
            reporter.stage(stage="verify", index=6, count=len(stages))
            landed: dict[str, int] | None = None
            verify_error: str | None = None
            try:
                landed = {entry.path: entry.size for entry in backend.list_files(DOCUMENTS_DIR)}
            except (RuntimeError, OSError) as error:
                # Caught here rather than left to `_run`: every book already has an
                # outcome, and letting this escape would replace them all with
                # `_fail`'s all-zero "nothing was attempted" result, which would be a
                # lie about a run that demonstrably wrote to the device.
                verify_error = str(error)
            for book in copied:
                if landed is None:
                    book.fail(
                        "engine_error",
                        DETAIL_VERIFY_FAILED,
                        f"the device could not be listed to confirm the write: {verify_error}",
                    )
                    continue
                size = landed.get(book.device_path)
                if size is None:
                    book.fail(
                        "engine_error",
                        DETAIL_VERIFY_FAILED,
                        "the device accepted the write but the file is not on it afterwards",
                    )
                elif size != book.size:
                    book.fail(
                        "engine_error",
                        DETAIL_SHORT_WRITE,
                        f"the file on the device is {format_size(size)}, not the "
                        f"{format_size(book.size)} that was sent",
                    )
                else:
                    book.succeed()
        except BaseException:
            # `BaseException`, because the realistic case is `KeyboardInterrupt`:
            # `MassStorageBackend.write` re-raises it after removing its own temp file,
            # and without this the books already on the device would have no record of
            # how they got there while `_run` reported all-zero counts. The journal is
            # best-effort HERE only — a second failure while writing it must not
            # replace the exception the user actually needs to see (and, for a
            # `KeyboardInterrupt`, would cost `_run` its exit-130 mapping too).
            with contextlib.suppress(Exception):
                _journal_add(root, key, written, snap.path.name)
            raise

        # --- journal --------------------------------------------------------------
        # Last on the happy path, exactly as the order of operations pins it. A
        # failure writing it DOES escape here (unlike in the guard above): there is no
        # other exception competing for the user's attention, and a run that wrote to
        # the device without recording it is not a success.
        operation_id = _journal_add(root, key, written, snap.path.name)

        return _add_result(
            reporter,
            planned,
            thumbnail_statuses=thumbnail_statuses,
            snapshot=snap,
            operation_id=operation_id,
            free_space=free_space,
            queued=queued,
        )

    return _run(
        args,
        kindle_command="add",
        stages=stages,
        items=len(sources),
        options=options,
        device_finder=device_finder or detect.find_device,
        backend_factory=backend_factory or default_backend_factory,
        body=body,
    )


def _add_result(
    reporter: Reporter,
    planned: list[_PlannedBook],
    *,
    thumbnail_statuses: dict[str, str],
    snapshot: backup_module.Snapshot | None,
    operation_id: str | None,
    free_space: int,
    queued: list[_PlannedBook],
) -> dict:
    """Emit one `item` per planned book and build `body`'s return value.

    Shared by the real run and `--dry-run` so the two cannot report different shapes
    for the same book: an agent parsing either gets the same keys, with a missing
    VALUE (`snapshot: null`, `operation: null`, `thumbnails: {}`) where a dry run has
    nothing to say rather than a missing key.
    """
    counts = {"total": 0, "done": 0, "skipped": 0, "failed": 0, "pending": 0}
    failed: list[dict] = []
    outputs: list[str] = []
    pending = [str(book.source.path) for book in planned if book.status == "pending"]
    for book in planned:
        counts["total"] += 1
        counts[book.status] += 1
        item_outputs = [book.device_path] if book.status == "done" else []
        if book.status == "done":
            outputs.append(book.device_path)
        if book.status == "failed":
            failed.append(
                {
                    "id": book.id,
                    "input": str(book.source.path),
                    "reason": book.reason,
                    "detail": book.detail,
                }
            )
        reporter.item(
            id=book.id,
            status=book.status,
            input=str(book.source.path),
            outputs=item_outputs,
            bytes_in=book.size or None,
            bytes_out=book.size if book.status == "done" else None,
            reason=book.reason,
            detail=book.detail,
            warnings=book.warnings,
        )

    ok = not failed
    return {
        "ok": ok,
        "exit_code": EXIT_OK if ok else EXIT_FAILED,
        "counts": counts,
        "failed": failed,
        "pending": pending,
        "outputs": outputs,
        "run_file": None,
        "data": {
            "books": [book.as_dict() for book in planned],
            "thumbnails": thumbnail_statuses,
            # The same shape `backup`/`status`/`thumbnails` report for a snapshot,
            # never a second hand-built dict (see `_snapshot_summary`).
            "snapshot": _snapshot_summary(snapshot.path, fallback=snapshot) if snapshot else None,
            # What Task 8's `restore --op ID` undoes; `None` when this run put nothing
            # on the device at all.
            "operation": operation_id,
            "free_space": free_space,
            "bytes_planned": sum(book.size for book in queued),
        },
    }
