"""The protocol every Kindle backend implements, and the naming rules a device's own
filesystem enforces regardless of which backend writes to it.

Mass storage and MTP talk to the device in unrelated ways — one is a mounted
filesystem, the other a media-transfer session — but a caller (backup, sync) must
not have to care which one it is driving. Every backend, `massstorage.py`'s
`MassStorageBackend` here and the MTP backend a later task adds, implements this
same shape, and every path either one hands back is device-relative and
POSIX-style, never an absolute host path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from media_tools.core.paths import truncate_name

# Characters no FAT32 filename may contain (backslash, forward slash, colon,
# asterisk, question mark, double quote, angle brackets, pipe) plus control
# characters. Forward slash is stripped for the same reason as everywhere else in
# this file's device-relative paths: it is a path separator, not a name character.
_FAT32_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


@dataclass(frozen=True)
class DeviceFile:
    """One file already on the device. `path` is device-relative and POSIX-style
    (`documents/en/Book.azw3`), never an absolute host path and never a Windows
    separator, regardless of which backend produced it."""

    path: str
    size: int
    mtime: float


class DeviceBackend(Protocol):
    """The contract both backends conform to. A caller must not be able to tell which
    one it is driving, so every rule below holds for mass storage AND for MTP — where
    they used to differ (an absent path raising `FileNotFoundError` on one and a
    backend-specific error on the other; `exists` answering `True` for a directory on
    one and `False` on the other) the divergence was the bug, not the contract.

    - **Paths** are device-relative and POSIX-style, never absolute host paths.
    - **`list_files(prefix)`** returns FILES only, never directories, depth-first with
      each directory's entries in name order. A prefix that is not on the device — or
      one inside the excluded areas below — returns `[]` rather than raising.
    - **Excluded from every listing**, on both backends: `audible/` at any depth
      (Amazon's audiobook data, untouchable by this whole plan) and everything under a
      `system/` directory except its `thumbnails/` child (device internals — Wi-Fi
      credentials, logs, settings — not book content), plus volume litter. The
      constants live in `massstorage.py` and the MTP backend imports them, so the two
      cannot drift apart.
    - **`read(path, dest)`** and **`remove(path)`** raise `FileNotFoundError` when
      `path` is not on the device.
    - **`exists(path)`** answers for FILES only: a directory is not "there".
    - **`write(local, path)`** creates any missing parent directories.
    - **`free_space()`** is bytes free on the device's main storage.
    - **`close()`** releases whatever the backend holds, including any cached listing.
    """

    def list_files(self, prefix: str = "") -> list[DeviceFile]: ...
    def read(self, path: str, dest: Path) -> None: ...
    def write(self, local: Path, path: str) -> None: ...
    def remove(self, path: str) -> None: ...
    def exists(self, path: str) -> bool: ...
    def free_space(self) -> int: ...
    def eject(self) -> None: ...
    def close(self) -> None: ...


class DeviceWriteProtected(RuntimeError):
    """The device refused a write outright — locked, or mounted read-only —
    rather than failing with an ordinary OSError a caller already knows how to
    handle."""


def sanitize_device_name(name: str, *, max_path: int) -> str:
    """A single device-safe filename: strips the characters FAT32 rejects and a
    trailing dot or space FAT32 would silently drop itself, then caps the *name
    component* at `max_path`, preserving the extension, via
    `core.paths.truncate_name`. Capping a full device path is the caller's job
    once it joins a directory onto this name — that lands in a later task, so
    `max_path` here only bounds this one component (250 for mass storage, 230
    for MTP, per the device's own filename limit, not the whole path budget).
    """
    dot = name.rfind(".")
    stem, suffix = (name[:dot], name[dot:]) if dot > 0 else (name, "")
    stem = _FAT32_ILLEGAL.sub("", stem).rstrip(". ")
    suffix = _FAT32_ILLEGAL.sub("", suffix)
    return truncate_name(f"{stem}{suffix}", max_path)
