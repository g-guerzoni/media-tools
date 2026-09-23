"""The mass-storage Kindle backend: the device mounts as an ordinary disk, so every
operation is a filesystem call — no MTP session, no vendor protocol.

`write()` follows this project's usual atomicity rule (`core.paths.temp_path` +
`fsync_replace`, spec 7.1/7.5's "partial outputs never count as done"): a Kindle
pulled mid-copy must never leave a truncated book sitting at its final name, and any
failure anywhere in that staged write — the copy itself or the final replace — must
leave no `.partial` file behind either. `list_files` skips the macOS/Linux volume
litter every removable disk accumulates, never descends into `audible/` (Amazon's
audiobook data, untouchable by this whole plan), and only descends into `system/`
as far as `system/thumbnails/` — the rest of `system/` is device internals (Wi-Fi
credentials, logs, settings), not book content.
"""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
from pathlib import Path

from media_tools.core.paths import fsync_replace, temp_path
from media_tools.tasks.ebook.kindle.backend import DeviceFile

# These four are PUBLIC on purpose: `mtp.py` imports them rather than re-deriving the
# same rules, so the two backends' listings cannot drift apart. `backend.py`'s protocol
# docstring is where the rule itself is written down.
VOLUME_LITTER = {".Trashes", ".fseventsd", ".Spotlight-V100"}
# Fully off-limits, at any depth: `audible/` is Amazon's audiobook data, untouchable
# by this whole plan. `system/` is different — only `system/thumbnails/` is ordinary
# cache data; everything else under `system/` is device internals, not book content.
PROTECTED_DIRS = {"audible"}
RESTRICTED_PARENT = "system"
RESTRICTED_EXCEPTION = "thumbnails"


def is_volume_litter(name: str) -> bool:
    return name.startswith("._") or name in VOLUME_LITTER


def prefix_targets_a_forbidden_system_child(parts: tuple[str, ...]) -> bool:
    return len(parts) >= 2 and parts[0] == RESTRICTED_PARENT and parts[1] != RESTRICTED_EXCEPTION


class MassStorageBackend:
    """Talks to a Kindle mounted as a disk at `mount` (a `Device.mount` from
    `detect.find_device` in mass-storage mode)."""

    def __init__(self, mount: Path) -> None:
        self.mount = mount

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
        shutil.copyfile(self.mount / path, dest)

    def read_many(self, items: list[tuple[str, Path]]) -> None:
        """`read` for a whole batch — see `backend.DeviceBackend`. A mounted disk has
        no round trip to save, so this is the loop the MTP backend cannot afford; what
        it adds over calling `read` directly is the contract's two guarantees, neither
        of which a bare `shutil.copyfile` makes: the destination's parent directory is
        created, and a pair that fails leaves NO file at its destination.

        The second one is why the copy is staged through `temp_path` rather than
        written straight to `dest`. MTP's helper already deletes a half-fetched local
        file; without staging here, a mid-copy failure (a full disk, a device yanked
        between two books) would leave mass storage holding a TRUNCATED file at the
        final name while MTP held none — a parity gap that only shows up in the one
        situation the backup exists for.
        """
        for path, dest in items:
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            temp = temp_path(dest)
            try:
                self.read(path, temp)
                temp.replace(dest)
            except BaseException:
                temp.unlink(missing_ok=True)
                raise

    def write(self, local: Path, path: str) -> None:
        target = self.mount / path
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = temp_path(target)
        try:
            shutil.copyfile(local, temp)
            fsync_replace(temp, target)
        except Exception:
            temp.unlink(missing_ok=True)
            raise

    def remove(self, path: str) -> None:
        (self.mount / path).unlink()

    def exists(self, path: str) -> bool:
        # FILES only, per `backend.DeviceBackend`'s contract. `.exists()` answered
        # `True` for a directory, which the MTP backend can never do (it only ever
        # sees files), so a caller written against one backend broke against the other.
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
