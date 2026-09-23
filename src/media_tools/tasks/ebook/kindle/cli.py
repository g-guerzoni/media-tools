"""`media-tools ebook kindle
status|scan|backup|thumbnails|add|remove|sync|restore|eject`.

`status`/`scan`/`eject` never write anywhere; `backup` writes only to the host.
`thumbnails`, `add`, `sync` and `restore` write to the DEVICE, and `remove`/`sync
--delete-extras` DELETE from it — see their own docstrings (`run_thumbnails`,
`run_add`, `run_remove`, `run_sync`, `run_restore`) for what that means for the
mandatory pre-write backup and per-book failure handling. Do not read this module's
name or this docstring's history as a promise that nothing here writes: that was true
through Task 5 and is no longer true of anything below `run_backup`.

`eject` is the exception that proves the rule: it touches the device and takes NO
backup, because it writes nothing — a mass-storage eject flushes bytes the host
already owed the device and then unmounts it, and an MTP eject only closes the
session. See `run_eject`.

**Nothing is ever deleted without `--yes`.** For `remove` and `restore` that covers
the whole run: without `--yes` they report what they would take (or put back), write
nothing at all — not to the device, not a backup, not a journal entry — and exit.
`sync` is deliberately not the same, and the difference matters when reading this
file: its ADDING half needs no confirmation and runs regardless (backup, copy,
journal, exactly as `add` does), and `--delete-extras` without `--yes` only adds the
removals to the PLAN. With `--yes` the mandatory backup runs FIRST and the deletions
follow it, in one backend instance, never re-detected in between.

A removal takes the book, its `.sdr` folder and its thumbnail together, and refuses
outright for anything outside the area every backup covers (`backup.DEFAULT_SCOPE` —
what the snapshot does not hold, `restore` could not put back), for `audible/`, for
`system/` outside `thumbnails/`, and for a purchased `*.kfx` (`_protection_refusal`).
`restore` is the other side of that: it only ever writes files back out of a snapshot,
and never deletes anything itself.

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
(`remove`, `sync` and `restore`): there the backup is a precondition and nothing the user
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
    EXIT_USAGE,
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
    PROTECTED_DIRS,
    RESTRICTED_EXCEPTION,
    RESTRICTED_PARENT,
    DeviceBackend,
    DeviceFile,
    DeviceWritePathRejected,
    DeviceWriteProtected,
    sanitize_device_name,
    validate_writable_path,
)
from media_tools.tasks.ebook.kindle.detect import Device, DeviceBusy, DeviceNotFound
from media_tools.tasks.ebook.normalize import IGNORED_LANGUAGE_TAGS

NAME = "kindle"
HELP = "Report on, scan, back up, add to, remove from, sync, restore or eject a connected Kindle."

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
# `documents/<lang>/`") is easier to reason about — and for `restore --op` to
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

# Which `ebook build` item statuses describe a book that IS in the library. `done` is
# one this run converted; `skipped` is one it did not have to (`reason: "exists"` —
# already at its target, or renamed into place), which is the ordinary outcome for
# nearly every book of a second build. Both carry the same `data.book_id` and
# `data.output`; only reading the first is how `sync --delete-extras` would come to
# treat an unchanged library as extras.
LIBRARY_ITEM_STATUSES = frozenset({"done", "skipped"})

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
# `remove`/`sync`/`restore`'s own five, same rule: one registry `reason` covers
# several causes, and `detail`'s term is what an agent branches on.
DETAIL_PROTECTED = "protected"
DETAIL_NOT_A_BOOK = "not_a_book"
DETAIL_REMOVE_FAILED = "remove_failed"
DETAIL_CORRUPT = "corrupt"
DETAIL_NOT_IN_SNAPSHOT = "not_in_snapshot"
DETAIL_TERMS = frozenset(
    {
        DETAIL_EXISTS,
        DETAIL_SOURCE_MISSING,
        DETAIL_OUTPUT_COLLISION,
        DETAIL_OUT_OF_SPACE,
        DETAIL_WRITE_REFUSED,
        DETAIL_SHORT_WRITE,
        DETAIL_VERIFY_FAILED,
        DETAIL_PROTECTED,
        DETAIL_NOT_A_BOOK,
        DETAIL_REMOVE_FAILED,
        DETAIL_CORRUPT,
        DETAIL_NOT_IN_SNAPSHOT,
    }
)

# The one warning a removal can carry: something that should have gone with the book
# did not. Over MTP that is not a fault but a documented gap — Calibre 9.15 has no
# delete-by-name and its cached device tree omits `*.sdr` folders and everything under
# `system/`, so `remove` cannot reach either (`mtp.MtpPathNotInCachedTree`). The book
# itself is gone regardless, and reporting the sidecar as removed when it is still
# there would be a lie the user discovers the next time they open that book.
SIDECAR_NOT_REMOVED_WARNING = "sidecar_not_removed"


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

    remove_help = (
        "Delete books from the connected Kindle, each with its .sdr folder and its "
        "thumbnail. Plans only and changes NOTHING unless --yes is given."
    )
    remove_parser = subparsers.add_parser("remove", help=remove_help, description=remove_help)
    _add_kindle_flags(remove_parser)
    remove_parser.add_argument(
        "books",
        nargs="*",
        metavar="BOOK",
        help="Device paths, exactly as `scan` reports them (documents/en/Book.azw3).",
    )
    remove_parser.add_argument(
        "--match",
        metavar="TEXT",
        default=None,
        help="Remove every book whose device path OR own title/author contains TEXT "
        "(case-insensitive) — the same rule `thumbnails --match` uses.",
    )
    remove_parser.add_argument(
        "--asin",
        metavar="ID",
        default=None,
        help="Remove the book carrying this EXTH 113 id, wherever it sits and "
        "whatever it is called.",
    )
    remove_parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete. Without it nothing is written, listed or not: the run "
        "reports what it WOULD remove and exits.",
    )

    sync_help = (
        "Add what an `ebook build` batch has and the device lacks. Removes the "
        "device's extras only with --delete-extras AND --yes."
    )
    sync_parser = subparsers.add_parser("sync", help=sync_help, description=sync_help)
    _add_kindle_flags(sync_parser)
    sync_parser.add_argument(
        "--batch",
        metavar="NAME",
        default=None,
        help="The `ebook build` batch this device should mirror.",
    )
    sync_parser.add_argument(
        "--lang",
        metavar="XX",
        default=None,
        help="Put every added book under documents/XX/ instead of its own language.",
    )
    sync_parser.add_argument(
        "--match",
        metavar="TEXT",
        default=None,
        help="Only consider books whose device path OR own title/author contains TEXT "
        "(case-insensitive) — narrows both halves, the adds and the extras.",
    )
    sync_parser.add_argument(
        "--delete-extras",
        action="store_true",
        dest="delete_extras",
        help="Also plan the removal of device books the batch does not have. Nothing "
        "is deleted without --yes as well.",
    )
    sync_parser.add_argument(
        "--yes",
        action="store_true",
        help="Allow --delete-extras to actually delete. Adding books never needs it.",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the whole plan without taking a backup or writing anything.",
    )

    restore_help = (
        "Put a backup snapshot's files back on the device, or undo exactly one "
        "journalled operation. Never deletes anything."
    )
    restore_parser = subparsers.add_parser("restore", help=restore_help, description=restore_help)
    _add_kindle_flags(restore_parser)
    restore_parser.add_argument(
        "snapshot",
        nargs="?",
        default=None,
        metavar="SNAPSHOT",
        help="A snapshot directory name (or path) under this device's backups. "
        "Defaults to the newest complete snapshot.",
    )
    restore_parser.add_argument(
        "--op",
        metavar="ID",
        default=None,
        help="Restore only what one journalled operation touched, from the snapshot "
        "that protected it (`result.data.operation` of the run that made it).",
    )
    restore_parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually write. Without it the run reports what it would put back — "
        "hashes checked, exactly as a real run checks them — and writes nothing.",
    )
    restore_parser.add_argument(
        "--force",
        action="store_true",
        help="Allow a snapshot taken from a DIFFERENT Kindle to be written to this "
        "one. Refused without it.",
    )

    eject_help = (
        "Release the connected Kindle so the cable can come out. Writes nothing to "
        "the device, and therefore takes no backup."
    )
    eject_parser = subparsers.add_parser("eject", help=eject_help, description=eject_help)
    _add_kindle_flags(eject_parser)


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
    if args.kindle_command == "remove":
        return run_remove(args)
    if args.kindle_command == "sync":
        return run_sync(args)
    if args.kindle_command == "restore":
        return run_restore(args)
    if args.kindle_command == "eject":
        return run_eject(args)
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
# (`thumbnails`, `add`, `remove`, `sync` and `restore`) must NOT reuse this
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


def _announcer(reporter: Reporter, stages: list[str]) -> Callable[[str], None]:
    """`announce("plan")` emits that stage's own `stage` event with the index it
    actually has in THIS run's `stages` list. Commands here build that list
    conditionally (`--yes`/`--dry-run`/`--delete-extras` each add or drop a stage), and
    a hand-written index is exactly the kind of thing that silently stops matching the
    list announced in `start` when a later flag shifts it."""

    def announce(stage: str) -> None:
        reporter.stage(stage=stage, index=stages.index(stage) + 1, count=len(stages))

    return announce


def _body_failure(reporter: Reporter, *, code: str, message: str, exit_code: int) -> dict:
    """A `body`-shaped failure for something `body` itself diagnosed: a `--op` id that
    is not in the journal, a snapshot that cannot be read. All-zero counts, because
    nothing the user asked for was attempted — the same shape `_mandatory_backup`
    returns for a failed precondition, and deliberately not `_fail`, which would emit
    its own `result` and bypass `_run`'s single-result-per-run guarantee."""
    reporter.error(code=code, message=message)
    return {
        "ok": False,
        "exit_code": exit_code,
        "counts": {"total": 0, "done": 0, "skipped": 0, "failed": 0, "pending": 0},
        "failed": [],
        "pending": [],
        "outputs": [],
        "run_file": None,
        "data": {},
    }


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

        records_by_path, _ = _records_for(
            root, device, backend, books, backup_module.device_key(device)
        )

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


