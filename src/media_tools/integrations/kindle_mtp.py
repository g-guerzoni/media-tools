"""Drive an MTP Kindle from inside Calibre's own interpreter.

This file is NOT part of the importable package surface. It is executed as

    calibre-debug -e <this file> -- <ops file>

by `tasks.ebook.kindle.mtp.MtpBackend`, which is the only thing that runs it. Calibre
ships its own Python and its own `calibre` package; nothing here may import
`media_tools`, and nothing here may rely on anything outside the standard library and
`calibre`. Every `calibre` import is therefore function-level: the module body must
stay loadable under a plain interpreter so the package's tests can check this file's
protocol constants against the backend's copies without dragging Calibre in.

Protocol
--------
Input is one JSON object, ``{"v": 1, "ops": [...]}``. Each op is an object with an
``"op"`` key — ``list``, ``get``, ``put``, ``rm``, ``mkdir``, ``free`` or ``eject`` —
plus a device-relative POSIX ``"path"`` and, for ``get``/``put``, a local absolute
``"local"`` path. Output is ``RESULT_MARKER`` on its own line followed by ONE JSON
object, ``{"v": 1, "results": [...]}``, with one entry per op in the order given.
`calibre-debug` writes plenty of its own chatter to stdout, which is exactly why the
marker exists; the backend parses only what follows the LAST occurrence of it.

Per-op failures are reported inside ``results`` (``{"ok": false, "error": ...}``), not
by the exit code. The exit code describes the invocation as a whole:

    0  the invocation ran — inspect ``results`` for per-op outcomes
    1  the invocation itself failed (bad ops file, unexpected exception)
    2  no MTP device found
    3  the device is held by something else
    4  the device refused a write as read-only

======================================================================
FIRST-RUN VERIFICATION — READ THIS BEFORE TRUSTING ANY OF IT
======================================================================
No line of the Calibre-facing code below has ever run against a real MTP Kindle. The
signatures, attribute names and constants were read off a locally installed Calibre
9.15.0 by introspection (`inspect.signature`, `dis`), so the SHAPES are real, but the
RUNTIME BEHAVIOUR is entirely unverified. Re-verify each of the following the first
time a real MTP Kindle is attached, and correct this file rather than working around
it downstream:

1.  Driver import path — ``calibre.devices.mtp.driver.MTP_DEVICE`` and
    ``calibre.devices.scanner.DeviceScanner``. Verified to import under Calibre
    9.15.0; NOT verified under any other version, and Calibre has moved device
    plugins between modules before.
2.  Open sequence — ``MTP_DEVICE(None)`` → ``startup()`` → ``DeviceScanner().scan()``
    → ``detect_managed_devices(scanner.devices)`` → ``open(connected, library_uuid)``.
    Unverified: whether ``MTP_DEVICE(None)`` is a legal construction outside
    Calibre's plugin loader, what ``detect_managed_devices`` returns when nothing is
    connected (assumed falsy here), and whether ``open`` needs a real library uuid
    rather than the constant string this file passes.
3.  Storage root — ``dev.filesystem_cache.storage(dev._main_id)``. ``_main_id`` is a
    PRIVATE attribute set during ``open``; it is what Calibre's own ``upload_books``
    uses, but a private name can vanish without notice. The fallback here (first
    entry of the cache) has never run either.
4.  Uncached lookups — ``list_folder_by_name(parent, *names)`` returns a tuple of
    ``ListEntry(name, is_folder, size, mtime)``, and ``get_file_by_name(outfile,
    parent, *names)`` writes into an open binary stream. Both were confirmed to exist
    with those signatures. UNVERIFIED: what either one raises for a path that does not
    exist (this file treats any exception at the requested prefix as "not there"), and
    whether ``ListEntry.mtime`` really is a timezone-aware datetime — ``_epoch`` below
    guesses defensively.
5.  Writes — ``ensure_parent(storage, parts)`` creates every component except the
    LAST and returns the parent folder (confirmed by disassembly); ``put_file(parent,
    name, stream, size)`` then writes the file. UNVERIFIED: whether ``put_file``
    replaces an existing file of the same name by default (``replace=True`` is the
    declared default) and what it raises when the device is full.
6.  Deletes are limited, and this is a real functional gap. ``delete_file_or_folder``
    takes a ``FileOrFolder`` object, and the only way to obtain one is
    ``storage.find_path(parts)`` against the CACHED tree — which omits ``*.sdr``
    folders and ``system/``. Calibre 9.15 exposes no delete-by-name primitive
    (its own ``scan_sdr_for_kfx_files`` reads those paths with the uncached lookups
    but never deletes through them). So ``rm`` can delete an ordinary book and cannot
    delete anything the cached tree hides. Verify against a real device whether the
    cached tree is really that narrow, and if so decide whether removing an ``.sdr``
    sidecar needs a different primitive.
7.  Exit-code mapping — this file decides "no device" / "busy" / "read-only" by
    catching ``calibre.devices.errors`` classes and, failing that, by matching
    substrings in the exception text (``_classify`` below). The substring list is a
    guess. Note the real error text a device produces and replace the guesses.
8.  Free space — ``free_space()`` is documented by the DevicePlugin API to return a
    three-element list (main, card A, card B); this file accepts either that or a bare
    integer. Confirm which one an MTP Kindle actually returns.
9.  Eject — MTP has no eject: the session is simply closed (``shutdown()``). Confirm
    the device is left in a clean state and the host does not need anything further.
10. Content scope — unlike the mass-storage backend, listing here does NOT skip
    ``audible/`` or restrict ``system/`` to ``thumbnails/``; it returns whatever the
    requested prefix contains. That divergence is deliberate for now (the uncached
    lookups exist precisely so ``system/thumbnails/`` is reachable) but it means a
    caller that walks the whole device over MTP sees more than it would over mass
    storage. Decide the parity rule before the first real backup runs.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import traceback

PROTOCOL_VERSION = 1

# Printed on its own line immediately before the JSON result. `calibre-debug` prints
# its own banner and progress chatter to stdout, and a Calibre plugin may print more
# at any point, so the result has to be findable rather than assumed to be the whole
# of stdout. The backend keeps its own copy of this string and a test pins the two
# together, because it cannot import this module to share the constant.
RESULT_MARKER = "@@media-tools-mtp-result-v1@@"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_DEVICE = 2
EXIT_BUSY = 3
EXIT_WRITE_PROTECTED = 4

# What this file falls back on when an exception is not one of the `calibre.devices.
# errors` classes it knows: a substring match against the exception text. Every one of
# these is a guess (see FIRST-RUN VERIFICATION item 7).
_BUSY_MARKERS = ("busy", "in use", "another application", "access denied", "lock")
_NO_DEVICE_MARKERS = ("no device", "not found", "no mtp", "disconnected", "unplugged")
_READ_ONLY_MARKERS = ("read-only", "read only", "write protect", "not writable", "readonly")

_LIBRARY_UUID = "media-tools"


class _HelperError(Exception):
    """An invocation-level failure, carrying the exit code it maps to."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _split(path: str) -> list[str]:
    """A device-relative POSIX path to its components, tolerating a leading or
    trailing slash and refusing to walk upwards."""
    parts = [part for part in str(path or "").split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise ValueError(f"path escapes the device root: {path!r}")
    return parts


def _epoch(value) -> float:
    """`ListEntry.mtime` is whatever Calibre's `convert_timestamp` produced — a
    timezone-aware datetime as far as the disassembly shows, but this has never run.
    Accept a datetime, a number, or nothing at all."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value.timestamp())
    except Exception:
        return 0.0


def _classify(error: Exception) -> int:
    """Map a Calibre device exception onto one of this helper's exit codes."""
    try:
        from calibre.devices import errors as calibre_errors
    except Exception:  # pragma: no cover - only reachable outside calibre-debug
        calibre_errors = None

    if calibre_errors is not None:
        for name, code in (
            ("DeviceBusy", EXIT_BUSY),
            ("DeviceLocked", EXIT_BUSY),
            ("OpenActionNeeded", EXIT_BUSY),
            ("OpenFailed", EXIT_NO_DEVICE),
            ("InitialConnectionError", EXIT_NO_DEVICE),
        ):
            klass = getattr(calibre_errors, name, None)
            if klass is not None and isinstance(error, klass):
                return code

    text = f"{type(error).__name__}: {error}".lower()
    if any(marker in text for marker in _READ_ONLY_MARKERS):
        return EXIT_WRITE_PROTECTED
    if any(marker in text for marker in _BUSY_MARKERS):
        return EXIT_BUSY
    if any(marker in text for marker in _NO_DEVICE_MARKERS):
        return EXIT_NO_DEVICE
    return EXIT_FAILED


# --- the device session ---------------------------------------------------------


def _open_device():
    """`startup` -> `DeviceScanner().scan()` -> `detect_managed_devices` -> `open`,
    the sequence Calibre's own device manager uses. See FIRST-RUN VERIFICATION 1-2."""
    from calibre.devices.mtp.driver import MTP_DEVICE
    from calibre.devices.scanner import DeviceScanner

    device = MTP_DEVICE(None)
    device.startup()
    try:
        scanner = DeviceScanner()
        scanner.scan()
        connected = device.detect_managed_devices(scanner.devices)
        if not connected:
            raise _HelperError(
                "no MTP device found: connect a Kindle over USB and unlock it",
                EXIT_NO_DEVICE,
            )
        device.open(connected, _LIBRARY_UUID)
    except _HelperError:
        _shutdown(device)
        raise
    except Exception as error:
        _shutdown(device)
        raise _HelperError(f"could not open the MTP device: {error}", _classify(error)) from error
    return device


def _shutdown(device) -> None:
    # Closing the session is the whole of "eject" for MTP, and a failure here must
    # never mask whatever the caller was actually reporting.
    with contextlib.suppress(Exception):
        device.shutdown()


def _storage(device):
    """The main storage root as a `FileOrFolder`. `_main_id` is private but is what
    Calibre's own `upload_books` uses; see FIRST-RUN VERIFICATION 3."""
    cache = device.filesystem_cache
    storage_id = getattr(device, "_main_id", None)
    if storage_id is not None:
        return cache.storage(storage_id)
    entries = list(getattr(cache, "entries", []))
    if not entries:
        raise _HelperError("the device reported no storage", EXIT_NO_DEVICE)
    return entries[0]


# --- the operations -------------------------------------------------------------


def _walk(device, storage, parts: list[str], out: list[dict]) -> None:
    """Recursive listing through the UNCACHED `list_folder_by_name`. The cached
    filesystem tree omits `*.sdr` folders and `system/`, both of which this project
    needs, so the cache is not an option here (see FIRST-RUN VERIFICATION 4)."""
    for entry in device.list_folder_by_name(storage, *parts):
        child = parts + [entry.name]
        if entry.is_folder:
            _walk(device, storage, child, out)
        else:
            out.append(
                {
                    "path": "/".join(child),
                    "size": int(entry.size or 0),
                    "mtime": _epoch(entry.mtime),
                }
            )


def _op_list(device, op: dict) -> dict:
    storage = _storage(device)
    parts = _split(op.get("path", ""))
    files: list[dict] = []
    try:
        _walk(device, storage, parts, files)
    except Exception as error:
        # A prefix that is not there is not an error — the mass-storage backend
        # returns an empty list for a missing directory and callers rely on that.
        # Anything deeper than the requested prefix would have surfaced as a real
        # failure; this file cannot tell the two apart until item 4 is verified.
        return {"op": "list", "ok": True, "files": files, "note": str(error)}
    return {"op": "list", "ok": True, "files": files}


def _op_get(device, op: dict) -> dict:
    storage = _storage(device)
    parts = _split(op["path"])
    local = op["local"]
    parent = os.path.dirname(local)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(local, "wb") as outfile:
        device.get_file_by_name(outfile, storage, *parts)
    return {"op": "get", "ok": True, "size": os.path.getsize(local)}


def _op_put(device, op: dict) -> dict:
    storage = _storage(device)
    parts = _split(op["path"])
    if not parts:
        raise ValueError("put needs a file path, not the device root")
    local = op["local"]
    size = os.path.getsize(local)
    parent = device.ensure_parent(storage, parts)
    with open(local, "rb") as stream:
        device.put_file(parent, parts[-1], stream, size)
    return {"op": "put", "ok": True, "size": size}


def _op_rm(device, op: dict) -> dict:
    storage = _storage(device)
    parts = _split(op["path"])
    if not parts:
        raise ValueError("rm needs a path, not the device root")
    target = storage.find_path(parts)
    if target is None:
        # See FIRST-RUN VERIFICATION 6: `find_path` walks the CACHED tree, which
        # omits `*.sdr` folders and `system/`, so "not found" here does not prove the
        # path is absent from the device.
        return {
            "op": "rm",
            "ok": False,
            "error": (
                f"{op['path']} is not in the device's cached file tree — it is either "
                "absent, or one of the paths (a *.sdr folder, anything under system/) "
                "that Calibre's cached tree does not expose for deletion"
            ),
        }
    device.delete_file_or_folder(target)
    return {"op": "rm", "ok": True}


def _op_mkdir(device, op: dict) -> dict:
    storage = _storage(device)
    parts = _split(op["path"])
    if not parts:
        return {"op": "mkdir", "ok": True}
    # `ensure_parent` creates every component but the last, so append a sentinel to
    # have the whole requested path created.
    device.ensure_parent(storage, parts + ["_"])
    return {"op": "mkdir", "ok": True}


def _op_free(device, op: dict) -> dict:
    free = device.free_space()
    if isinstance(free, (list, tuple)):
        free = free[0] if free else 0
    return {"op": "free", "ok": True, "free": int(free or 0)}


def _op_eject(device, op: dict) -> dict:
    # MTP has nothing to eject: closing the session is the whole of it, and `main`
    # shuts the device down on the way out regardless.
    return {"op": "eject", "ok": True}


_OPS = {
    "list": _op_list,
    "get": _op_get,
    "put": _op_put,
    "rm": _op_rm,
    "mkdir": _op_mkdir,
    "free": _op_free,
    "eject": _op_eject,
}

_WRITE_OPS = frozenset({"put", "rm", "mkdir"})


def _run_ops(device, ops: list) -> list[dict]:
    results: list[dict] = []
    for op in ops:
        name = op.get("op") if isinstance(op, dict) else None
        handler = _OPS.get(name)
        if handler is None:
            results.append({"op": name, "ok": False, "error": f"unknown op: {name!r}"})
            continue
        try:
            results.append(handler(device, op))
        except _HelperError:
            raise
        except Exception as error:
            code = _classify(error)
            if code == EXIT_WRITE_PROTECTED and name in _WRITE_OPS:
                # A read-only device fails the whole batch, not just this op: every
                # remaining write would fail the same way.
                _emit(results)
                raise _HelperError(f"the device refused the write: {error}", code) from error
            results.append({"op": name, "ok": False, "error": f"{type(error).__name__}: {error}"})
    return results


# --- entry point ----------------------------------------------------------------


def _emit(results: list[dict]) -> None:
    """Marker on its own line, then the result object on one line. The leading
    newline guarantees the marker starts a line even if chatter left one open."""
    sys.stdout.write("\n" + RESULT_MARKER + "\n")
    sys.stdout.write(json.dumps({"v": PROTOCOL_VERSION, "results": results}) + "\n")
    sys.stdout.flush()


def _read_ops(path: str) -> list:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise _HelperError("the ops file must hold a JSON object", EXIT_FAILED)
    if payload.get("v") != PROTOCOL_VERSION:
        raise _HelperError(
            f"ops file protocol version {payload.get('v')!r}, expected {PROTOCOL_VERSION}",
            EXIT_FAILED,
        )
    ops = payload.get("ops")
    if not isinstance(ops, list):
        raise _HelperError("the ops file must hold a list under 'ops'", EXIT_FAILED)
    return ops


def main(argv: list) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: calibre-debug -e kindle_mtp.py -- <ops file>\n")
        return EXIT_FAILED
    try:
        ops = _read_ops(argv[0])
    except _HelperError as error:
        sys.stderr.write(str(error) + "\n")
        return error.code
    except Exception as error:
        sys.stderr.write(f"could not read the ops file: {error}\n")
        return EXIT_FAILED

    device = None
    try:
        device = _open_device()
        _emit(_run_ops(device, ops))
        return EXIT_OK
    except _HelperError as error:
        sys.stderr.write(str(error) + "\n")
        return error.code
    except Exception as error:
        traceback.print_exc()
        return _classify(error)
    finally:
        if device is not None:
            _shutdown(device)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
