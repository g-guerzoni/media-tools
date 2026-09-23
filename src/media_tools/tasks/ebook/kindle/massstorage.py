"""The mass-storage Kindle backend: the device mounts as an ordinary disk, so every
operation is a filesystem call — no MTP session, no vendor protocol.

`write()` and `read()` both follow this project's usual atomicity rule
(`core.paths.temp_path` + `fsync_replace`/`replace`, spec 7.1/7.5's "partial outputs
never count as done"): a Kindle pulled mid-copy must never leave a truncated book
sitting at its final name — on the device or on the host — and any failure anywhere in
either staged copy, including a `KeyboardInterrupt`, must leave no `.partial` file
behind either. `list_files` AND `exists` both refuse to answer at all once the
device is gone — a missing directory, or a mountpoint whose `st_dev` no longer matches
the one recorded at construction — because an empty listing (or a bare `False` from
`exists`) must mean "nothing is there", never "I could not look": a caller verifying a
just-written file needs to tell "verified absent" from "the device vanished
mid-check" apart, and a `Path.is_file()` that silently swallows the `OSError` an
unmounted volume raises cannot make that distinction on its own. It skips the
macOS/Linux volume litter every removable disk accumulates, never descends into
`audible/` (Amazon's audiobook data, untouchable by this whole plan), and only
descends into `system/` as far as `system/thumbnails/` — the rest of `system/` is
device internals (Wi-Fi credentials, logs, settings), not book content. `write` also
refuses any path outside what this project will ever touch (`validate_writable_path`,
`backend.py`) before it moves a single byte — a device path can be built from
untrusted data (a book's own EXTH records), so this is not merely a mirror of the
read-side exclusions above; it is the one place a write specifically is checked.
"""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
from pathlib import Path

from media_tools.core.paths import fsync_replace, temp_path
from media_tools.tasks.ebook.kindle.backend import (
    PROTECTED_DIRS,
    RESTRICTED_EXCEPTION,
    RESTRICTED_PARENT,
    VOLUME_LITTER,
    DeviceFile,
    is_volume_litter,
    prefix_targets_a_forbidden_system_child,
    validate_writable_path,
)
from media_tools.tasks.ebook.kindle.detect import DeviceNotFound

# The four constants and two functions above USED to be defined here, with `mtp.py`
# importing them from this module so the two backends' listings could not drift
# apart. They now live in `backend.py` instead (re-exported here for anything that
# still imports them from this module) because `backend.py`'s own write-path guard
# needs them too, and `backend.py` cannot import FROM `massstorage.py` (this module
# already imports `DeviceFile` the other way) without a cycle.
__all__ = [
    "PROTECTED_DIRS",
    "RESTRICTED_EXCEPTION",
    "RESTRICTED_PARENT",
    "VOLUME_LITTER",
    "is_volume_litter",
    "prefix_targets_a_forbidden_system_child",
]