def _records_for(
    root: Path,
    device: Device,
    backend: DeviceBackend,
    books: list[DeviceFile],
    key: str,
) -> tuple[dict[str, dict[int, bytes]], dict[str, Path]]:
    """`({device path: its EXTH records}, {device path: its local copy})` for whatever
    subset of the device's books it is given.

    The one place this subsystem turns device books into EXTH records: off the mount
    directly for mass storage, and over MTP through the per-book header cache
    (`_materialize_for_scan`, one `read_many` batch), which is also why the local
    paths come back — `thumbnails` reuses them so a book is never fetched twice.
    `scan`, `thumbnails` and `remove`/`sync` all go through this rather than each
    repeating the mode check, which is exactly the kind of thing that drifts.
    """
    if device.mode == "mass_storage":
        return {book.path: exth.read_records_safe(device.mount / book.path) for book in books}, {}
    cache_dir = backup_module.backup_root(root, key) / ".cache" / "headers"
    local_paths = _materialize_for_scan(backend, books, cache_dir)
    return {
        book.path: exth.read_records_safe(local_paths[book.path]) for book in books
    }, local_paths


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
    """book id -> {title, author, language, output} for every surviving item in an
    `ebook build`/`ebook scan` batch's `run.json` — `done` or `skipped`, per
    `LIBRARY_ITEM_STATUSES`: a book the build did not have to reconvert is still a
    book the library has, and reading only `done` would make a second build's compare
    report call almost the whole device `device_only`."""
    index: dict[str, dict] = {}
    for item in _read_library_items(root, batch_name, flag="--compare"):
        if not isinstance(item, dict) or item.get("status") not in LIBRARY_ITEM_STATUSES:
            continue
        book_data = item.get("data") if isinstance(item.get("data"), dict) else {}
        book_id = book_data.get("book_id")
        # `isinstance`, not just truthiness: this value becomes a dict KEY compared
        # against ids read off the device, and a hand-edited `run.json` holding a
        # number or a list there would otherwise quietly enter the index. Its twin in
        # `_library_batch` checks the same way.
        if not isinstance(book_id, str) or not book_id:
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
    `run_backup`, `run_thumbnails`, `run_add`, `run_remove`, `run_sync` and
    `run_restore` all reach the device through this one function rather than through
    six copies of its callback wiring.

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

        records_by_path, local_paths = _records_for(root, device, backend, book_entries, key)

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

    # --- what `_report_rows` reads off every reported row ----------------------
    @property
    def report_input(self) -> str:
        return str(self.source.path)

    @property
    def report_outputs(self) -> list[str]:
        return [self.device_path] if self.status == "done" else []

    @property
    def report_bytes_in(self) -> int | None:
        return self.size or None

    @property
    def report_bytes_out(self) -> int | None:
        return self.size if self.status == "done" else None

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

    def provenance(self) -> dict:
        """What the journal records about a book this run put on the device, so a
        LATER run can recognise it without a filename comparison — see
        `_previous_placements`.

        `size` is the SOURCE's size (what was SENT), not what landed: that is exactly
        what makes a short write fail the conjunction on the next run and get copied
        again. `sha256` and `verified` are filled in AFTERWARDS — the record is put on
        the journal's list the instant the write returns, so that a Ctrl+C during the
        hash (a window proportional to file size) cannot leave a book on the device
        with no record of it. Until they are filled in, the record reads as "no
        digest, never confirmed", which is the honest reading of both: `verified`
        means this tool never CONFIRMED the write, not that it knows the write failed.
        """
        return {
            "device_path": self.device_path,
            "source": self.source.key(),
            "size": self.size,
            "book_id": self.book_id or None,
            "sha256": "",
            "verified": False,
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
    from `run_add`/`run_sync`, whose language is two or three letters or
    `UNKNOWN_LANGUAGE` — but a caller that one day joins a longer directory onto this
    would otherwise overrun the budget silently, and a contract that fails loudly is
    the point of stating one.
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


def _library_batch(root: Path, batch: str, *, flag: str) -> tuple[list[_SourceBook], set[str], int]:
    """`(every book an `ebook build` run placed, every book id it recorded, how many
    of its items describe neither)`, from ONE read of that batch's `run.json`.

    **A book in the library is one whose item is `done` OR `skipped`, never `done`
    alone.** `ebook build` writes `skipped`/`exists` for every book it did not have to
    reconvert (`build.py`'s `reused` branch) — which, on the second build of the same
    batch, is nearly all of them — and those items carry exactly the same `data`, with
    the same `book_id` and the same `output`, as the ones it converted this run. Taking
    only `done` here would mean an ordinary re-build empties `ids`, so the device's
    copies of an unchanged library match nothing, and `sync --delete-extras --yes`
    deletes almost the whole thing. `LIBRARY_ITEM_STATUSES` is that rule, in one place.

    The SOURCES are filtered by `ADDABLE_SUFFIXES`, exactly as a FOLDER given on the
    command line is: a batch is a scan result, not a file the user pointed at, so an
    output in some other format is skipped rather than turned into a usage error. This
    filter is also load-bearing for `_device_path_for`'s budget arithmetic, which
    assumes a short, known extension. An item whose recorded output has since been
    deleted is KEPT rather than filtered out here, so it is reported as its own
    `source_missing` item instead of quietly shrinking the plan — the same reason
    `ebook build` reports that case rather than dropping it.

    The IDS are deliberately NOT filtered that way: they answer `sync --delete-extras`'
    only question — "does the library have this book at all" — and a library book
    whose output happens to be a format this tool would not ADD is still a book the
    library has, so deleting the device's copy of it would be wrong.

    The THIRD value counts every item that describes no book: one whose status is
    NEITHER of the two (`pending`, `failed`, anything a future version invents), AND
    one that carries a library status but no `data` at all, which is the same hole
    wearing a better status. A batch holding any of them is not a complete statement
    about the library, and `sync --delete-extras` refuses it rather than reading every
    book behind them as "the library does not have this".
    """
    sources: list[_SourceBook] = []
    ids: set[str] = set()
    seen: set[str] = set()
    unfinished = 0
    for item in _read_library_items(root, batch, flag=flag):
        if not isinstance(item, dict):
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        # An item with no `data` at all describes no book, whatever its status says —
        # it is exactly the incomplete entry the `--delete-extras` refusal is about,
        # and counting only the status would let it through as a library item that
        # happens to contribute nothing.
        if item.get("status") not in LIBRARY_ITEM_STATUSES or not data:
            unfinished += 1
            continue
        book_id = data.get("book_id")
        if isinstance(book_id, str) and book_id:
            ids.add(book_id)
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
    return sources, ids, unfinished


def _sources_from_batch(root: Path, name: str) -> list[_SourceBook]:
    """`_library_batch`'s sources alone, for `add --batch NAME`, which has no use for
    the library's ids or for how complete the batch is (it matches what is already on
    the DEVICE, and it never deletes anything)."""
    try:
        batch = sanitize_batch(name)
    except BatchNameError as error:
        raise UsageError(str(error)) from error
    return _library_batch(root, batch, flag="--batch")[0]


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
) -> tuple[dict[str, str], list[str]]:
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

    **A book whose read RAISED is never cached, and is counted.** Returning
    `(ids, paths that could not be read)` rather than folding a failure into `""` is
    load-bearing twice over. First, the index key is size + mtime and a device file
    does not change, so caching `""` for a thrown read would serve that answer on
    every future run — long after whatever caused it (a full host disk while
    materialising over MTP, a path the OS refuses to open) was gone. Second, a book
    that looks id-less looks ABSENT: `add` would then copy a source it already has,
    under a name the device copy need not share, so a systemic read failure would
    write the whole library a second time. The caller surfaces the count rather than
    leaving it to be inferred from duplicates appearing.
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

    unreadable: list[str] = []
    if unknown:
        if device.mode == "mass_storage":
            local_paths = {book.path: device.mount / book.path for book in unknown}
        else:
            cache_dir = backup_module.backup_root(root, key) / ".cache" / "headers"
            local_paths = _materialize_for_scan(backend, unknown, cache_dir)
        for book in unknown:
            book_id = _book_id_of(local_paths[book.path])
            if book_id is None:
                unreadable.append(book.path)
                ids[book.path] = ""  # unknown to THIS run; never written to the index
            else:
                ids[book.path] = book_id

    # A set, not the list: this is one membership test per book on the device, and a
    # list scan makes that quadratic over a real library.
    unreadable_paths = set(unreadable)
    cacheable = {
        book.path: (_book_id_key(book), ids[book.path])
        for book in books
        if book.path not in unreadable_paths
    }
    if unknown or set(index) != set(cacheable):
        _write_book_id_index(index_path, cacheable)
    return ids, unreadable


def _book_id_of(path: Path) -> str | None:
    """`""` for a book that parsed and carries no EXTH 113 id; `None` when reading it
    RAISED, which is a different thing and must not be cached as if it were the first
    (see `_device_book_ids`). The guard itself lives in `exth.read_records_or_none`,
    beside the function it guards, so that every EXTH read in this subsystem shares
    one — including `backup.py`'s, which imports `read_records` directly and therefore
    could never have been covered by a helper living here. That read is on the RESTORE
    path (`_companions_of`, reached only from `restore(only=...)`), not in `snapshot()`
    — the mandatory pre-write backup never reaches it."""
    records = exth.read_records_or_none(path)
    if records is None:
        return None
    return exth.record_text(records, exth.TAG_UUID) or ""


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


def _previous_placements(root: Path, key: str) -> dict[str, dict[str, dict]]:
    """`{resolved source path: {device path: latest placement record}}`, read back out
    of this device's own journal. Each record is `{device_path, size, sha256,
    verified}`.

    This is what lets a re-run recognise a source that carries NO EXTH 113 id — an
    `.epub`, a `.pdf`, a MOBI nobody wrote an id into. It is PROVENANCE, not filename
    matching: it answers "did this tool put these exact bytes here", which is a
    different question from "do these two files have similar names". It can only ever
    recognise books this tool placed; a book sideloaded by Calibre or by hand is
    invisible to it, which is a real limit rather than a regression.

    **Only the LATEST record for a given (source, device path) survives.** The journal
    is append-only and `journal_read` returns it oldest first, so a later entry
    overwrites an earlier one here. Accumulating them instead would make the
    overwrite waiver below permanent: once ANY unverified record existed for that
    pair, the successful re-run that followed would add a `verified: true` record
    without removing the old one, and the refusal at that path would stay waived
    forever — so a user replacing that book with their own copy months later would
    have it silently clobbered by a re-run of the same source. The newest record is
    the only one that describes what this tool last did.

    `verified` defaults to TRUE for a record that lacks the key. Every record this
    code writes carries it explicitly (including `False` on the interrupt path, where
    this tool demonstrably never got to confirm the write), so a record without one
    comes from an older version of this command — and `True` is the safe default,
    because it is `False` that waives a refusal to overwrite
    (`_provenance_verdict`).
    """
    placements: dict[str, dict[str, dict]] = {}
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
            digest = record.get("sha256")
            placements.setdefault(source, {})[device_path] = {
                "device_path": device_path,
                "size": size,
                "sha256": digest if isinstance(digest, str) else "",
                "verified": bool(record.get("verified", True)),
            }
    return placements


def _provenance_verdict(
    placements: dict[str, dict[str, dict]], source: _SourceBook, sizes: dict[str, int]
) -> tuple[str | None, set[str]]:
    """`(the path this source is already correctly at, the paths holding a placement of
    these exact bytes that this tool never CONFIRMED)` — both `None`/empty unless the
    journal can be verified against the source as it is right now.

    The source is hashed once (only when the journal has anything to say about it at
    all) and every record whose recorded `sha256` differs is discarded outright. That
    digest gate is what makes the journal a statement about BYTES rather than about a
    host path, and it closes a false skip that path-only matching allowed: a source
    edited from 500 to 900 bytes whose device copy still matches the OLD recorded size
    would otherwise be reported "already on the device", for a file whose contents had
    changed. A record with no usable digest (an old entry, or one whose hash could not
    be computed) proves nothing and is ignored, which errs towards copying again.

    **First value — the `exists` skip.** A record whose digest matches AND whose device
    path still holds exactly the recorded size: these bytes are on the device, so there
    is nothing to do.

    **Second value — the collision waiver, and the narrower test it needs.** A short
    or interrupted write leaves a file at the target path that is NOT the recorded
    size, so the skip above correctly declines — and then the occupied-path refusal
    would decline too, leaving the user a truncated book and no way forward but
    deleting it by hand. Waiving that refusal is right, but "the journal mentions this
    path" is not enough authorisation on its own: a user who deleted our copy and put
    their OWN file there would have it silently overwritten on the next run. So the
    waiver is restricted to records marked `verified: false`.

    Read that flag precisely: it means **this tool never CONFIRMED the write**, not
    that it knows the write failed. A run interrupted during the thumbnails stage, or
    one whose verify-stage listing failed, records a perfectly-landed book as
    unverified — and that is the honest reading, because the tool genuinely does not
    know. The waiver is defined against exactly that ignorance: a placement this tool
    never confirmed may be re-sent, while one it DID confirm describes a file that
    landed correctly, so anything different at that path now was changed by something
    other than this tool — precisely the case the refusal exists for.

    **The residual exposure is real and worth stating.** A run whose verify-stage
    listing failed records a book that landed PERFECTLY as unconfirmed, and nothing
    later revises that. If the user then replaces that file with their own, the next
    `add` of the same source overwrites THEIR file, not a redundant copy of ours. What
    stands behind it is the mandatory snapshot every write command takes: those bytes
    are inside the backup taken at the start of the overwriting run, and
    `restore`/`restore --op` puts them back. Closing it properly would need the DEVICE
    file hashed, which is a full read — and over MTP a full fetch — of every
    candidate: the cost this design deliberately does not pay.
    """
    records = placements.get(source.key())
    if not records:
        return None, set()
    digest = _source_digest(source.path)
    if not digest:
        return None, set()
    mine = [record for record in records.values() if record["sha256"] == digest]
    placed_at = next(
        (
            record["device_path"]
            for record in mine
            if sizes.get(record["device_path"]) == record["size"]
        ),
        None,
    )
    unconfirmed = {record["device_path"].casefold() for record in mine if not record["verified"]}
    return placed_at, unconfirmed


