"""Find a connected Kindle and decide how to talk to it.

Detection never trusts a model table: a Kindle that exposes a disk is mass storage,
and a Kindle with no matching mount is MTP. Amazon moved the 2024 models and the
Scribe to MTP, but firmware updates have moved that line before.
"""

from __future__ import annotations

import platform
import plistlib
import subprocess
import xml.parsers.expat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

KINDLE_VENDOR_ID = 0x1949
MTP_PRODUCT_IDS = frozenset({0x9981})
_REQUIRED_DIRS = ("documents", "system")


class DeviceNotFound(RuntimeError):
    """No Kindle is connected, or it exposes nothing we can talk to."""


class DeviceBusy(RuntimeError):
    """Another program holds the device (MTP allows exactly one holder)."""


@dataclass(frozen=True)
class Device:
    """One connected Kindle, as detection found it.

    `model_hint` is the name the DEVICE reports over USB ("Kindle", "Kindle Scribe",
    ...), not a lookup in a model table — this module deliberately has none, because
    firmware has moved the mass-storage/MTP line before. It is `None` when the device
    reports no product name, and for a device resolved with `identify=False`. Nothing
    branches on it: it is reported in `ebook kindle status` for a human, and it is the
    second rung of `backup.device_key`'s fallback chain, which is the only rung that
    can tell two serial-less MTP Kindles apart (they have no mount to be named after).
    """

    serial: str | None
    product_id: int | None
    mode: str  # "mass_storage" | "mtp"
    mount: Path | None
    model_hint: str | None = None


def _list_usb_macos() -> list[dict]:
    try:
        raw = subprocess.run(
            ["ioreg", "-r", "-c", "IOUSBHostDevice", "-l", "-a"],
            capture_output=True,
            timeout=30,
        )
        entries = plistlib.loads(raw.stdout) if raw.stdout else []
    except (subprocess.SubprocessError, OSError, ValueError, xml.parsers.expat.ExpatError):
        # `ExpatError` is NOT a `ValueError`: `plistlib` answers `InvalidFileException`
        # (which is) for output that is empty or not a plist at all, and lets expat's
        # own error through for XML that stops part-way — a truncated `ioreg`, i.e. the
        # realistic failure. Without it, detection raised out of every `ebook kindle`
        # command as `internal_error`/exit 1 instead of `device_not_found`/exit 3.
        return []
    found = []
    for entry in entries if isinstance(entries, list) else []:
        vendor = entry.get("idVendor")
        if vendor == KINDLE_VENDOR_ID:
            found.append(
                {
                    "vendor_id": vendor,
                    "product_id": entry.get("idProduct"),
                    "serial": entry.get("USB Serial Number"),
                    "product": entry.get("USB Product Name"),
                }
            )
    return found


def _list_usb_linux() -> list[dict]:
    found = []
    for device in sorted(Path("/sys/bus/usb/devices").glob("*")):
        try:
            vendor = int((device / "idVendor").read_text().strip(), 16)
        except (OSError, ValueError):
            continue
        if vendor != KINDLE_VENDOR_ID:
            continue

        def read(name: str, device=device) -> str | None:
            try:
                return (device / name).read_text().strip()
            except OSError:
                return None

        product_id = read("idProduct")
        found.append(
            {
                "vendor_id": vendor,
                "product_id": int(product_id, 16) if product_id else None,
                "serial": read("serial"),
                "product": read("product"),
            }
        )
    return found


def list_usb_devices() -> list[dict]:
    return _list_usb_macos() if platform.system() == "Darwin" else _list_usb_linux()


def list_candidate_mounts() -> list[Path]:
    roots = (
        [Path("/Volumes")]
        if platform.system() == "Darwin"
        else [Path("/run/media") / Path.home().name, Path("/media") / Path.home().name]
    )
    mounts = []
    for root in roots:
        try:
            mounts.extend(sorted(p for p in root.iterdir() if p.is_dir()))
        except OSError:
            continue
    return mounts


def _looks_like_a_kindle(mount: Path) -> bool:
    try:
        return all((mount / name).is_dir() for name in _REQUIRED_DIRS)
    except OSError:
        return False


def find_device(
    *,
    usb_lister: Callable[[], list[dict]] | None = None,
    mount_lister: Callable[[], list[Path]] | None = None,
    identify: bool = True,
) -> Device:
    """The connected Kindle, or `DeviceNotFound`.

    `identify=False` answers the two questions that do not need the USB bus — is one
    attached, and in which mode — and skips enumerating it when a mount already
    answers both. The returned `Device` then carries no `serial`, `product_id` or
    `model_hint`, so it MUST NOT be handed to `backup.device_key`: a device that
    reports a serial would resolve to a different backup directory than the same
    device found the ordinary way. It exists for `doctor`, which runs at every session
    start, reports only the mode, and deliberately never prints a serial — the same
    "do not spend a probe that cannot change the answer" gate Ruling R55 applied to
    the `calibre-debug` driver probe. A device with no mount is MTP, and nothing but
    the bus can say so, so that probe still runs.
    """
    # The mount is listed FIRST so `identify=False` can return on it without touching
    # the bus. Neither lister has side effects, so the order is free.
    mounts = [m for m in (mount_lister or list_candidate_mounts)() if _looks_like_a_kindle(m)]
    if mounts and not identify:
        return Device(serial=None, product_id=None, mode="mass_storage", mount=mounts[0])

    # The default listers already filter by vendor before returning, but an
    # injected `usb_lister` (tests, or a future caller) is not required to — a
    # stray non-Kindle USB device must never be mistaken for one in MTP mode.
    usb = [
        entry
        for entry in (usb_lister or list_usb_devices)()
        if entry.get("vendor_id") == KINDLE_VENDOR_ID
    ]

    if mounts:
        first = usb[0] if usb else {}
        return Device(
            serial=first.get("serial"),
            product_id=first.get("product_id"),
            mode="mass_storage",
            mount=mounts[0],
            model_hint=first.get("product"),
        )
    if usb:
        first = usb[0]
        return Device(
            serial=first.get("serial"),
            product_id=first.get("product_id"),
            mode="mtp",
            mount=None,
            model_hint=first.get("product"),
        )
    raise DeviceNotFound(
        "no Kindle found: connect one over USB and unlock it. A 2024-or-later model "
        "(or a Scribe) shows no disk — that is expected, it speaks MTP."
    )
