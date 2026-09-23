"""The mass-storage Kindle backend: the device mounts as an ordinary disk, so every
operation is a filesystem call — no MTP session, no vendor protocol.

`write()` follows this project's usual atomicity rule (`core.paths.temp_path` +
`fsync_replace`, spec 7.1/7.5's "partial outputs never count as done"): a Kindle
pulled mid-copy must never leave a truncated book sitting at its final name, and a
copy that fails partway must leave no `.partial` file behind either. `list_files`
skips the macOS/Linux volume litter every removable disk accumulates and never
descends into `audible/` — that folder is Amazon's audiobook data, untouchable by
this whole plan, not ordinary ebook content.
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

_VOLUME_LITTER = {".Trashes", ".fseventsd", ".Spotlight-V100"}
_PROTECTED_DIRS = {"audible"}


def _is_volume_litter(name: str) -> bool:
    return name.startswith("._") or name in _VOLUME_LITTER


class MassStorageBackend:
    """Talks to a Kindle mounted as a disk at `mount` (a `Device.mount` from
    `detect.find_device` in mass-storage mode)."""

    def __init__(self, mount: Path) -> None:
        self.mount = mount

    def list_files(self, prefix: str = "") -> list[DeviceFile]:
        if prefix and _PROTECTED_DIRS & set(Path(prefix).parts):
            return []
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
        for entry in entries:
            if _is_volume_litter(entry.name):
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name in _PROTECTED_DIRS:
                    continue
                yield from self._walk(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                stat = entry.stat()
                relative = Path(entry.path).relative_to(self.mount).as_posix()
                yield DeviceFile(path=relative, size=stat.st_size, mtime=stat.st_mtime)

    def read(self, path: str, dest: Path) -> None:
        shutil.copyfile(self.mount / path, dest)

    def write(self, local: Path, path: str) -> None:
        target = self.mount / path
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = temp_path(target)
        try:
            shutil.copyfile(local, temp)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        fsync_replace(temp, target)

    def remove(self, path: str) -> None:
        (self.mount / path).unlink()

    def exists(self, path: str) -> bool:
        return (self.mount / path).exists()

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


def _device_for_mount(mount: Path) -> str:
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
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