# --- the command ------------------------------------------------------------------------


def _journal_add(root: Path, key: str, records: list[dict], snapshot_name: str) -> str | None:
    """Record everything this run put on the device, or nothing if it put nothing.

    `paths` is every device path this run WROTE (books and their thumbnails), which
    is what `restore --op ID` selects — and, for an `add`, deliberately finds nothing
    for: the snapshot that protected this run was taken before those files existed, so
    it reports them as `not_in_snapshot` rather than deleting them (`restore` never
    deletes; taking an added book off again is `remove`'s job). `books` is the per-book
    provenance `_previous_placements` reads back, which is what lets a later run
    recognise an id-less source without ever comparing filenames.
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
    """The source's sha256, or `""` when it cannot be read.

    Two callers, and the suppressed `OSError` means something different — but equally
    safe — in each:

    - The copy loop, AFTER the bytes are already on the device. Letting a failed hash
      escape there would cost the provenance record for a book that is demonstrably
      on the device, which is strictly worse than recording the placement without a
      digest. A digestless record then authorises nothing (below).
    - `_provenance_verdict`, in the PLAN, where this is what the journal is checked
      against. `""` there means no journal record can be matched at all, so the book
      is neither skipped as already-present nor allowed to overwrite anything — it is
      simply copied again, or refused if its path is taken. Failing to hash the source
      therefore fails safe in both directions.
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
    MOBI nobody wrote one into — is recognised by PROVENANCE instead
    (`_provenance_verdict`): the journal says this tool put these exact BYTES (the
    source hashes to the digest recorded with the placement) at path P, and the device
    still holds P at the size that was sent. Every part of that is load-bearing — the
    journal alone is a memory, the listing alone is a name comparison, and without the
    digest an edited source would be reported as already there.

    **What the journal does and does not authorise.** It never redirects a write: the
    target path is recomputed from scratch every run, and the journal can only suppress
    a refusal at that already-computed path. The one thing it waives is the
    occupied-path refusal, and only for a placement recorded as UNVERIFIED — one this
    tool never CONFIRMED, which includes a run interrupted before the verify stage as
    well as a write that demonstrably failed it. Re-running `add` is therefore how a
    short or interrupted write gets fixed, while a file the user put at that path
    themselves is still refused. Only the LATEST record for a (source, path) pair
    counts, so a successful re-run revokes an earlier run's waiver.

    **Nothing is `done` until the device confirms it.** After the copy, the `verify`
    stage lists `documents/` once and compares each written file's size against the
    source's. A write the backend accepted that left nothing, or left the wrong number
    of bytes (the real MTP failure mode — there is no rename primitive there, per
    Ruling R12), is a `failed` item, not a done one. The short file is left where it
    is rather than deleted: this command never removes anything from a device by
    itself, and the journal records the short file so `ebook kindle remove` can take
    it off deliberately (re-running `add` also overwrites it, which is what the
    unverified-placement waiver exists for).

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
        announce = _announcer(reporter, stages)
        snap: backup_module.Snapshot | None = None

        if not dry_run:
            announce("backup")
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
        announce("plan")
        view = _survey_device(reporter, root, device, backend, key)
        planned, queued = _plan_add(
            view,
            sources,
            language_override=language_override,
            needle=args.match.lower() if args.match else None,
            max_path=MAX_DEVICE_PATH.get(device.mode, MAX_DEVICE_PATH["mtp"]),
            placements=_previous_placements(root, key),
        )
        free_space, to_copy = _fit_to_free_space(backend, queued)

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
                device_books_unreadable=len(view.unreadable),
            )

        thumbnail_statuses, operation_id = _copy_books(
            reporter, root, key, backend, to_copy, snap, announce
        )

        return _add_result(
            reporter,
            planned,
            thumbnail_statuses=thumbnail_statuses,
            snapshot=snap,
            operation_id=operation_id,
            free_space=free_space,
            queued=queued,
            device_books_unreadable=len(view.unreadable),
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


# --- add's own phases, shared with `sync` ------------------------------------------------


@dataclass(frozen=True)
class _DeviceView:
    """Everything a plan needs to know about what is already on the device, read in
    ONE listing: sizes by path, the casefolded set of occupied paths (FAT32 compares
    that way), the books among them, each book's EXTH 113 id, and the paths whose id
    could not be read at all."""

    entries: list[DeviceFile]
    sizes: dict[str, int]
    occupied: set[str]
    books: list[DeviceFile]
    ids_by_path: dict[str, str]
    unreadable: list[str]

    @property
    def paths(self) -> set[str]:
        return set(self.sizes)

    @property
    def ids(self) -> set[str]:
        return set(self.ids_by_path.values()) - {""}


def _survey_device(
    reporter: Reporter, root: Path, device: Device, backend: DeviceBackend, key: str
) -> _DeviceView:
    entries = backend.list_files()
    sizes = {entry.path: entry.size for entry in entries}
    books = [
        entry for entry in entries if PurePosixPath(entry.path).suffix.lower() in BOOK_SUFFIXES
    ]
    ids_by_path, unreadable = _device_book_ids(root, device, backend, books, key)
    if unreadable:
        # A book whose EXTH could not be READ looks ABSENT to the id check, so a
        # source it already holds reads as new and gets copied a second time —
        # under a name the device copy need not share, which is how a systemic
        # read failure quietly duplicates a whole library. (For `sync
        # --delete-extras` it cuts the other way: a book with no readable id can
        # never be PROVEN absent from the library, so it is never an extra.) Its own
        # code, NOT `book_id_missing`: that one means a book legitimately carries no
        # EXTH 113 (permanent, and cacheable as such), while this means the read
        # itself failed (transient, and specifically never cached — see
        # `_device_book_ids`). One code for both would re-conflate at the wire
        # exactly what that function separates in the logic, and a consumer
        # aggregating `book_id_missing` would be summing two populations.
        reporter.warning(
            code="book_id_unreadable",
            message=(
                f"{len(unreadable)} book(s) on the device could not be read for "
                "their EXTH 113 id and will look absent to this run (first: "
                f"{unreadable[0]})"
            ),
        )
    return _DeviceView(
        entries=entries,
        sizes=sizes,
        occupied={path.casefold() for path in sizes},
        books=books,
        ids_by_path=ids_by_path,
        unreadable=unreadable,
    )


def _plan_add(
    view: _DeviceView,
    sources: list[_SourceBook],
    *,
    language_override: str | None,
    needle: str | None,
    max_path: int,
    placements: dict[str, dict[str, dict]],
) -> tuple[list[_PlannedBook], list[_PlannedBook]]:
    """`(every matched source with a verdict, the ones that still want copying)`.

    Read-only: every check here is a comparison against `view` or the journal, which
    is what lets `--dry-run` report the same verdicts a real run would reach. Shared
    with `sync`, which has exactly the same question to answer about its batch.
    """
    planned: list[_PlannedBook] = []
    queued: list[_PlannedBook] = []
    claimed: dict[str, str] = {}  # casefolded device path -> the source that took it
    for source in sources:
        records = exth.read_records_safe(source.path)
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

        placed_at, unconfirmed = (
            (None, set()) if book_id else _provenance_verdict(placements, source, view.sizes)
        )
        if not source.path.is_file():
            book.fail(
                "source_missing",
                DETAIL_SOURCE_MISSING,
                f"{source.path} is no longer on the host",
            )
        elif book_id and book_id in view.ids:
            book.skip("exists", DETAIL_EXISTS, "already on the device (matched by its EXTH 113 id)")
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
        elif device_path.casefold() in view.occupied and device_path.casefold() not in unconfirmed:
            book.fail(
                "output_collision",
                DETAIL_OUTPUT_COLLISION,
                f"{device_path} is already occupied on the device by a file that is "
                "not this book; refusing to overwrite it",
            )
        else:
            claimed[device_path.casefold()] = str(source.path)
            queued.append(book)

    return planned, queued


def _fit_to_free_space(
    backend: DeviceBackend, queued: list[_PlannedBook]
) -> tuple[int, list[_PlannedBook]]:
    """`(free bytes on the device, the books that fit)`, failing the rest in
    place."""
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
    return free_space, to_copy


def _copy_books(
    reporter: Reporter,
    root: Path,
    key: str,
    backend: DeviceBackend,
    to_copy: list[_PlannedBook],
    snap: backup_module.Snapshot,
    announce: Callable[[str], None],
) -> tuple[dict[str, str], str | None]:
    """The three stages that actually write: copy, thumbnails, verify — then the
    journal. Returns `(per-book thumbnail statuses, the journalled operation id)`;
    every book's own verdict is settled on the `_PlannedBook` objects themselves.
    Shared with `sync`, which writes exactly the same way `add` does.
    """
    written: list[dict] = []
    try:
        # --- copy -----------------------------------------------------------
        announce("copy")
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
            # Appended BEFORE the hash, which reads the whole source again: a
            # Ctrl+C in that window would otherwise leave this book on the device
            # and absent from the journal, which is the very hole the outer guard
            # exists to close.
            record = book.provenance()
            written.append(record)
            copied.append(book)
            record["sha256"] = _source_digest(book.source.path)
            reporter.progress(
                stage="copy",
                index=index,
                count=len(to_copy),
                path=book.device_path,
                percent=100.0 * index / len(to_copy),
            )

        # --- thumbnails -------------------------------------------------------
        announce("thumbnails")

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
        announce("verify")
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
        # `record["verified"]` is set inside this loop, per book, the moment that
        # book's own verdict is known — not in a second pass afterwards. It is
        # what the NEXT run consults before overwriting anything
        # (`_provenance_verdict`), and every statement between the write and the
        # flag is a window in which an interrupt leaves a book that landed
        # perfectly recorded as unconfirmed. The window cannot be closed (nothing
        # can confirm a write before the device has been asked), but it should be
        # as short as the code allows.
        for record, book in zip(written, copied, strict=True):
            if landed is None:
                book.fail(
                    "engine_error",
                    DETAIL_VERIFY_FAILED,
                    f"the device could not be listed to confirm the write: {verify_error}",
                )
                record["verified"] = False
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
            record["verified"] = book.status == "done"
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
    return thumbnail_statuses, operation_id


def _report_rows(reporter: Reporter, rows: list) -> tuple[dict, list[dict], list[str], list[str]]:
    """Emit one `item` per row and return `(counts, failed, pending, outputs)`.

    The one place an `item` event is built for `add`, `remove`, `sync` and `restore`,
    so the four cannot drift into reporting the same outcome four slightly different
    ways. A row is anything carrying the `report_*` properties `_PlannedBook` and
    `_PlannedRemoval` both define; `pending` names the row's own input, which is a
    host path for a book being added and a device path for one being removed.
    """
    counts = {"total": 0, "done": 0, "skipped": 0, "failed": 0, "pending": 0}
    failed: list[dict] = []
    pending: list[str] = []
    outputs: list[str] = []
    for row in rows:
        counts["total"] += 1
        counts[row.status] += 1
        if row.status == "pending":
            pending.append(row.report_input)
        if row.status == "failed":
            failed.append(
                {
                    "id": row.id,
                    "input": row.report_input,
                    "reason": row.reason,
                    "detail": row.detail,
                }
            )
        outputs.extend(row.report_outputs)
        reporter.item(
            id=row.id,
            status=row.status,
            input=row.report_input,
            outputs=row.report_outputs,
            bytes_in=row.report_bytes_in,
            bytes_out=row.report_bytes_out,
            reason=row.reason,
            detail=row.detail,
            warnings=row.warnings,
        )
    return counts, failed, pending, outputs


def _add_result(
    reporter: Reporter,
    planned: list[_PlannedBook],
    *,
    thumbnail_statuses: dict[str, str],
    snapshot: backup_module.Snapshot | None,
    operation_id: str | None,
    free_space: int,
    queued: list[_PlannedBook],
    device_books_unreadable: int,
) -> dict:
    """Emit one `item` per planned book and build `body`'s return value.

    Shared by the real run and `--dry-run` so the two cannot report different shapes
    for the same book: an agent parsing either gets the same keys, with a missing
    VALUE (`snapshot: null`, `operation: null`, `thumbnails: {}`) where a dry run has
    nothing to say rather than a missing key.
    """
    counts, failed, pending, outputs = _report_rows(reporter, planned)

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
            # The id `restore --op ID` takes; `None` when this run put nothing on the
            # device at all.
            "operation": operation_id,
            "free_space": free_space,
            "bytes_planned": sum(book.size for book in queued),
            # How many device books this run could not read an id from — 0 normally.
            # Non-zero means the "already on the device" check was blind for that
            # many books, which is worth knowing BEFORE wondering why duplicates
            # appeared. See `_device_book_ids`.
            "device_books_unreadable": device_books_unreadable,
        },
    }


