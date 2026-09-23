"""Snapshots of a Kindle's user content, the restore that puts them back, and the
journal a later `undo` reads.

This is the load-bearing safety feature of the whole Kindle path: every command that
writes to the device takes a backup first and aborts if the backup fails, with no flag
to skip it. Everything downstream therefore trusts that a *completed* snapshot is
complete, and three rules exist to make that trust honest:

1. **The listing comes from `backend.list_files()`** — the root listing, which is
   already filtered, raises on failure, and is never cached when incomplete. A
   self-built `{"op": "list"}` through `run_ops`, or a negative from `exists()`
   against a cold cache, would both let a FAILED listing pass as an empty device: the
   backup would write an empty snapshot, report success, and clear a destructive
   command to run. Anything the listing raises becomes `BackupFailed`, never an empty
   snapshot. The one remaining hole — a mount that vanished, where a filesystem walk
   legitimately returns nothing — is closed by asking the backend for its free space
   before a zero-file listing is believed.
2. **A snapshot directory is built under a `.partial` name** (`core.paths.temp_path`)
   and renamed only once it is complete, and the `latest` pointer moves only after
   that rename. A killed process can therefore leave an incomplete snapshot behind,
   but never one that looks finished.
3. **Snapshots are never pruned.** This tool does not delete the user's backups. The
   only directory it ever removes is its own `.partial` staging area, and only after a
   failure it caught and converted into `BackupFailed`.

Layout, under the output root every other task resolves through
`core.paths.output_root`:

    <output root>/_kindle/<serial>/
        backups/<UTC timestamp>/manifest.json
        backups/<UTC timestamp>/files/<the device's own paths>
        backups/latest                     a pointer, not a symlink
        journal.jsonl

`_kindle` is one of `core.paths.RESERVED_ROOT_ENTRIES`, so nothing else writes there.

**Incremental via hard links.** The device is listed once. A file whose path and size
match the previous manifest and whose mtime differs by at most two seconds — or by a
whole number of hours — is hard-linked from the previous snapshot instead of being
transferred again. The whole-hour rule is not slack: FAT stores local time, so a DST
change shifts every mtime on the device by exactly an hour, and treating that as
"changed" would re-copy the entire library twice a year. Everything else is fetched
through `read_many` (one batch, never one `calibre-debug` spawn per file) and hashed
from the bytes that landed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Any
from uuid import uuid4

from media_tools.core.paths import fsync_replace, temp_path, truncate_name
from media_tools.tasks.ebook.exth import TAG_UUID, read_records, record_text
from media_tools.tasks.ebook.kindle.backend import DeviceFile
from media_tools.tasks.ebook.kindle.massstorage import (
    PROTECTED_DIRS,
    RESTRICTED_EXCEPTION,
    RESTRICTED_PARENT,
    is_volume_litter,
)

# Codes from the CLOSED registry in `core/events.py`. The plan called the warning
# `backup_hash_from_previous`; the registry already ships `hash_from_previous`, and an
# unregistered code raises inside `Reporter` by design — so these name the registered
# ones rather than adding a second code for one meaning.
HASH_FROM_PREVIOUS_WARNING = "hash_from_previous"
BACKUP_FAILED_ERROR = "backup_failed"

KINDLE_DIR = "_kindle"
MANIFEST_NAME = "manifest.json"
FILES_DIR = "files"
LATEST_NAME = "latest"
JOURNAL_NAME = "journal.jsonl"
MANIFEST_VERSION = 1

# How far two mtimes may differ and still count as the same file, and the period a
# whole-hour shift is measured against. FAT has 2-second granularity to begin with,
# which is where the tolerance itself comes from.
MTIME_TOLERANCE_S = 2.0
HOUR_S = 3600.0

# Files per `read_many` call. One call for the whole library would be the cheapest
# possible MTP run but would report no progress for hours; one call per file is the
# thing `read_many` exists to prevent. A first backup of a few thousand books pays a
# handful of extra device rescans for progress the user can actually watch, and every
# later backup transfers far fewer files than this anyway.
TRANSFER_CHUNK = 200

_HASH_CHUNK = 1 << 20
# Long enough for any real device name, short enough that a mirrored path survives on
# a host filesystem with a 255-byte component limit even in multi-byte scripts.
_MAX_COMPONENT = 200
_UNSAFE_KEY = re.compile(r"[^A-Za-z0-9._-]")
_BOOK_SUFFIXES = frozenset({".azw", ".azw3", ".azw8", ".kfx", ".mobi", ".prc", ".pdb"})
_JOURNAL_RESERVED = frozenset({"v", "id", "at"})


class BackupFailed(RuntimeError):
    """The backup (or the restore reading one back) could not be completed.

    Maps to the `backup_failed` error code, which is the only one the closed registry
    offers for this whole area — a restore failure raises it too rather than inventing
    a code this task is not allowed to add.
    """


# --- scope ----------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """Which of the device's files a backup takes.

    This is a further filter on top of the backends' own exclusions, not a replacement
    for them: `list_files` already refuses `audible/`, everything under `system/` but
    `thumbnails/`, and volume litter, and those rules are re-applied here (from
    `massstorage`'s own constants) so that a scope written by hand can never widen
    them by accident.
    """

    directories: tuple[str, ...]
    root_suffixes: tuple[str, ...]
    root_names: frozenset[str]

    def includes(self, path: str) -> bool:
        cleaned = (path or "").strip("/")
        if not cleaned:
            return False
        parts = cleaned.split("/")
        if any(is_volume_litter(part) for part in parts):
            return False
        directories = parts[:-1]
        if any(part in PROTECTED_DIRS for part in directories):
            return False
        for index, part in enumerate(directories):
            if part == RESTRICTED_PARENT and parts[index + 1] != RESTRICTED_EXCEPTION:
                return False
        if any(cleaned.startswith(f"{name}/") for name in self.directories):
            return True
        if len(parts) == 1:
            return parts[0] in self.root_names or parts[0].endswith(self.root_suffixes)
        return False


# `documents/` holds the books, their `.sdr` sidecars (reading position, highlights,
# page numbers) and `My Clippings.txt`; `system/thumbnails/` the cover cache;
# `amazon-cover-bug/` the covers some firmwares stash there; `fonts/` the user's own
# sideloaded fonts. At the device root, Calibre's own `*.calibre` bookkeeping files —
# and `My Clippings.txt` by name, because firmware has put it in both places.
DEFAULT_SCOPE = Scope(
    directories=("documents", "system/thumbnails", "amazon-cover-bug", "fonts"),
    root_suffixes=(".calibre",),
    root_names=frozenset({"My Clippings.txt"}),
)


# --- results --------------------------------------------------------------------


@dataclass
class Snapshot:
    """One completed snapshot.

    `bytes_copied` is what came off the DEVICE; `bytes_linked` is what did not — a
    hard link to the previous snapshot, or (on a filesystem that refuses hard links) a
    local copy of it. The split is the one worth reading: it says how much of the
    device was actually re-read.
    """

    path: Path
    files: int
    bytes_copied: int
    bytes_linked: int
    manifest: Path
    warnings: tuple[str, ...] = ()


@dataclass
class RestoreReport:
    """What `restore` put back (or, under `dry_run`, would have).

    `missing` names manifest entries whose stored file is gone from the snapshot —
    they are reported rather than silently dropped, because a restore that quietly
    skips a book is the same class of lie as a backup that quietly skips one.
    """

    files: int
    bytes: int
    paths: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    dry_run: bool = False


# --- locations ------------------------------------------------------------------


def device_key(device) -> str:
    """The directory name a device's backups live under.

    A serial, when the device reports one. When it does not, `unknown-<8 hex of the
    sha256 of the mount name or model hint>` — never a shared constant, because two
    serial-less devices under one root would write into each other's snapshots and
    silently corrupt both.
    """
    serial = _safe_key(getattr(device, "serial", None) or "")
    if serial:
        return serial
    mount = getattr(device, "mount", None)
    hint = (
        (mount.name if mount is not None else None)
        or getattr(device, "model_hint", None)
        or f"{getattr(device, 'mode', '?')}:{getattr(device, 'product_id', None)}"
    )
    return "unknown-" + hashlib.sha256(str(hint).encode("utf-8")).hexdigest()[:8]


def backup_root(root: Path, serial: str) -> Path:
    """`<output root>/_kindle/<serial>/`. `root` is the output root itself, resolved
    by the caller through `core.paths.output_root` like every other task."""
    return Path(root) / KINDLE_DIR / _require_key(serial)


def latest(root: Path, serial: str) -> Path | None:
    """The newest COMPLETE snapshot, or None.

    Read from the `latest` pointer file rather than by sorting directory names: the
    pointer moves only after a snapshot's `.partial` rename, so it can never name an
    incomplete one. A pointer whose target has since been deleted by hand reads back
    as None rather than as a path that is not there.
    """
    pointer = backup_root(root, serial) / "backups" / LATEST_NAME
    data = _read_json(pointer)
    name = data.get("snapshot") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name:
        return None
    candidate = pointer.parent / name
    return candidate if (candidate / MANIFEST_NAME).is_file() else None


# --- taking a snapshot ----------------------------------------------------------


def snapshot(
    device_backend,
    *,
    root: Path,
    serial: str,
    full: bool = False,
    verify_hashes: bool = False,
    on_progress=None,
    on_warning=None,
    scope: Scope = DEFAULT_SCOPE,
) -> Snapshot:
    """Back the device up into a new snapshot under `root`, and return it.

    `full` ignores the previous snapshot entirely: every file is transferred, nothing
    is linked. `verify_hashes` never carries a hash over from the previous manifest —
    every hash is computed from the bytes now in the snapshot — but still links rather
    than re-fetching, because the bytes it needs to hash are already local; the two
    flags are therefore not the same thing.

    `on_progress(done, total, phase)` fires as `covers.resolve`'s does, with `phase`
    either `"link"` or `"transfer"`. `on_warning(code, message)` fires at most once per
    run, and only for `hash_from_previous`.

    Raises `BackupFailed` for anything that stops the snapshot from being complete,
    including a failed listing — never a silently empty snapshot. A `KeyboardInterrupt`
    propagates untouched and leaves the `.partial` directory where it is, since a
    directory that still carries that name cannot be mistaken for a finished backup.
    """
    key = _require_key(serial)
    backups = backup_root(root, key) / "backups"
    # The listing comes FIRST, before a single directory is created: a backup that
    # cannot list the device leaves no trace of having been attempted.
    entries = _listing(device_backend, scope)
    previous_dir = None if full else latest(root, key)
    previous = _manifest_index(previous_dir)

    name = _free_snapshot_name(backups)
    final = backups / name
    partial = temp_path(final)
    partial.mkdir(parents=True)  # creates `backups/` itself on a first-ever backup

    try:
        planned = [_plan_one(entry, previous, previous_dir, partial) for entry in entries]
        _link_phase(planned, previous_dir, on_progress)
        _transfer_phase(device_backend, planned, on_progress)
        result = _finish(
            planned,
            partial=partial,
            final=final,
            key=key,
            previous_dir=previous_dir,
            full=full,
            verify_hashes=verify_hashes,
            on_warning=on_warning,
        )
    except BackupFailed:
        _discard(partial)
        raise
    except Exception as error:  # noqa: BLE001 - re-raised as BackupFailed below
        _discard(partial)
        raise BackupFailed(
            f"the backup failed and its incomplete snapshot was discarded: {error}"
        ) from error
    return result


@dataclass
class _Planned:
    entry: DeviceFile
    stored: str
    target: Path
    previous: dict | None  # the previous manifest entry, when this file can be reused
    linked: bool = False
    origin: str = "device"


def _listing(device_backend, scope: Scope) -> list[DeviceFile]:
    """The root listing, filtered to `scope`.

    Everything the backend raises becomes `BackupFailed` here: a failed listing must
    abort the backup, because the alternative — treating it as an empty device — is
    how a snapshot ends up empty, successful and trusted by a destructive command.
    """
    try:
        found = device_backend.list_files()
    except Exception as error:  # noqa: BLE001 - every listing failure aborts the backup
        raise BackupFailed(f"could not list the device, so nothing was backed up: {error}") from (
            error
        )
    if not found:
        # A mounted volume that has gone away lists as empty rather than raising, so
        # the one case where "nothing at all" is indistinguishable from a failure gets
        # a second question the backend cannot answer from a stale cache.
        _confirm_the_device_answered(device_backend)
    return sorted(
        (entry for entry in found if scope.includes(entry.path)), key=lambda entry: entry.path
    )


def _confirm_the_device_answered(device_backend) -> None:
    try:
        device_backend.free_space()
    except Exception as error:  # noqa: BLE001 - an unreachable device aborts the backup
        raise BackupFailed(
            "the device listed no files and then failed to report its free space, so "
            f"it is gone rather than empty; nothing was backed up: {error}"
        ) from error


def _plan_one(
    entry: DeviceFile, previous: dict[str, dict], previous_dir: Path | None, partial: Path
) -> _Planned:
    stored = _stored_path(entry.path)
    target = partial / FILES_DIR / stored
    candidate = previous.get(entry.path)
    reusable = (
        candidate is not None
        and previous_dir is not None
        and _is_unchanged(entry, candidate)
        and (previous_dir / FILES_DIR / _stored_of(candidate)).is_file()
    )
    return _Planned(
        entry=entry, stored=stored, target=target, previous=candidate if reusable else None
    )


def _is_unchanged(entry: DeviceFile, previous: dict) -> bool:
    try:
        size = int(previous.get("size"))
        mtime = float(previous.get("mtime"))
    except (TypeError, ValueError):
        return False
    return entry.size == size and _mtime_matches(entry.mtime, mtime)


def _mtime_matches(now: float, before: float) -> bool:
    """Same file? Within two seconds (FAT's own granularity), or off by a whole number
    of hours — FAT stores LOCAL time, so a DST change shifts every mtime on the device
    by exactly an hour and would otherwise re-copy the entire library twice a year."""
    delta = abs(float(now) - float(before))
    if delta <= MTIME_TOLERANCE_S:
        return True
    remainder = delta % HOUR_S
    return min(remainder, HOUR_S - remainder) <= MTIME_TOLERANCE_S


def _link_phase(planned: list[_Planned], previous_dir: Path | None, on_progress) -> None:
    reusable = [item for item in planned if item.previous is not None]
    for done, item in enumerate(reusable, start=1):
        source = previous_dir / FILES_DIR / _stored_of(item.previous)
        item.target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, item.target)
            item.linked, item.origin = True, "link"
        except OSError:
            # exFAT, a network share, or a link count already at the limit: copying
            # from the PREVIOUS SNAPSHOT still costs the device nothing, which is the
            # expensive resource here.
            shutil.copyfile(source, item.target)
            item.linked, item.origin = False, "previous-copy"
        if on_progress:
            on_progress(done, len(reusable), "link")


def _transfer_phase(device_backend, planned: list[_Planned], on_progress) -> None:
    pending = [item for item in planned if item.previous is None]
    total = len(pending)
    done = 0
    for start in range(0, total, TRANSFER_CHUNK):
        chunk = pending[start : start + TRANSFER_CHUNK]
        device_backend.read_many([(item.entry.path, item.target) for item in chunk])
        done += len(chunk)
        if on_progress:
            on_progress(done, total, "transfer")


def _finish(
    planned: list[_Planned],
    *,
    partial: Path,
    final: Path,
    key: str,
    previous_dir: Path | None,
    full: bool,
    verify_hashes: bool,
    on_warning,
) -> Snapshot:
    records: list[dict] = []
    bytes_copied = 0
    bytes_linked = 0
    carried = 0

    for item in planned:
        if not item.target.is_file():
            # `read_many` reported success for a file that is not there. Failing the
            # whole backup is the only honest answer: the manifest would otherwise
            # name bytes nobody holds, and a later restore would find nothing.
            raise BackupFailed(
                f"{item.entry.path} is missing from the snapshot after the transfer "
                "reported success, so the backup is not complete"
            )
        # The size recorded is what actually landed, never what the listing promised —
        # the next run compares the device against this manifest, so a size that
        # describes anything but the stored bytes would corrupt every later decision.
        size = item.target.stat().st_size
        carry = item.previous.get("sha256") if (item.previous and not verify_hashes) else None
        if isinstance(carry, str) and len(carry) == 64:
            digest, hash_from = carry, "previous"
            carried += 1
        else:
            digest, hash_from = _sha256(item.target), "computed"
        if item.previous is None:
            bytes_copied += size
        else:
            bytes_linked += size
        records.append(
            {
                "path": item.entry.path,
                "stored": item.stored,
                "size": size,
                "mtime": item.entry.mtime,
                "sha256": digest,
                "hash_from": hash_from,
                "linked": item.linked,
                "origin": item.origin,
            }
        )

    manifest = {
        "v": MANIFEST_VERSION,
        "type": "kindle-backup-manifest",
        "serial": key,
        "snapshot": final.name,
        "created_at": _timestamp(),
        "previous": previous_dir.name if previous_dir is not None else None,
        "full": full,
        "verify_hashes": verify_hashes,
        "counts": {
            "files": len(records),
            "copied": sum(1 for r in records if r["origin"] == "device"),
            "reused": sum(1 for r in records if r["origin"] != "device"),
        },
        "bytes": {"copied": bytes_copied, "linked": bytes_linked},
        "files": records,
    }
    _write_json(partial / MANIFEST_NAME, manifest)

    # Only now, with every byte and the manifest on disk, does the directory stop
    # being `.partial` — and only after THAT does `latest` move.
    _promote(partial, final)
    _write_json(
        final.parent / LATEST_NAME,
        {"v": MANIFEST_VERSION, "snapshot": final.name, "at": manifest["created_at"]},
    )

    warnings: tuple[str, ...] = ()
    if carried:
        warnings = (HASH_FROM_PREVIOUS_WARNING,)
        if on_warning:
            # Once per RUN, not once per file: a library-sized backup would otherwise
            # bury every other message under thousands of identical warnings.
            on_warning(
                HASH_FROM_PREVIOUS_WARNING,
                f"{carried} file(s) kept the hash recorded by the previous snapshot "
                "instead of being re-read; pass --verify-hashes to recompute every one.",
            )
    return Snapshot(
        path=final,
        files=len(records),
        bytes_copied=bytes_copied,
        bytes_linked=bytes_linked,
        manifest=final / MANIFEST_NAME,
        warnings=warnings,
    )


def _promote(partial: Path, final: Path) -> None:
    """The one instant a snapshot stops being partial, as its own function so the
    ordering around it is testable: everything before this can be abandoned, and
    `latest` is written strictly after it."""
    partial.replace(final)


def _discard(partial: Path) -> None:
    """Remove this run's own incomplete staging directory. The only thing this module
    ever deletes — a finished snapshot is the user's, and is never pruned."""
    shutil.rmtree(partial, ignore_errors=True)


# --- restoring ------------------------------------------------------------------


def restore(
    device_backend, snapshot: Path, *, only: list[str] | None = None, dry_run: bool
) -> RestoreReport:
    """Put a snapshot's files back on the device.

    `only` names device paths; each one is expanded so a book never goes back alone —
    its `.sdr` sidecar (reading position, highlights, page numbers) and its thumbnail
    travel with it, because a book restored without them reads as a different, unread
    book. The thumbnail is found through the book's own EXTH 113 id, read from the
    snapshot's copy of it, never from its filename. An entry that names a directory
    selects everything beneath it.

    `dry_run` computes the same report and writes nothing at all.
    """
    directory = Path(snapshot)
    data = _read_json(directory / MANIFEST_NAME)
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise BackupFailed(
            f"{directory} is not a readable snapshot: no {MANIFEST_NAME} that this "
            "tool wrote. Pass the path of a snapshot directory."
        )

    index = {
        str(entry["path"]): entry
        for entry in data["files"]
        if isinstance(entry, dict) and entry.get("path")
    }
    wanted = set(index) if only is None else _expand(directory, index, only)

    restored: list[str] = []
    missing: list[str] = []
    total = 0
    for path in sorted(wanted):
        stored = directory / FILES_DIR / _stored_of(index[path])
        if not stored.is_file():
            missing.append(path)
            continue
        total += stored.stat().st_size
        if not dry_run:
            device_backend.write(stored, path)
        restored.append(path)
    return RestoreReport(
        files=len(restored), bytes=total, paths=restored, missing=missing, dry_run=dry_run
    )


def _expand(directory: Path, index: dict[str, dict], only: list[str]) -> set[str]:
    wanted: set[str] = set()
    for request in only:
        cleaned = str(request).strip("/")
        if not cleaned:
            continue
        direct = [p for p in index if p == cleaned or p.startswith(f"{cleaned}/")]
        wanted.update(direct)
        for path in direct:
            wanted.update(_companions_of(directory, index, path))
    return wanted


def _companions_of(directory: Path, index: dict[str, dict], path: str) -> set[str]:
    """A book's `.sdr` sidecar and its thumbnail — the two things that make a restored
    book the same book rather than a fresh copy of it."""
    found: set[str] = set()
    book = PurePath(path)
    if book.suffix.lower() not in _BOOK_SUFFIXES:
        return found
    sidecar = f"{path[: -len(book.suffix)]}.sdr/"
    found.update(p for p in index if p.startswith(sidecar))

    stored = directory / FILES_DIR / _stored_of(index[path])
    book_id = record_text(read_records(stored), TAG_UUID) if stored.is_file() else None
    if book_id:
        # Matched by the id INSIDE the name rather than by rebuilding the filename:
        # the CDE type and the suffix vary by firmware, the id does not.
        found.update(
            p for p in index if p.startswith("system/thumbnails/") and book_id in PurePath(p).name
        )
    return found


# --- the journal ----------------------------------------------------------------


def journal_append(root: Path, serial: str, entry: dict) -> str:
    """Record one operation and return its id, for a later task to undo exactly it.

    JSON Lines, appended and fsynced: a crash can lose the entry being written but can
    never corrupt the ones already there, which is the property an undo needs.
    """
    base = backup_root(root, serial)
    base.mkdir(parents=True, exist_ok=True)
    operation_id = f"{_stamp()}-{uuid4().hex[:8]}"
    record: dict[str, Any] = {"v": MANIFEST_VERSION, "id": operation_id, "at": _timestamp()}
    record.update({k: v for k, v in dict(entry).items() if k not in _JOURNAL_RESERVED})
    with open(base / JOURNAL_NAME, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return operation_id


def journal_read(root: Path, serial: str) -> list[dict]:
    """Every journalled operation, oldest first. A line that will not parse is skipped
    rather than allowed to hide the good entries around it."""
    path = backup_root(root, serial) / JOURNAL_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            found.append(parsed)
    return found


# --- small helpers --------------------------------------------------------------


def _safe_key(value: str) -> str:
    return _UNSAFE_KEY.sub("", str(value or "")).strip(".")


def _require_key(serial: str) -> str:
    key = _safe_key(serial)
    if not key:
        raise BackupFailed(
            f"{serial!r} is not usable as a backup directory name. Resolve the device's "
            "key with backup.device_key(device), which falls back to a hashed name when "
            "the device reports no serial."
        )
    return key


def _stored_path(device_path: str) -> str:
    """Where a device path lives inside the snapshot. Identical to the device path
    except where a component is too long for the host filesystem, which
    `core.paths.truncate_name` shortens deterministically (so the same device file
    always maps to the same stored file, and the manifest records both)."""
    return "/".join(truncate_name(part, _MAX_COMPONENT) for part in device_path.split("/"))


def _stored_of(entry: dict) -> str:
    stored = entry.get("stored")
    return stored if isinstance(stored, str) and stored else str(entry.get("path", ""))


def _manifest_index(previous_dir: Path | None) -> dict[str, dict]:
    if previous_dir is None:
        return {}
    data = _read_json(previous_dir / MANIFEST_NAME)
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        return {}
    return {
        str(entry["path"]): entry
        for entry in files
        if isinstance(entry, dict) and entry.get("path")
    }


def _free_snapshot_name(backups: Path) -> str:
    """A UTC timestamp, with `-2`, `-3`, ... appended if a snapshot (or an abandoned
    `.partial` one) already holds that second."""
    base = _stamp()
    name = base
    suffix = 1
    while (backups / name).exists() or temp_path(backups / name).exists():
        suffix += 1
        name = f"{base}-{suffix}"
    return name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(target: Path, payload: dict) -> None:
    """`core.paths`' own temp+fsync+rename, so a crash mid-write never leaves a
    truncated manifest or a `latest` pointing at half a name."""
    temp = temp_path(target)
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_replace(temp, target)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