class MassStorageBackend:
    """Talks to a Kindle mounted as a disk at `mount` (a `Device.mount` from
    `detect.find_device` in mass-storage mode)."""

    def __init__(self, mount: Path) -> None:
        self.mount = mount
        # The filesystem the mount sat on when detection handed it over, i.e. when this
        # really was the device. Unmounting a volume leaves its mountpoint DIRECTORY
        # behind, empty and on the PARENT filesystem, so `is_dir()` still says yes and
        # a walk still returns nothing — the one remaining way an empty listing could
        # mean "I could not look". A changed `st_dev` catches exactly that.
        #
        # Recorded-vs-now, deliberately, rather than the usual "is this a mountpoint?"
        # test (comparing `mount` against `mount.parent`): that would call every
        # folder-backed Kindle a non-device — every fixture here, and any legitimate
        # "treat this directory as a Kindle" use. Comparing against what was recorded
        # at construction does not care whether the path was ever a real mountpoint,
        # only whether it changed underneath us.
        self._device_id = _device_id(self.mount)

    def list_files(self, prefix: str = "") -> list[DeviceFile]:
        parts = Path(prefix).parts if prefix else ()
        if PROTECTED_DIRS & set(parts) or prefix_targets_a_forbidden_system_child(parts):
            return []
        if not self.mount.is_dir():
            # A missing MOUNT is not a missing prefix. Unmounted, ejected or never
            # there, the device itself is unreachable, and answering `[]` is how a
            # backup writes an empty snapshot, reports success and clears a destructive
            # command to run. `[]` keeps its meaning for a prefix that is genuinely not
            # on the device (below), per `backend.DeviceBackend`.
            raise FileNotFoundError(f"the Kindle is no longer mounted at {self.mount}")
        if self._device_id is not None and _device_id(self.mount) != self._device_id:
            raise FileNotFoundError(
                f"{self.mount} is no longer the filesystem it was when this device was "
                "detected: the volume was unmounted and what is left is an empty "
                "mountpoint directory, not a Kindle with nothing on it."
            )
        start = self.mount / prefix if prefix else self.mount
        if not start.is_dir():
            return []
        return list(self._walk(start))

    def _walk(self, directory: Path):
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda entry: entry.name)
        except OSError:
            return
        # `system/` holds device internals (Wi-Fi credentials, logs, settings) that
        # are none of this project's business — only its `thumbnails/` child is
        # ordinary cache data worth listing.
        restricted = directory.name == RESTRICTED_PARENT
        for entry in entries:
            if is_volume_litter(entry.name):
                continue
            if restricted and entry.name != RESTRICTED_EXCEPTION:
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name in PROTECTED_DIRS:
                    continue
                yield from self._walk(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                stat = entry.stat()
                relative = Path(entry.path).relative_to(self.mount).as_posix()
                yield DeviceFile(path=relative, size=stat.st_size, mtime=stat.st_mtime)

    def read(self, path: str, dest: Path) -> None:
        """Copy one file off the device, staged so a failure leaves NOTHING at `dest`.

        MTP's helper removes a half-fetched local file itself
        (`integrations/kindle_mtp.py:_op_get`), so writing straight to `dest` here made
        the two backends diverge in exactly the situation a backup exists for: a full
        disk or a yanked cable would leave mass storage holding a TRUNCATED file at the
        final name and MTP holding none. `read_many` inherits this rather than staging
        a second time — one guarantee is easier to keep than two.
        """
        dest = Path(dest)
        temp = temp_path(dest)
        try:
            shutil.copyfile(self.mount / path, temp)
            temp.replace(dest)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def read_many(self, items: list[tuple[str, Path]]) -> None:
        """`read` for a whole batch — see `backend.DeviceBackend`. A mounted disk has
        no round trip to save, so this is the loop the MTP backend cannot afford. What
        it adds over calling `read` directly is the parent directory: the no-partial
        guarantee comes from `read` itself, which stages every copy.
        """
        for path, dest in items:
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            self.read(path, dest)

    def write(self, local: Path, path: str) -> None:
        # Validated BEFORE anything else: `path` may be built from data this project
        # did not produce (a book's own EXTH records), and nothing else here stops
        # `..`/`audible/`/a forbidden `system/` child from ever reaching a real write.
        path = validate_writable_path(path)
        target = self.mount / path
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = temp_path(target)
        try:
            shutil.copyfile(local, temp)
            fsync_replace(temp, target)
        except BaseException:
            # BaseException, not Exception: a Ctrl+C mid-copy is exactly when a
            # `.partial` would be left sitting ON THE DEVICE, which is what this
            # module's own docstring promises never happens.
            temp.unlink(missing_ok=True)
            raise

    def remove(self, path: str) -> None:
        """Delete one file. `FileNotFoundError` means exactly one thing here — THIS
        PATH is not on the device — because that is what a caller acts on: `ebook
        kindle remove` reports it as "the book was already gone" and moves on.

        So a vanished DEVICE must not take that shape. `unlink()` on an unmounted
        volume raises `FileNotFoundError` like any other missing path, and a Kindle
        pulled mid-run would then report every remaining book as already gone and exit
        0 — telling the user their books were not there when the device simply left.
        The same check `list_files` and `exists` make, raising `DeviceNotFound`
        instead: a `RuntimeError`, which every caller already treats as a real fault
        (and which is what the MTP backend raises in the same situation).
        """
        if not self.mount.is_dir() or (
            self._device_id is not None and _device_id(self.mount) != self._device_id
        ):
            raise DeviceNotFound(f"the Kindle is no longer mounted at {self.mount}")
        (self.mount / path).unlink()

    def exists(self, path: str) -> bool:
        # FILES only, per `backend.DeviceBackend`'s contract. `.exists()` answered
        # `True` for a directory, which the MTP backend can never do (it only ever
        # sees files), so a caller written against one backend broke against the other.
        #
        # The SAME "device gone" check `list_files` already does, repeated here: a
        # bare `Path.is_file()` swallows `OSError` internally and answers `False` for
        # an unmounted volume exactly as it would for a file that genuinely never
        # existed — a caller verifying a just-written file (e.g. Kindle thumbnail
        # install) cannot tell "verified absent" from "the device vanished mid-check"
        # without this raising instead of answering a possibly-wrong `False`.
        if not self.mount.is_dir() or (
            self._device_id is not None and _device_id(self.mount) != self._device_id
        ):
            raise FileNotFoundError(f"the Kindle is no longer mounted at {self.mount}")
        return (self.mount / path).is_file()

    def free_space(self) -> int:
        return shutil.disk_usage(self.mount).free

    def eject(self) -> None:
        _run(["sync"])
        if platform.system() == "Darwin":
            _eject_macos(self.mount)
        else:
            _eject_linux(self.mount)

    def close(self) -> None:
        pass


def _device_id(mount: Path) -> int | None:
    """`st_dev` for the mount, or None when it cannot be read — in which case
    `list_files`' own `is_dir()` check is what refuses the listing anyway."""
    try:
        return mount.stat().st_dev
    except OSError:
        return None


def _run(argv: list[str], *, text: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=text, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as error:
        raise RuntimeError(f"{argv[0]} failed: {error}") from error


def _run_with_retry(argv: list[str]) -> None:
    result = _run(argv)
    if result.returncode != 0 and "busy" in (result.stderr or "").lower():
        result = _run(argv)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {(result.stderr or '').strip()}")


def _parent_disk_macos(mount: Path) -> str:
    result = _run(["diskutil", "info", "-plist", str(mount)], text=False)
    try:
        info = plistlib.loads(result.stdout) if result.stdout else {}
    except (ValueError, TypeError):
        info = {}
    return info.get("ParentWholeDisk") or str(mount)


def _eject_macos(mount: Path) -> None:
    _run_with_retry(["diskutil", "eject", _parent_disk_macos(mount)])


def _device_for_mount(mount: Path, *, mounts_file: Path = Path("/proc/mounts")) -> str:
    try:
        lines = mounts_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return str(mount)
    for line in lines:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == str(mount):
            return fields[0]
    return str(mount)


def _eject_linux(mount: Path) -> None:
    device = _device_for_mount(mount)
    _run_with_retry(["udisksctl", "unmount", "-b", device])
    _run_with_retry(["udisksctl", "power-off", "-b", device])