# --- remove: what is never deleted ---------------------------------------------------


# The folder a purchased KFX keeps its DRM assets in, inside its own `.sdr` sidecar.
ASSETS_DIR = "assets"


def _sidecar_prefix(book_path: str) -> str:
    """`documents/en/Book.azw3` -> `documents/en/Book.sdr/` — the device's own pairing
    rule, by name rather than by any recorded link, because that is how the firmware
    itself pairs a book with its reading position."""
    suffix = PurePosixPath(book_path).suffix
    stem = book_path[: -len(suffix)] if suffix else book_path
    return f"{stem}.sdr/"


def _protection_refusal(path: str, all_paths: set[str], *, sidecars_visible: bool) -> str | None:
    """Why this tool will never delete `path`, or `None` if it may.

    Six rules, listed in the order they are CHECKED — a path can break several, and
    the first match is the message the user gets, so the more specific reasons come
    before the more general one:

    0. **An empty path**, which names nothing to remove. Only reachable from a command
       line (`remove ""`), and answered rather than allowed to fall through the five
       below into a `None` that would read as permission.
    1. **An absolute path, or one with a `.`/`..` component** — the same shapes
       `validate_writable_path` refuses before a write. Unreachable from a device
       listing, reachable from a command line, and the two checks are otherwise
       line-for-line mirrors: they should not diverge.
    2. **`audible/`** is Amazon's audiobook data, off-limits to this whole plan.
    3. **`system/`** is device internals — Wi-Fi credentials, logs, settings — with
       only its `thumbnails/` child being ordinary cache data this tool writes.
    4. **A `*.kfx` whose sidecar holds an `assets/` folder** is a book bought from
       Amazon: the purchase's DRM assets live in that folder, and nothing on the host
       can reconstitute them.
    5. **Anything the mandatory backup does not cover** (`backup.DEFAULT_SCOPE`) — a
       book in a folder of the user's own making, or sitting at the device root. The
       snapshot taken moments earlier does not hold it, so `restore` could not put it
       back and would say `not_in_snapshot` while the book stayed gone. Deleting only
       what the backup holds is the invariant that makes "every deletion is undoable"
       true rather than merely intended, and it costs nothing: everything this tool
       itself places lives under `documents/<lang>/`, which is in scope.

    `sidecars_visible` is what makes rule 4 (the KFX one) safe on BOTH backends. Over
    MTP the cached device tree omits `*.sdr` folders entirely, so the `assets/` marker
    is simply not there to be found — and "no marker" would then read as "sideloaded, delete away"
    for exactly the books this rule exists to protect. So over MTP every `*.kfx` is
    refused, sideloaded or not: the two cannot be told apart there, and the wrong
    guess costs a purchase.

    **How a path reaches each rule differs, and rule 5 is the one that matters.** Both
    backends' `list_files` already refuse to LIST anything under `audible/` or under a
    non-`thumbnails` child of `system/`, so no selector can reach rules 2 and 3 — only
    a path NAMED on the command line can, which is why they are checked here as well
    as in the backends. Rule 5 is different: a book in `Books/` or at the device root
    IS listed, and is an ordinary book in every other respect, so `remove` keeps it out
    of `--match`'s net itself while `--asin` and a named path reach this and get the
    message. `validate_writable_path` and the scope check are both applied again
    immediately before every delete as the backstop (`_guarded_remove`); this is the
    layer that can explain itself to a user.
    """
    cleaned = str(path).replace("\\", "/").strip("/")
    parts = PurePosixPath(cleaned).parts
    if not cleaned or not parts:
        return "an empty device path names nothing to remove"
    if str(path).startswith(("/", "~")) or any(part in ("..", ".") for part in parts):
        return (
            f"{path!r} is not a plain device-relative path (as `ebook kindle scan` "
            "reports one), so this tool will not delete it"
        )
    directories = parts[:-1]
    if PROTECTED_DIRS & set(directories):
        return (
            f"{cleaned} is inside audible/, which holds Amazon's audiobook data — "
            "this tool never removes anything from there"
        )
    for index, part in enumerate(directories):
        if part == RESTRICTED_PARENT and (
            index + 1 >= len(directories) or directories[index + 1] != RESTRICTED_EXCEPTION
        ):
            return (
                f"{cleaned} is under system/, which holds device internals — only "
                f"system/{RESTRICTED_EXCEPTION}/ is ever written or removed"
            )
    if PurePosixPath(cleaned).suffix.lower() == ".kfx":
        assets = f"{_sidecar_prefix(cleaned)}{ASSETS_DIR}/"
        if not sidecars_visible:
            return (
                f"{cleaned} is a KFX book and this device is connected over MTP, "
                f"where {assets} cannot be listed at all — so a purchased book cannot "
                "be told apart from a sideloaded one, and this tool refuses both"
            )
        if any(other.startswith(assets) for other in all_paths):
            return (
                f"{cleaned} is a purchased KFX book: its DRM assets sit in {assets}, "
                "and nothing on the host could put them back"
            )
    if not backup_module.DEFAULT_SCOPE.includes(cleaned):
        return (
            f"{cleaned} is outside the area every backup covers "
            f"({', '.join(backup_module.DEFAULT_SCOPE.directories)}), so the snapshot "
            "taken to protect this run does not hold it and nothing could put it back"
        )
    return None


def _guarded_remove(backend: DeviceBackend, path: str) -> None:
    """`validate_writable_path` FIRST, then the backup scope, then the delete — the
    same backstop `write` already has, for the same reason: a path handed to this can
    be built from a book's own EXTH records (a thumbnail's name is), and neither
    backend's `remove` checks anything itself.

    The scope check is what makes "nothing is deleted that the last snapshot does not
    hold" an invariant of the one function that deletes, rather than a property of
    whichever planner happened to call it (`_protection_refusal` explains the same
    rule to the user, earlier and in words)."""
    cleaned = validate_writable_path(path)
    if not backup_module.DEFAULT_SCOPE.includes(cleaned):
        raise DeviceWritePathRejected(
            f"refusing to delete {path!r}: outside the area every backup covers, so "
            "no snapshot holds it"
        )
    backend.remove(cleaned)


def _companions_on_device(
    book_path: str, book_id: str, cdetype: str, all_paths: set[str]
) -> tuple[list[str], str | None, str]:
    """`(the book's sidecar files, its thumbnail if the device has one, the `.sdr`
    directory itself)` — the three things that make a removed book actually gone
    rather than half-gone, kept apart here because they are not equally safe to take
    (see `_books_sharing_sidecar`).

    The sidecar is every file under `<stem>.sdr/` — reading position, highlights, page
    numbers — and the thumbnail is the EXACT name `thumbnails.thumbnail_name` builds
    from the book's own EXTH 113 id and content type, never a substring search under
    `system/thumbnails/` (which could hit a DIFFERENT book's thumbnail that happened
    to share a substring — the same fix `_has_thumbnail` and `backup._companions_of`
    already apply). A book with no id has no thumbnail to find.
    """
    prefix = _sidecar_prefix(book_path)
    sidecar = sorted(path for path in all_paths if path.startswith(prefix))
    if any(
        PurePosixPath(path).suffix.lower() == ".kfx" and _sidecar_prefix(path) == prefix
        for path in all_paths
    ):
        # A `.kfx` shares this stem, so this `.sdr` is a purchased book's — belt and
        # braces behind `_books_sharing_sidecar`: a sideloaded conversion sitting
        # beside a purchase (`Book.kfx` + `Book.azw3`) must never be able to take the
        # purchase's DRM assets with it, whatever any caller decides about the rest.
        assets = f"{prefix}{ASSETS_DIR}/"
        sidecar = [path for path in sidecar if not path.startswith(assets)]
    thumbnail = None
    if book_id:
        candidate = f"{thumbnails.THUMBNAIL_DIR}{thumbnails.thumbnail_name(book_id, cdetype)}"
        if candidate in all_paths:
            thumbnail = candidate
    return sidecar, thumbnail, prefix.rstrip("/")


def _books_sharing_sidecar(book_path: str, all_paths: set[str], removing: set[str]) -> list[str]:
    """Other books on the device that the firmware pairs with the SAME `.sdr` folder
    and that this run is NOT removing.

    The device pairs a document with its sidecar by STEM, so `Book.azw3` and
    `Book.mobi` in one folder share `Book.sdr/` — and taking it with one of them would
    delete the reading position, highlights and page numbers of a book the user is
    KEEPING. That is the one case where a removal must leave the sidecar exactly where
    it is; it is reported as `kept` on the item rather than passed over in silence.

    **Any surviving file sharing the stem counts, with no extension filter at all.**
    The asymmetry is total: a missed owner costs reading position and highlights,
    unrecoverably, while a spurious one costs an empty `.sdr` folder left on the
    device — which this already reports honestly as `kept`/`shared_with`. An allowlist
    fails safe on the cheap side and open on the expensive one, and would have to name
    every format a Kindle opens (`.pdf`, `.txt`, `.htm`, `.html`, `.rtf`, `.doc`, and
    whatever the next firmware adds) to be even approximately right. A denylist can be
    added here if some file type ever proves to need one.

    A file INSIDE the sidecar cannot match: `_sidecar_prefix` builds its prefix from
    the stem, so `Book.sdr/position.mbp` yields `Book.sdr/position.sdr/`, never
    `Book.sdr/`. Thumbnails live under a different directory entirely.

    **`removing` must be the books that will ACTUALLY be removed, not the ones that
    were selected.** A selection can contain books this tool then refuses — a
    purchased `Book.kfx` beside a sideloaded `Book.azw3` is exactly that shape, and
    both match one `--match`. Counting the refused purchase as "being removed" makes
    its own `.sdr/assets` look unshared, and the conversion beside it would take the
    purchase's DRM with it and report `done`. `_plan_removals` therefore resolves
    every refusal BEFORE it computes this.
    """
    prefix = _sidecar_prefix(book_path)
    return sorted(
        path
        for path in all_paths
        if path.casefold() != book_path.casefold()
        and path.casefold() not in removing
        and _sidecar_prefix(path) == prefix
    )


def _matches(needle: str, path: str, records: dict[int, bytes] | None) -> bool:
    """Ruling R36's `--match` rule, in the one place every command applies it: the
    device path OR the book's own EXTH title OR its author, case-insensitively, any
    hit counting. A device's filenames are often opaque, so a user typing an author
    name must not silently match nothing."""
    title = exth.record_text(records or {}, exth.TAG_TITLE) or ""
    author = exth.record_text(records or {}, exth.TAG_AUTHOR) or ""
    return any(needle in haystack.lower() for haystack in (path, title, author))


@dataclass
class _PlannedRemoval:
    """One book this run was asked to delete, and everything decided about it.

    `status` starts `"pending"` — which for a run WITHOUT `--yes` is the final,
    honest answer ("this is what would go"), and for a run with it is settled by
    exactly one `fail()`/`skip()`/`succeed()` call.

    `removed` and `not_removed` are the two halves of the truth about a removal that
    only partly happened: over MTP a `.sdr` sidecar and anything under `system/`
    cannot be deleted at all (Calibre 9.15 has no delete-by-name and its cached tree
    omits both), so the book goes and its sidecar stays. That is reported as the
    `sidecar_not_removed` WARNING on a book that is still `done`, because the book
    really is gone — claiming the sidecar went with it would be a lie the user finds
    the next time they re-add that book and it opens where they left off.

    `kept` is a different thing from either, and carries NO warning: a sidecar that
    another book on the device still reads (`shared_with`) was never part of the plan
    to begin with. Nothing went wrong, and nothing was left half-done — this removal
    simply does not own that folder.
    """

    id: int
    device_path: str
    book_id: str = ""
    title: str = ""
    author: str = ""
    size: int | None = None
    # The sidecar files THIS row is slated to take (empty when another book keeps them
    # or another row of the same run takes them), the co-selected books that share
    # them, and the book's own thumbnail.
    sidecar: list[str] = field(default_factory=list)
    sidecar_siblings: list[str] = field(default_factory=list)
    thumbnail: str | None = None
    kept: list[str] = field(default_factory=list)
    shared_with: list[str] = field(default_factory=list)
    sidecar_dir: str | None = None
    status: str = "pending"
    reason: str | None = None
    detail: str | None = None
    removed: list[str] = field(default_factory=list)
    not_removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, reason: str, term: str, message: str) -> None:
        self.status, self.reason, self.detail = "failed", reason, _detail(term, message)

    def skip(self, reason: str, term: str, message: str) -> None:
        self.status, self.reason, self.detail = "skipped", reason, _detail(term, message)

    def succeed(self) -> None:
        self.status, self.reason, self.detail = "done", None, None

    @property
    def companions(self) -> list[str]:
        """Everything that goes WITH the book, in deletion order."""
        return [*self.sidecar, *([self.thumbnail] if self.thumbnail else [])]

    @property
    def would_remove(self) -> list[str]:
        return [self.device_path, *self.companions]

    @property
    def is_off_the_device(self) -> bool:
        """Did this row's book actually leave? `skipped`/`source_missing` counts: it
        was not there to begin with. Anything else (refused, failed, not yet
        attempted) means the book is still on the device."""
        return self.status == "done" or (
            self.status == "skipped" and self.reason == "source_missing"
        )

    # --- what `_report_rows` reads off every reported row ----------------------
    @property
    def report_input(self) -> str:
        return self.device_path

    @property
    def report_outputs(self) -> list[str]:
        # Deliberately empty on every path: a removal PRODUCES nothing, and reporting
        # deleted paths as `outputs` would make one field mean two opposite things
        # across `sync`'s two halves. What went away is `data.removed`.
        return []

    @property
    def report_bytes_in(self) -> int | None:
        return self.size or None

    @property
    def report_bytes_out(self) -> int | None:
        return None

    def as_dict(self) -> dict:
        return {
            "device_path": self.device_path,
            "book_id": self.book_id or None,
            "title": self.title or None,
            "author": self.author or None,
            "size": self.size,
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
            "would_remove": self.would_remove,
            "removed": list(self.removed),
            "not_removed": list(self.not_removed),
            # Deliberately left on the device, with the surviving books that need it.
            "kept": list(self.kept),
            "shared_with": list(self.shared_with),
        }


def _plan_removals(
    paths: list[str],
    *,
    records_by_path: dict[str, dict[int, bytes]],
    sizes: dict[str, int],
    all_paths: set[str],
    start_id: int,
    sidecars_visible: bool,
    protected_status: str = "failed",
    ids_by_path: dict[str, str] | None = None,
) -> list[_PlannedRemoval]:
    """One row per book to remove, each already carrying the `.sdr` sidecar and the
    thumbnail that will go with it — and already refused if it names something this
    tool never deletes.

    **Refusals are resolved FIRST**, before anything is decided about shared
    sidecars, because a refused book stays on the device and therefore still needs
    the sidecar it shares (see `_books_sharing_sidecar`).

    `protected_status` is `"failed"` for `remove`, where the user aimed a selector at
    that specific book and it did not happen, and `"skipped"` for `sync
    --delete-extras`, where the user asked to mirror a batch and a protected book is a
    permanent structural exclusion rather than a failed attempt — a mirror run that
    can never exit 0 teaches everyone reading it to ignore exit 1 on the one command
    that deletes books.

    `ids_by_path`, when the caller has the whole device's ids (`sync` does, for free),
    keeps a thumbnail that ANOTHER surviving book with the same EXTH 113 id also uses.
    Partial or absent, it simply finds fewer sharers.
    """
    refusals = {
        path: _protection_refusal(path, all_paths, sidecars_visible=sidecars_visible)
        for path in paths
    }
    removing = {path.casefold() for path, refusal in refusals.items() if refusal is None}
    ids = ids_by_path or {}

    rows: list[_PlannedRemoval] = []
    for offset, path in enumerate(paths):
        records = records_by_path.get(path) or {}
        book_id = exth.record_text(records, exth.TAG_UUID) or ""
        cdetype = exth.record_text(records, exth.TAG_CDETYPE) or thumbnails.DEFAULT_CDETYPE
        sidecar, thumbnail, sidecar_dir = _companions_on_device(path, book_id, cdetype, all_paths)
        # Tracked apart from the thumbnail's own sharers below, because only THIS one
        # decides whether the sidecar stays: a cover shared with another book says
        # nothing about who reads the reading position.
        sidecar_sharers = _books_sharing_sidecar(path, all_paths, removing)
        # Among the books that WILL go and share this one sidecar, exactly one takes
        # it — the last of them, so that by the time it runs every other has already
        # been attempted and `_remove_one` can check they really went.
        siblings = [
            other
            for other in paths
            if other.casefold() != path.casefold()
            and other.casefold() in removing
            and _sidecar_prefix(other) == _sidecar_prefix(path)
        ]
        takes_sidecar = (
            path.casefold() in removing
            and not sidecar_sharers
            and not any(paths.index(other) > paths.index(path) for other in siblings)
        )
        thumbnail_sharers: list[str] = []
        kept_thumbnail: str | None = None
        if thumbnail and book_id:
            # Another book on the device carrying the same EXTH 113 id reads the same
            # cover — regenerable (`ebook kindle thumbnails`), but still not this
            # removal's to take while that book is there.
            thumbnail_sharers = sorted(
                other
                for other, other_id in ids.items()
                if other != path and other_id == book_id and other.casefold() not in removing
            )
            if thumbnail_sharers:
                kept_thumbnail, thumbnail = thumbnail, None
        row = _PlannedRemoval(
            id=start_id + offset,
            device_path=path,
            book_id=book_id,
            title=exth.record_text(records, exth.TAG_TITLE) or "",
            author=exth.record_text(records, exth.TAG_AUTHOR) or "",
            size=sizes.get(path),
            # A sidecar another SURVIVING book also reads is KEPT, not removed — and
            # the `.sdr` directory is then not offered for cleanup either.
            sidecar=sidecar if takes_sidecar else [],
            sidecar_siblings=siblings if takes_sidecar else [],
            thumbnail=thumbnail,
            kept=([*sidecar] if sidecar_sharers else [])
            + ([kept_thumbnail] if kept_thumbnail else []),
            shared_with=sorted({*sidecar_sharers, *thumbnail_sharers}),
            sidecar_dir=sidecar_dir if takes_sidecar else None,
        )
        refusal = refusals[path]
        if refusal is not None:
            # Nothing about this book will be touched, so nothing is advertised:
            # `would_remove` collapses to the book's own path, and the thumbnail it
            # would otherwise have listed is not part of any plan. (`sidecar` is
            # already empty — `takes_sidecar` requires being in `removing`, which a
            # refused path never is.)
            row.thumbnail = None
            if protected_status == "skipped":
                row.skip("unsupported_input", DETAIL_PROTECTED, refusal)
            else:
                row.fail("engine_error", DETAIL_PROTECTED, refusal)
        rows.append(row)
    return rows


def _remove_one(
    backend: DeviceBackend,
    device: Device,
    row: _PlannedRemoval,
    siblings: list[_PlannedRemoval],
) -> None:
    """Delete one book and then its companions, settling `row`'s own verdict.

    **The book goes FIRST and its companions only after it is gone.** The other order
    reads better on paper (clean up around the book, then the book) but is wrong: a
    device that refuses the book would leave it sitting there stripped of its reading
    position and its cover, which is precisely the damage a removal is supposed to
    take care of. If the book cannot go, nothing else does either.

    `siblings` are the other books of this same run that share this one's `.sdr`
    folder, all of them already attempted (the planner hands the sidecar to the last
    of the group). A sidecar is only taken when every one of them really did leave:
    the plan said they would, but a device that refused one at runtime turns this into
    the shared-sidecar case, and the reading position of a book that is still there
    must not go.
    """
    try:
        _guarded_remove(backend, row.device_path)
    except mtp.MtpPathNotInCachedTree as error:
        # A `FileNotFoundError` subclass, so it is caught BEFORE the "already gone"
        # clause below: this path was in the listing moments ago, so for a BOOK the
        # honest reading is "this cannot be deleted over MTP", not "it is not there".
        row.fail(
            "engine_error",
            DETAIL_REMOVE_FAILED,
            f"the device could not be asked to delete {row.device_path}: {error}",
        )
        return
    except FileNotFoundError:
        # Gone between the listing and now. The companions are NOT skipped with it:
        # they are this book's sidecar and cover, and with the book itself absent they
        # are exactly the orphans this command exists to clean up. Nothing is recorded
        # as removed for the book — because nothing was.
        row.skip(
            "source_missing",
            DETAIL_SOURCE_MISSING,
            f"{row.device_path} is no longer on the device",
        )
    except (RuntimeError, OSError) as error:
        # Per book, never fatal to the run: a write-protected device, a yanked cable,
        # an MTP `CalibreError`. The books after this one still deserve their turn.
        row.fail(
            "engine_error",
            DETAIL_REMOVE_FAILED,
            f"the device refused the deletion: {error}",
        )
        return
    else:
        row.removed.append(row.device_path)

    still_there = [sibling.device_path for sibling in siblings if not sibling.is_off_the_device]
    if row.sidecar and still_there:
        # The plan handed this row the shared sidecar because every book sharing it
        # was going to go; one of them did not. `row.sidecar` is cleared rather than
        # only filtered out of a local copy, so `would_remove` and `kept` cannot
        # disagree about the same files afterwards.
        row.kept.extend(row.sidecar)
        row.shared_with.extend(still_there)
        row.sidecar = []
        row.sidecar_dir = None

    for companion in row.companions:
        try:
            _guarded_remove(backend, companion)
        except mtp.MtpPathNotInCachedTree:
            row.not_removed.append(companion)
        except FileNotFoundError:
            # Genuinely already gone: nothing was removed and nothing is left behind,
            # so there is neither a journal entry nor a warning to make about it.
            continue
        except (RuntimeError, OSError):
            row.not_removed.append(companion)
        else:
            row.removed.append(companion)

    if device.mode == "mass_storage" and row.sidecar_dir and device.mount is not None:
        # Best-effort, and mass storage only: `remove` deletes FILES, so an emptied
        # `.sdr` would otherwise stay on the device as an empty directory forever.
        # `rmdir` never deletes content — a sidecar file that could not go keeps the
        # directory, and the `OSError` that reports it is swallowed here. This is the
        # one delete that does not go through `backend.remove`, so it validates the
        # path itself rather than inheriting `_guarded_remove`'s check.
        with contextlib.suppress(OSError, DeviceWritePathRejected):
            (device.mount / validate_writable_path(row.sidecar_dir)).rmdir()

    if row.not_removed:
        row.warnings.append(SIDECAR_NOT_REMOVED_WARNING)
    if row.status == "pending":
        # ...and not over a `skip()` already recorded above: a book that was already
        # gone stays `skipped`/`source_missing` even though its orphaned companions
        # were cleaned up after it.
        row.succeed()


def _journal_remove(
    root: Path, key: str, rows: list[_PlannedRemoval], snapshot_name: str
) -> str | None:
    """Record what this run took off the device, or nothing if it took nothing.

    `paths` is what `restore --op ID` puts back — the books AND the sidecars and
    thumbnails that went with them, which is exactly the set the protecting snapshot
    holds. A path that could NOT be removed is deliberately absent: it is still on the
    device, and restoring it would be a no-op at best.
    """
    paths = [path for row in rows for path in row.removed]
    if not paths:
        return None
    return backup_module.journal_append(
        root,
        key,
        {
            "op": "remove",
            "paths": paths,
            "books": [
                {
                    "device_path": row.device_path,
                    "book_id": row.book_id or None,
                    "removed": list(row.removed),
                    "not_removed": list(row.not_removed),
                }
                for row in rows
                if row.removed
            ],
            "snapshot": snapshot_name,
        },
    )


def _remove_books(
    reporter: Reporter,
    root: Path,
    key: str,
    backend: DeviceBackend,
    device: Device,
    rows: list[_PlannedRemoval],
    snap: backup_module.Snapshot,
) -> str | None:
    """Delete every row that still wants deleting, then journal what went.

    The journal is written on the way out of an interrupt too (the same guard `add`
    puts around its copy phase, for the same reason): a Ctrl+C between the first
    delete and the journal call would otherwise leave books off the device with no
    record of which operation took them, and `restore --op` needs that record. The
    snapshot behind it holds the bytes either way.
    """
    pending = [row for row in rows if row.status == "pending"]
    by_path = {row.device_path.casefold(): row for row in rows}
    try:
        for index, row in enumerate(pending, start=1):
            siblings = [
                by_path[other.casefold()]
                for other in row.sidecar_siblings
                if other.casefold() in by_path
            ]
            _remove_one(backend, device, row, siblings)
            reporter.progress(
                stage="remove",
                index=index,
                count=len(pending),
                path=row.device_path,
                percent=100.0 * index / len(pending),
            )
    except BaseException:
        with contextlib.suppress(Exception):
            _journal_remove(root, key, rows, snap.path.name)
        raise
    return _journal_remove(root, key, rows, snap.path.name)


def _removal_result(
    reporter: Reporter,
    rows: list[_PlannedRemoval],
    *,
    snapshot: backup_module.Snapshot | None,
    operation_id: str | None,
) -> dict:
    """Emit one `item` per planned removal and build `body`'s return value. Shared by
    the planning run (no `--yes`) and the real one, so an agent parsing either gets
    the same keys — `snapshot`/`operation` explicitly `null` where a plan has nothing
    to say, never a missing key."""
    counts, failed, pending, outputs = _report_rows(reporter, rows)
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
            "books": [row.as_dict() for row in rows],
            "removed": [path for row in rows for path in row.removed],
            "snapshot": _snapshot_summary(snapshot.path, fallback=snapshot) if snapshot else None,
            "operation": operation_id,
        },
    }


def run_remove(
    args,
    *,
    device_finder: Callable[[], Device] | None = None,
    backend_factory: Callable[..., DeviceBackend] | None = None,
) -> int:
    """Delete books from the device — the one command in this subsystem whose whole
    purpose is destructive, and therefore the one with the most refusals in it.

    **Nothing is deleted without `--yes`.** Without it the run lists what it would
    take, writes nothing (not to the device, not a backup, not a journal entry) and
    exits 0 — the plan IS the dry run, which is why this command has no `--dry-run`
    flag of its own to be confused with it. With `--yes` the mandatory backup runs
    first, exactly as it does for every other write command, and a failure there is a
    precondition that was never met: exit 3 with all-zero counts, nothing deleted.

    **A removal takes three things together**: the book, its `.sdr` folder (reading
    position, highlights, page numbers) and its thumbnail. Leaving the `.sdr` behind
    is why a re-added book resumes in the wrong place, and leaving the thumbnail
    behind leaves a cover for a book that is gone.

    **Selection is by device path, by EXTH 113 id (`--asin`) or by `--match`** — the
    device path OR the book's own title OR its author, case-insensitively, any hit
    counting (Ruling R36, the same rule `thumbnails` and `add` use). At least one
    selector is required: a bare `remove --yes` is a usage error, never "everything".
    Identity is read from the books themselves, never from their filenames.

    **Some things are never removed** and are refused per item with a clear message
    (`detail: "protected: ..."`), rather than aborting the whole run: anything the
    mandatory backup does not cover (`backup.DEFAULT_SCOPE` — a book in a folder of
    the user's own making, or at the device root, which no snapshot holds and
    `restore` could therefore never put back), anything under `audible/`, anything
    under `system/` but its `thumbnails/` child, and a purchased `*.kfx` whose
    `.sdr/assets` holds its DRM — over MTP, where that marker cannot be listed at all,
    EVERY `*.kfx` instead (`_protection_refusal`). A refusal makes the run's own exit
    code 1, because something the user asked for demonstrably did not happen.

    **The selectors reach those refusals differently, on purpose.** `--match` is a net
    and never casts it outside the backed-up area, so a book in a folder of the user's
    own making produces no item at all. A named path and `--asin` are identities: they
    name specific books, reach the refusal, and get the message — a run that reported
    nothing and exited 0 would leave the user wondering which of the two it meant.

    **Over MTP a `.sdr` sidecar and anything under `system/` cannot be deleted at
    all** — Calibre 9.15 exposes no delete-by-name and its cached tree omits both, so
    `backend.remove` raises `MtpPathNotInCachedTree`. That is reported as the
    `sidecar_not_removed` warning on a book that is still `done`: the book is gone
    either way, and pretending the sidecar went with it would be a lie the user
    discovers later.
    """
    yes = bool(args.yes)
    names = [str(book).strip("/") for book in args.books]
    needle = args.match.lower() if args.match else None
    asin = (args.asin or "").strip() or None
    if not names and needle is None and asin is None:
        raise UsageError(
            "nothing selected: name one or more device paths (as `ebook kindle scan` "
            "reports them), or pass --match TEXT or --asin ID. `remove` never means "
            "'remove everything'."
        )

    stages = ["detect", "backup", "plan", "remove"] if yes else ["detect", "plan"]
    options = {
        "kindle_command": "remove",
        "books": len(names),
        "match": args.match,
        "asin": asin,
        "yes": yes,
    }

    def body(reporter: Reporter, root: Path, device: Device, backend: DeviceBackend) -> dict:
        key = backup_module.device_key(device)
        announce = _announcer(reporter, stages)
        snap: backup_module.Snapshot | None = None

        if yes:
            announce("backup")
            failure, snap = _mandatory_backup(reporter, root, backend, key)
            if failure is not None:
                return failure
            if snap is None:  # pragma: no cover - one or the other, never neither
                raise RuntimeError(
                    "internal error: a remove reached its delete phase with no "
                    "protecting snapshot on record"
                )

        announce("plan")
        entries = backend.list_files()
        sizes = {entry.path: entry.size for entry in entries}
        all_paths = set(sizes)
        books = [
            entry for entry in entries if PurePosixPath(entry.path).suffix.lower() in BOOK_SUFFIXES
        ]

        # Which books have to be READ, which over MTP means fetching each one into
        # the header cache. `--asin` needs every book's id, wherever it sits, because
        # an id names a book this run may still have to refuse by name. `--match`
        # only ever selects inside the backed-up area (below), so nothing outside it
        # needs its title read. With only paths named, just those books are read —
        # for the content type the thumbnail's name needs.
        wanted = {name.casefold(): name for name in names}
        if asin is not None:
            to_read = books
        elif needle is not None:
            to_read = [
                entry
                for entry in books
                if backup_module.DEFAULT_SCOPE.includes(entry.path)
                or entry.path.casefold() in wanted
            ]
        else:
            to_read = [entry for entry in books if entry.path.casefold() in wanted]
        records_by_path, _ = _records_for(root, device, backend, to_read, key)

        folded_asin = asin.casefold() if asin else None
        selected: list[str] = []
        for entry in books:
            records = records_by_path.get(entry.path)
            hit = entry.path.casefold() in wanted
            if not hit and folded_asin is not None:
                book_id = exth.record_text(records or {}, exth.TAG_UUID) or ""
                hit = book_id.casefold() == folded_asin
            if not hit and needle is not None and backup_module.DEFAULT_SCOPE.includes(entry.path):
                # `--match` is a NET, so it never sweeps in a book outside the area
                # every backup covers — there would be nothing to put back if it did.
                # The two IDENTITY selectors are different: a path names one file and
                # an id names every copy carrying it, but either way the user asked
                # for THOSE books by name, and deserves the refusal and its reason
                # (`_protection_refusal`, via `_plan_removals`) rather than a run that
                # reports nothing at all and exits 0.
                hit = _matches(needle, entry.path, records)
            if hit:
                selected.append(entry.path)
                wanted.pop(entry.path.casefold(), None)

        # Over MTP a `*.sdr` folder is not in the cached tree at all, so nothing
        # under one can be seen — which changes what can be PROVEN about a KFX book
        # (see `_protection_refusal`) as well as what can be deleted.
        sidecars_visible = device.mode == "mass_storage"
        rows = _plan_removals(
            selected,
            records_by_path=records_by_path,
            sizes=sizes,
            all_paths=all_paths,
            start_id=1,
            sidecars_visible=sidecars_visible,
        )
        # A path the user NAMED that no book on the device answers to gets an item of
        # its own rather than vanishing from the report: they pointed at it
        # explicitly, so a typo — or a path that is real but is not a book, or is one
        # this tool never touches — deserves saying so.
        # Matched the way FAT32 itself matches: a user who typed a path in the wrong
        # case must be told what is actually there, not that it is missing.
        folded_paths = {path.casefold() for path in all_paths}
        for offset, name in enumerate(wanted.values(), start=len(rows) + 1):
            row = _PlannedRemoval(id=offset, device_path=name, size=sizes.get(name))
            refusal = _protection_refusal(name, all_paths, sidecars_visible=sidecars_visible)
            if refusal is not None:
                row.fail("engine_error", DETAIL_PROTECTED, refusal)
            elif name.casefold() in folded_paths:
                row.fail(
                    "engine_error",
                    DETAIL_NOT_A_BOOK,
                    f"{name} is on the device but is not a book "
                    f"({', '.join(sorted(BOOK_SUFFIXES))}); a book's sidecar and "
                    "thumbnail are removed with the book itself, never on their own",
                )
            else:
                row.fail(
                    "source_missing",
                    DETAIL_SOURCE_MISSING,
                    f"{name} is not on the device",
                )
            rows.append(row)

        if not yes:
            return _removal_result(reporter, rows, snapshot=None, operation_id=None)

        announce("remove")
        operation_id = _remove_books(reporter, root, key, backend, device, rows, snap)
        return _removal_result(reporter, rows, snapshot=snap, operation_id=operation_id)

    return _run(
        args,
        kindle_command="remove",
        stages=stages,
        options=options,
        device_finder=device_finder or detect.find_device,
        backend_factory=backend_factory or default_backend_factory,
        body=body,
    )


# --- sync ----------------------------------------------------------------------------


def run_sync(
    args,
    *,
    device_finder: Callable[[], Device] | None = None,
    backend_factory: Callable[..., DeviceBackend] | None = None,
) -> int:
    """Make the device match an `ebook build` batch: add what the library has and the
    device lacks, and — only with `--delete-extras` AND `--yes` — remove what the
    device has and the library does not.

    **The adding half IS `add --batch NAME`**, down to the same planning
    (`_plan_add`), the same identity rules (EXTH 113, with the journal's provenance
    fallback for an id-less book), the same free-space arithmetic and the same
    copy/thumbnails/verify/journal phases (`_copy_books`). It is shared code, not a
    second implementation, so the two commands cannot drift into placing books
    differently.

    **An extra is a device book whose EXTH 113 id the batch does not carry.** Four
    things are never extras, each for its own reason: a book whose id could not be
    read at all (absence from the library cannot be PROVEN for it, and guessing costs
    the user a book), a book outside the area every backup covers (no snapshot holds
    it, so no `restore` could undo it), a book at a path this run's own plan targets
    (the copy phase is about to write there, or already refused to), and — because
    `--delete-extras` reads a batch as a statement about the whole library — every
    book of a batch that never finished, which is refused up front as a usage error
    rather than silently treated as "the library does not have these".

    Extras are reported in `result.data.extras` whether or not `--delete-extras` was
    given, so the removal can be seen before it is armed; with `--delete-extras` they
    become planned removals (`pending`), and only with `--yes` as well are they
    actually deleted — after the same mandatory backup, with the same
    `.sdr`-and-thumbnail pairing and the same refusals `remove` applies. A protected
    extra is `skipped`, not `failed`: the user asked for a mirror, and a purchased
    book is a permanent structural exclusion from one rather than a failed attempt at
    anything — a run that can never exit 0 would teach everyone reading it to ignore
    exit 1 on the one command that deletes books.

    **The two halves are journalled as two operations**, an `add` and a `remove`,
    exactly as if the two commands had been run in turn. `restore --op ID` therefore
    undoes either half on its own, and `add`'s provenance lookup keeps working for
    books this command placed.

    **The hazard worth stating plainly**: an extra is anything the batch does not
    name, including a book somebody else put on the device. `--delete-extras --yes`
    on a batch that is not actually the whole library will remove books the user
    wanted. That is why it needs two flags, why the plan is printed first, why the
    mandatory backup runs before it, and why `restore --op` exists.
    """
    dry_run = bool(args.dry_run)
    delete_extras = bool(args.delete_extras)
    root = output_root(args.output_dir)
    language_override = _validated_language_override(args.lang)
    if not args.batch:
        raise UsageError(
            "sync needs --batch NAME: the `ebook build` batch this device should "
            "mirror. To copy books named on the command line, use `ebook kindle add`."
        )
    try:
        batch = sanitize_batch(args.batch)
    except BatchNameError as error:
        raise UsageError(str(error)) from error
    sources, library_ids, unfinished = _library_batch(root, batch, flag="--batch")
    if not sources:
        raise UsageError(f"no books found in batch {batch!r}", code="no_input_matched")
    if delete_extras and unfinished:
        # An interrupted or partly-failed build still writes a `run.json`, and every
        # book it never got to is then missing from `library_ids` — which would make
        # the device's copy of it an EXTRA and delete it. The batch has to describe
        # the whole library before it can be used to decide what the library is not.
        raise UsageError(
            f"batch {batch!r} has {unfinished} item(s) that are neither done nor "
            "skipped, so it does not describe the whole library yet — "
            "--delete-extras would treat every book behind them as an extra. Finish "
            "the build (or re-run it) first, or sync without --delete-extras."
        )

    deletions_armed = delete_extras and bool(args.yes) and not dry_run
    stages = (
        ["detect", "plan"]
        if dry_run
        else ["detect", "backup", "plan", "copy", "thumbnails", "verify"]
    )
    if deletions_armed:
        stages.append("remove")
    options = {
        "kindle_command": "sync",
        "batch": batch,
        "lang": language_override,
        "match": args.match,
        "delete_extras": delete_extras,
        "yes": bool(args.yes),
        "dry_run": dry_run,
    }

    def body(reporter: Reporter, root: Path, device: Device, backend: DeviceBackend) -> dict:
        key = backup_module.device_key(device)
        announce = _announcer(reporter, stages)
        needle = args.match.lower() if args.match else None
        snap: backup_module.Snapshot | None = None

        if not dry_run:
            announce("backup")
            failure, snap = _mandatory_backup(reporter, root, backend, key)
            if failure is not None:
                return failure
            if snap is None:  # pragma: no cover - one or the other, never neither
                raise RuntimeError(
                    "internal error: a sync reached its write phase with no protecting "
                    "snapshot on record"
                )

        announce("plan")
        view = _survey_device(reporter, root, device, backend, key)
        planned, queued = _plan_add(
            view,
            sources,
            language_override=language_override,
            needle=needle,
            max_path=MAX_DEVICE_PATH.get(device.mode, MAX_DEVICE_PATH["mtp"]),
            placements=_previous_placements(root, key),
        )
        free_space, to_copy = _fit_to_free_space(backend, queued)

        # A book with no readable id is never an extra — see this command's
        # docstring. Neither is anything sitting at a path THIS run's own plan
        # targets, whatever its id: either the copy phase is about to overwrite it
        # (the journal's unverified-placement waiver, `_provenance_verdict`) and
        # deleting it afterwards would take the book just written, or the plan already
        # refused to touch it as an `output_collision` — and deleting a file the same
        # run declined to overwrite would leave the user with neither copy.
        planned_paths = {book.device_path.casefold() for book in planned}
        extra_entries = [
            entry
            for entry in view.books
            if (view.ids_by_path.get(entry.path) or "") not in ("", *library_ids)
            and entry.path.casefold() not in planned_paths
            # ...and nothing the mandatory backup does not hold: a book outside
            # `backup.DEFAULT_SCOPE` cannot be restored afterwards, so it is not this
            # command's to delete. `remove` refuses such a path by name (with a
            # message); here there is no name to answer, so it is simply not an extra.
            and backup_module.DEFAULT_SCOPE.includes(entry.path)
        ]
        extra_records: dict[str, dict[int, bytes]] = {}
        if extra_entries:
            # Only the extras are read, not the library: their own title/author are
            # what `--match` narrows on and what the report names them by, and their
            # content type is what a thumbnail's name needs.
            extra_records, _ = _records_for(root, device, backend, extra_entries, key)
            if needle is not None:
                extra_entries = [
                    entry
                    for entry in extra_entries
                    if _matches(needle, entry.path, extra_records.get(entry.path))
                ]
        removals = (
            _plan_removals(
                [entry.path for entry in extra_entries],
                records_by_path=extra_records,
                sizes=view.sizes,
                all_paths=view.paths,
                start_id=len(planned) + 1,
                sidecars_visible=device.mode == "mass_storage",
                # A protected book is a permanent structural exclusion from a MIRROR,
                # not a failed attempt at something the user aimed at — see
                # `_plan_removals`. `remove` keeps `failed`.
                protected_status="skipped",
                # `sync` knows every device book's id already, so a thumbnail another
                # surviving book also uses can be kept here (it cannot in `remove`,
                # which does not always read the whole device).
                ids_by_path=view.ids_by_path,
            )
            if delete_extras
            else []
        )

        thumbnail_statuses: dict[str, str] = {}
        operation_id: str | None = None
        if not dry_run:
            thumbnail_statuses, operation_id = _copy_books(
                reporter, root, key, backend, to_copy, snap, announce
            )

        remove_operation: str | None = None
        if deletions_armed:
            announce("remove")
            remove_operation = _remove_books(reporter, root, key, backend, device, removals, snap)

        rows = [*planned, *removals]
        counts, failed, pending, outputs = _report_rows(reporter, rows)
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
                "removals": [row.as_dict() for row in removals],
                # Reported whether or not `--delete-extras` was given: seeing what a
                # removal WOULD take is the point of a mirror command.
                "extras": [
                    {
                        "device_path": entry.path,
                        "book_id": view.ids_by_path.get(entry.path) or None,
                        "title": exth.record_text(
                            extra_records.get(entry.path) or {}, exth.TAG_TITLE
                        )
                        or None,
                        "size": entry.size,
                    }
                    for entry in extra_entries
                ],
                "removed": [path for row in removals for path in row.removed],
                "thumbnails": thumbnail_statuses,
                "snapshot": _snapshot_summary(snap.path, fallback=snap) if snap else None,
                "operation": operation_id,
                "remove_operation": remove_operation,
                "free_space": free_space,
                "bytes_planned": sum(book.size for book in queued),
                "device_books_unreadable": len(view.unreadable),
            },
        }

    return _run(
        args,
        kindle_command="sync",
        stages=stages,
        items=len(sources),
        options=options,
        device_finder=device_finder or detect.find_device,
        backend_factory=backend_factory or default_backend_factory,
        body=body,
    )


# --- restore -------------------------------------------------------------------------


@dataclass
class _RestoredFile:
    """One file a restore selected, with the outcome `backup.restore` reported for
    it. `pending` is what a run WITHOUT `--yes` reports (it WOULD go back); every
    other status is a real verdict."""

    id: int
    device_path: str
    status: str
    reason: str | None = None
    detail: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def report_input(self) -> str:
        return self.device_path

    @property
    def report_outputs(self) -> list[str]:
        return [self.device_path] if self.status == "done" else []

    @property
    def report_bytes_in(self) -> int | None:
        return None

    @property
    def report_bytes_out(self) -> int | None:
        # Per-file sizes are not in `RestoreReport` (only the run's total, in
        # `data.restore.bytes`), and inventing one per item would mean re-reading the
        # manifest here for a number nothing needs.
        return None


def _resolve_snapshot(root: Path, key: str, name: str | None) -> Path | None:
    """The snapshot directory a restore will read: a name under THIS device's own
    `backups/` first, then `name` as a path, or — with no name at all — the newest
    complete snapshot. `None` when nothing readable answers to it.

    The order matters. A snapshot name is a bare timestamp, and trying it as a path
    first resolves it against the current working directory: a directory of that name
    sitting next to the user (another device's backups, copied there to look at) would
    win over this device's own snapshot of the same name."""
    if not name:
        return backup_module.latest(root, key)
    for candidate in (backup_module.backup_root(root, key) / "backups" / name, Path(name)):
        if (candidate / backup_module.MANIFEST_NAME).is_file():
            return candidate
    return None


def run_restore(
    args,
    *,
    device_finder: Callable[[], Device] | None = None,
    backend_factory: Callable[..., DeviceBackend] | None = None,
) -> int:
    """Put a snapshot's files back on the device, or undo exactly one journalled
    operation.

    **It only ever WRITES files back; it never deletes.** Undoing an operation that
    REMOVED files therefore restores them, which is the case this exists for; undoing
    one that ADDED files restores nothing, because the snapshot that protected it was
    taken before those files existed — reported honestly as `not_in_snapshot` rather
    than as a silent success. Taking an added book off the device is `remove`'s job,
    behind its own `--yes`.

    **Nothing is written without `--yes`**, the same gate `remove` has and for a
    closely related reason: this is the one operation that overwrites files the user
    still has. A bare `restore` would otherwise roll a whole device back to the newest
    snapshot — destroying a book they replaced since, and resurrecting ones they
    deliberately removed — from a command line with no confirmation in it at all.
    Without `--yes` the run reports exactly what it would put back and writes nothing.

    **Every selected file is hashed against the manifest before a byte is written**,
    including in that plan, and one that disagrees is refused rather than restored:
    recovery is exactly where a corrupt snapshot does the most damage. That hashing is
    a full read of everything selected, which is why this run reports `progress` in
    two phases (`verify`, then `restore`) — a whole-library plan would otherwise sit
    silent for minutes.

    **A snapshot from a DIFFERENT Kindle is refused** unless `--force` says otherwise
    (the manifest records the device it came from). Writing one device's library onto
    another is not a restore, and nothing downstream could tell afterwards.

    **The snapshot is resolved BEFORE the mandatory pre-write backup**, never after.
    Restoring writes to the device, so it takes the same backup every write command
    takes (so the restore itself can be undone) — and that backup becomes the newest
    snapshot, so a `restore` with no SNAPSHOT argument resolved afterwards would
    restore the state it had just recorded and do nothing at all. A run without
    `--yes` takes no backup, since it writes nothing.

    One `item` is emitted per SELECTED FILE — not per book — so undoing an operation
    reports the book, its sidecar and its thumbnail separately, and a whole-snapshot
    restore reports every file in it.
    """
    plan_only = not args.yes
    stages = ["detect", "restore"] if plan_only else ["detect", "backup", "restore"]
    options = {
        "kindle_command": "restore",
        "snapshot": args.snapshot,
        "op": args.op,
        "yes": bool(args.yes),
        "force": bool(args.force),
    }

    def body(reporter: Reporter, root: Path, device: Device, backend: DeviceBackend) -> dict:
        key = backup_module.device_key(device)
        announce = _announcer(reporter, stages)

        only: list[str] | None = None
        snapshot_name = args.snapshot
        if args.op:
            operation = next(
                (
                    entry
                    for entry in backup_module.journal_read(root, key)
                    if entry.get("id") == args.op
                ),
                None,
            )
            if operation is None:
                return _body_failure(
                    reporter,
                    code="usage",
                    message=(
                        f"no operation {args.op!r} in this device's journal "
                        f"({backup_module.backup_root(root, key) / backup_module.JOURNAL_NAME}); "
                        "the id is `result.data.operation` of the run that made it"
                    ),
                    exit_code=EXIT_USAGE,
                )
            only = [path for path in (operation.get("paths") or []) if isinstance(path, str)]
            # An explicit SNAPSHOT still wins: the operation names the snapshot that
            # protected it, but that one can have been deleted by hand, and refusing
            # to look anywhere else would make recovery impossible.
            snapshot_name = args.snapshot or operation.get("snapshot")

        snapshot_dir = _resolve_snapshot(root, key, snapshot_name)
        if snapshot_dir is None:
            # `dependency_missing`, not `backup_failed`: nothing was attempted, and
            # `_EXIT_FOR_CODE` maps `backup_failed` to exit 1 — a code whose own table
            # disagrees with the exit code beside it is worse than a looser code.
            return _body_failure(
                reporter,
                code="dependency_missing",
                message=(
                    f"no readable snapshot {snapshot_name!r} for this device under "
                    f"{backup_module.backup_root(root, key) / 'backups'}"
                    if snapshot_name
                    else "this device has no complete backup on this host yet: run "
                    "`media-tools ebook kindle backup` first"
                ),
                exit_code=EXIT_DEPENDENCY,
            )

        taken_from = backup_module.manifest_serial(snapshot_dir)
        if taken_from and taken_from != key and not args.force:
            return _body_failure(
                reporter,
                code="usage",
                message=(
                    f"{snapshot_dir} was taken from a different Kindle ({taken_from}, "
                    f"not {key}): writing its library onto this device would not be a "
                    "restore. Pass --force if that is genuinely what you want."
                ),
                exit_code=EXIT_USAGE,
            )

        if not plan_only:
            announce("backup")
            failure, _ = _mandatory_backup(reporter, root, backend, key)
            if failure is not None:
                return failure

        announce("restore")

        def on_progress(done: int, total: int, phase: str) -> None:
            reporter.progress(
                stage=phase,
                index=done,
                count=total,
                path=f"kindle:{key}",
                percent=(100.0 * done / total) if total else 100.0,
            )

        try:
            report = backup_module.restore(
                backend, snapshot_dir, only=only, dry_run=plan_only, on_progress=on_progress
            )
        except backup_module.BackupFailed as error:
            # Caught here rather than left to `_run`, which would map it to exit 1
            # ("an item failed"): nothing was attempted, which is exit 3's meaning —
            # the same reading `_mandatory_backup` applies to a failed precondition.
            # `dependency_missing` for the same reason as above: the snapshot named is
            # not usable, and `backup_failed`'s own table says exit 1.
            return _body_failure(
                reporter, code="dependency_missing", message=str(error), exit_code=EXIT_DEPENDENCY
            )

        rows: list[_RestoredFile] = []
        for path in report.paths:
            rows.append(
                _RestoredFile(
                    id=len(rows) + 1,
                    device_path=path,
                    status="pending" if plan_only else "done",
                )
            )
        for path in report.missing:
            rows.append(
                _RestoredFile(
                    id=len(rows) + 1,
                    device_path=path,
                    status="failed",
                    reason="source_missing",
                    detail=_detail(
                        DETAIL_SOURCE_MISSING,
                        "the snapshot's manifest names this file but its stored copy "
                        "is gone from the snapshot",
                    ),
                )
            )
        for path in report.corrupt:
            rows.append(
                _RestoredFile(
                    id=len(rows) + 1,
                    device_path=path,
                    status="failed",
                    reason="engine_error",
                    detail=_detail(
                        DETAIL_CORRUPT,
                        "the stored copy no longer hashes to what the manifest "
                        "recorded, so it was NOT written to the device",
                    ),
                )
            )
        for path in report.not_in_snapshot:
            rows.append(
                _RestoredFile(
                    id=len(rows) + 1,
                    device_path=path,
                    status="skipped",
                    reason="source_missing",
                    detail=_detail(
                        DETAIL_NOT_IN_SNAPSHOT,
                        "this snapshot holds no copy of it — an operation that ADDED "
                        "it is undone with `remove`, not here: restore never deletes",
                    ),
                )
            )

        counts, failed, pending, outputs = _report_rows(reporter, rows)
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
                "restore": {
                    "snapshot": str(snapshot_dir),
                    "operation": args.op,
                    "plan_only": plan_only,
                    "files": report.files,
                    "bytes": report.bytes,
                    "missing": report.missing,
                    "corrupt": report.corrupt,
                    "no_thumbnail": report.no_thumbnail,
                    "not_in_snapshot": report.not_in_snapshot,
                }
            },
        }

    return _run(
        args,
        kindle_command="restore",
        stages=stages,
        options=options,
        device_finder=device_finder or detect.find_device,
        backend_factory=backend_factory or default_backend_factory,
        body=body,
    )


# --- eject -----------------------------------------------------------------------------


def run_eject(
    args,
    *,
    device_finder: Callable[[], Device] | None = None,
    backend_factory: Callable[..., DeviceBackend] | None = None,
) -> int:
    """Release the device so the cable can come out — the one command here that
    touches a Kindle and writes NOTHING to it, and therefore the one that takes no
    backup.

    **What each backend does is different, and both are `backend.eject()`'s own
    business, not this function's.** Mass storage flushes the host's pending writes
    (`sync`) and then asks the platform to eject the whole disk the mount sits on
    (`diskutil eject` on macOS, `udisksctl unmount` + `power-off` on Linux), retrying
    once if the first attempt reports the volume busy. MTP has nothing to eject at
    all: the helper simply closes the session (`shutdown()`), and the op exists so a
    caller can treat the two backends identically rather than branching on the mode
    itself.

    **Nothing is deleted, written or backed up.** The `sync` a mass-storage eject runs
    is a flush of bytes the host already owed the device, not new content — so there is
    nothing for a snapshot to protect, and `eject` is the one device command with no
    `_mandatory_backup` call in it. Run it before unplugging, especially after `add`,
    `sync`, `thumbnails` or `restore`: those commands' writes are the ones the flush is
    for.

    **A failed eject is reported through the same mapping every other command uses**
    (`_error_code_for`, via `_run`), and the two failures it actually has are told
    apart there. A volume still busy after the retry is `device_busy`: `massstorage`
    raises `DeviceBusy` for exactly that case, and the fix is to close whatever is
    reading the volume and run `eject` again. Anything else — a missing `diskutil`,
    `udisksctl` or `sync` binary — is `dependency_missing`, which is what that code
    means. Both exit 3, and the device is untouched either way. (A `sync` that runs
    and returns non-zero is not a failure here at all: its return code is deliberately
    not checked, since it says nothing actionable.)
    """
    stages = ["detect", "eject"]

    def body(reporter: Reporter, root: Path, device: Device, backend: DeviceBackend) -> dict:
        reporter.stage(stage="eject", index=2, count=len(stages))
        backend.eject()
        return {
            "counts": {"total": 0, "done": 0, "skipped": 0, "failed": 0, "pending": 0},
            "failed": [],
            "pending": [],
            "outputs": [],
            "run_file": None,
            # The same two fields `status`'s own `data.device` leads with, so one
            # parser reads both. No serial: `eject` needs no identity beyond "the
            # device that was just detected", and `status` is where a user asks for
            # that deliberately.
            "data": {"device": {"mode": device.mode, "backend": _backend_label(backend)}},
        }

    return _run(
        args,
        kindle_command="eject",
        stages=stages,
        options={"kindle_command": "eject"},
        device_finder=device_finder or detect.find_device,
        backend_factory=backend_factory or default_backend_factory,
        body=body,
    )
