"""Drive an MTP Kindle from inside Calibre's own interpreter.

This file is NOT part of the importable package surface. It is executed as

    calibre-debug -e <this file> -- <ops file>

by `tasks.ebook.kindle.mtp.MtpBackend`, which is the only thing that runs it. Calibre
ships its own Python and its own `calibre` package; nothing here may import
`media_tools`, and nothing here may rely on anything outside the standard library and
`calibre`. Every `calibre` import is therefore function-level: the module body must
stay loadable under a plain interpreter, which is how the package's tests exercise the
whole op layer against a stub device and pin these protocol constants against the
backend's copies.

Protocol
--------
Input is one JSON object::

    {"v": 1, "serial": "<expected device serial or null>", "ops": [...]}

Each op is an object with an ``"op"`` key — ``list``, ``get``, ``put``, ``rm``,
``mkdir``, ``free`` or ``eject`` — plus a device-relative POSIX ``"path"`` and, for
``get``/``put``, a local absolute ``"local"`` path.

Output is two marker lines. ``START_MARKER`` is printed before anything else, so a
non-zero exit WITHOUT it means `calibre-debug` never reached this script at all (a
broken invocation) rather than anything about the device. The result is then
``RESULT_MARKER`` immediately followed, ON THE SAME LINE, by one JSON object::

    {"v": 1, "device": {...}, "results": [...]}

with one entry in ``results`` per op, in the order given. Marker and payload share a
line so the backend can take the last line that starts with the marker: `calibre-debug`
and Calibre's plugins print freely to stdout both before and after, and a separator
line between marker and payload would be one more thing to interleave with.

Per-op failures are reported inside ``results`` (``{"ok": false, "code": ..., "error":
...}``), not by the exit code. The exit code describes the invocation as a whole:

    0  the invocation ran — inspect ``results`` for per-op outcomes
    1  the invocation itself failed (bad ops file, unexpected exception)
    2  no MTP device found, or a DIFFERENT device than the caller asked for
    3  the device is held by something else
    4  the device refused a write as read-only (the results so far are still emitted)

======================================================================
FIRST-RUN VERIFICATION — READ THIS BEFORE TRUSTING ANY OF IT
======================================================================
No line of the DEVICE-facing code below has ever run against a real MTP Kindle. The
signatures, attribute names and constants were read off a locally installed Calibre
9.15.0 by introspection (`inspect.signature`, `dis`), so the SHAPES are real, but the
runtime behaviour of everything that touches a device is unverified. Re-verify each of
the following the first time a real MTP Kindle is attached, and correct this file
rather than working around it downstream.

ALREADY VERIFIED, by running this script under `calibre-debug` with NO device
attached (`calibre-debug -e kindle_mtp.py -- <ops file>`, isolated
CALIBRE_CONFIG_DIRECTORY, Calibre 9.15.0 / macOS): the `-e ... -- <args>` invocation
convention and `sys.argv[1:]`; `_read_ops`; `_emit_start`; the `MTP_DEVICE(None)`
construction; `startup()`; `DeviceScanner().scan()`; `detect_managed_devices`
returning falsy when nothing is connected; and the resulting exit 2 reaching the
backend as `DeviceNotFound` with the start marker present. Nothing past
`detect_managed_devices` has run.

1.  **SERIAL MATCHING — verify this first; it is the guard on everything else.**
    Calibre's MTP driver is a GENERAL MTP driver, not a Kindle driver, and this code
    path deletes files. `_device_serial` reads ``current_serial_num`` (seen in
    ``open``'s disassembly) and falls back to ``get_device_uid()``. Confirm that at
    least one of them yields a value, and that it is the SAME string
    ``detect.find_device`` reports from the USB layer — if the two spell the serial
    differently, every invocation will refuse to run. When neither yields anything,
    this file proceeds unguarded and says so in ``device.checked``; decide whether
    that is acceptable before running any destructive command.
2.  Driver import path — ``calibre.devices.mtp.driver.MTP_DEVICE`` and
    ``calibre.devices.scanner.DeviceScanner``. Verified to import and construct under
    Calibre 9.15.0 on macOS; NOT verified under any other version or platform, and
    Calibre has moved device plugins between modules before.
3.  Open sequence — everything up to and including ``detect_managed_devices`` is
    verified (see above). **``open(connected, library_uuid)`` has never run**: it is
    unknown whether it accepts the constant string this file passes for
    ``library_uuid`` rather than a real library UUID, and unknown what it raises when
    the device is present but locked. This is the first line that needs a device.
4.  Storage root — ``dev.filesystem_cache.storage(dev._main_id)``. ``_main_id`` is a
    PRIVATE attribute set during ``open``; it is what Calibre's own ``upload_books``
    uses, but a private name can vanish without notice. The fallback here (first
    entry of the cache) has never run either.
5.  Uncached lookups — ``list_folder_by_name(parent, *names)`` returns a tuple of
    ``ListEntry(name, is_folder, size, mtime)``, and ``get_file_by_name(outfile,
    parent, *names)`` writes into an open binary stream. Both were confirmed to exist
    with those signatures. UNVERIFIED: **what either one raises for a folder that does
    not exist**, which is the single assumption `_op_list` cannot make safely — it
    treats a raise at a NON-ROOT requested prefix as "not there" (matching mass
    storage's empty-list-for-a-missing-directory) but a raise at the device root, or
    anywhere deeper in the walk, as a real failure, because reporting a transient
    error as an empty device is how a backup silently writes nothing and calls it a
    success. If the real API returns an empty tuple for a missing folder instead of
    raising, tighten this to treat EVERY raise as a failure. Also unverified: whether
    ``ListEntry.mtime`` really is a timezone-aware datetime — ``_epoch`` guesses.
6.  **Write atomicity is an open question.** ``put_file(parent, name, stream, size)``
    writes straight to the final name; there is no staging primitive here and none was
    invented. So an interrupted transfer may leave a short file at the real name. This
    file no longer reports the LOCAL size as the written size — it re-reads the file
    through the uncached lookup and reports what the DEVICE says, so a short write is
    visible rather than disguised. Confirm on real hardware: what ``put_file`` leaves
    behind when the transfer is cut, whether it replaces a same-named file by default
    (``replace=True`` is the declared default), and what it raises when the device is
    full. Task 7's verify stage is what actually catches a truncated book.
7.  Parent creation — ``ensure_parent(storage, parts)`` creates every component except
    the LAST and returns the parent folder (confirmed by disassembly). ``_op_mkdir``
    therefore appends a sentinel component to have the whole requested path created;
    confirm the sentinel is never itself created.
8.  Deletes are limited, and this is a real functional gap. ``delete_file_or_folder``
    takes a ``FileOrFolder`` object, and the only way to obtain one is
    ``storage.find_path(parts)`` against the CACHED tree — which omits ``*.sdr``
    folders and ``system/``. Calibre 9.15 exposes no delete-by-name primitive (its own
    ``scan_sdr_for_kfx_files`` reads those paths with the uncached lookups but never
    deletes through them). So ``rm`` can delete an ordinary book and cannot delete
    anything the cached tree hides; that case returns ``code: "not_in_cached_tree"``,
    which the backend raises as a named exception. Verify whether the cached tree is
    really that narrow.
9.  Exit-code mapping — ``_classify`` first catches ``calibre.devices.errors`` classes
    and otherwise matches substrings in the exception text. **The substring list is a
    guess.** Note in particular that read-only classification is gated on the op
    actually being a write: before that gate, a `put` whose error message merely
    contained a book title or local path with "read only" in it would abort the batch
    and tell the user their Kindle was write-protected. Record the real error text a
    device produces and replace the guesses.
10. Free space — ``free_space()`` is documented by the DevicePlugin API to return a
    three-element list (main, card A, card B); this file accepts either that or a bare
    integer. Confirm which one an MTP Kindle actually returns.
11. Eject — MTP has no eject: the session is simply closed (``shutdown()``). Confirm
    the device is left in a clean state and the host does not need anything further.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import traceback

PROTOCOL_VERSION = 1

# Printed before anything else, so a non-zero exit WITHOUT it is a `calibre-debug`
# invocation failure rather than a device verdict.
START_MARKER = "@@media-tools-mtp-start-v1@@"
# Printed immediately before the JSON result, on the SAME line as it.
RESULT_MARKER = "@@media-tools-mtp-result-v1@@"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_DEVICE = 2
EXIT_BUSY = 3
EXIT_WRITE_PROTECTED = 4

# What `_classify` falls back on when an exception is not one of the
# `calibre.devices.errors` classes it knows. Every one of these is a guess (see
# FIRST-RUN VERIFICATION item 9).
_BUSY_MARKERS = ("busy", "in use", "another application", "access denied", "lock")
_NO_DEVICE_MARKERS = ("no device", "not found", "no mtp", "disconnected", "unplugged")
_READ_ONLY_MARKERS = ("read-only", "read only", "write protect", "not writable", "readonly")

_LIBRARY_UUID = "media-tools"
_WRITE_OPS = frozenset({"put", "rm", "mkdir"})


class _HelperError(Exception):
    """An invocation-level failure, carrying the exit code it maps to and any
    per-op results already collected (so an aborted batch still reports what landed)."""

    def __init__(self, message: str, code: int, results: list | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.results = results or []


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


def _classify(error: Exception, *, is_write: bool = False) -> int:
    """Map a device exception onto one of this helper's exit codes.

    `is_write` gates the read-only verdict: only an op that actually writes may be
    classified as "the device is read-only". Without that gate, a `put` failing for an
    unrelated reason whose message happened to contain "read only" — a book title, a
    local path — would abort the whole batch and tell the user their Kindle is
    write-protected.
    """
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
    if is_write and any(marker in text for marker in _READ_ONLY_MARKERS):
        return EXIT_WRITE_PROTECTED
    if any(marker in text for marker in _BUSY_MARKERS):
        return EXIT_BUSY
    if any(marker in text for marker in _NO_DEVICE_MARKERS):
        return EXIT_NO_DEVICE
    return EXIT_FAILED


# --- the device session ---------------------------------------------------------


def _open_device():
    """`startup` -> `DeviceScanner().scan()` -> `detect_managed_devices` -> `open`,
    the sequence Calibre's own device manager uses. See FIRST-RUN VERIFICATION 2-3."""
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


def _device_serial(device) -> str | None:
    """The serial of the device actually opened. See FIRST-RUN VERIFICATION 1."""
    value = getattr(device, "current_serial_num", None)
    if value:
        return str(value)
    getter = getattr(device, "get_device_uid", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:
            value = None
        if value:
            return str(value)
    return None


def _check_serial(device, expected: str | None) -> dict:
    """Refuse to touch a device that is not the one the caller asked for.

    Calibre's MTP driver is a general MTP driver and `detect_managed_devices` returns
    whatever it finds; this code path deletes files. When the driver reports no serial
    at all the run proceeds — there is nothing to compare — but `checked` says so
    rather than implying a match.
    """
    found = _device_serial(device)
    info = {"serial": found, "expected": expected, "checked": bool(expected and found)}
    if expected and found and found != expected:
        raise _HelperError(
            f"a different MTP device is attached: expected serial {expected!r}, "
            f"found {found!r}. Nothing was touched.",
            EXIT_NO_DEVICE,
        )
    if expected and not found:
        info["note"] = "the driver reported no serial, so the device could not be verified"
    return info


def _storage(device):
    """The main storage root as a `FileOrFolder`. `_main_id` is private but is what
    Calibre's own `upload_books` uses; see FIRST-RUN VERIFICATION 4."""
    cache = device.filesystem_cache
    storage_id = getattr(device, "_main_id", None)
    if storage_id is not None:
        return cache.storage(storage_id)
    entries = list(getattr(cache, "entries", []))
    if not entries:
        raise _HelperError("the device reported no storage", EXIT_NO_DEVICE)
    return entries[0]


# --- the operations -------------------------------------------------------------


def _entries(device, storage, parts: list[str]) -> list:
    """One folder's entries through the UNCACHED `list_folder_by_name`, sorted by name.

    The cached filesystem tree omits `*.sdr` folders and `system/`, both of which this
    project needs, so the cache is not an option here. Sorting matches
    `massstorage._walk`, which sorts each directory's entries by name, so both backends
    return a listing in the same order.
    """
    return sorted(device.list_folder_by_name(storage, *parts), key=lambda entry: entry.name)


def _walk(device, storage, parts: list[str], entries: list, out: list) -> None:
    for entry in entries:
        child = parts + [entry.name]
        if entry.is_folder:
            _walk(device, storage, child, _entries(device, storage, child), out)
        else:
            out.append(
                {
                    "path": "/".join(child),
                    "size": int(entry.size or 0),
                    "mtime": _epoch(entry.mtime),
                }
            )


def _op_list(device, op: dict) -> dict:
    """Recursive listing.

    The failure handling here is the whole point of the op. A listing that reports
    "the device is empty" when it actually failed is worse than an error: the backend
    caches it, a backup writes nothing and calls itself a success, and a later sync
    sees an empty device. So only ONE case is allowed to come back as a clean empty
    listing — a non-root prefix that is not there, which is what the mass-storage
    backend's empty-list-for-a-missing-directory behaviour means. A failure at the
    device ROOT (which always exists) or anywhere deeper in the walk is `ok: false`.
    """
    storage = _storage(device)
    parts = _split(op.get("path", ""))
    try:
        top = _entries(device, storage, parts)
    except Exception as error:
        if not parts:
            return {
                "op": "list",
                "ok": False,
                "code": "list_failed",
                "error": f"could not list the device root: {type(error).__name__}: {error}",
            }
        return {"op": "list", "ok": True, "files": [], "missing": True, "note": str(error)}

    files: list = []
    try:
        _walk(device, storage, parts, top, files)
    except Exception as error:
        return {
            "op": "list",
            "ok": False,
            "code": "list_partial",
            "partial": True,
            "files": files,
            "error": f"the listing failed part-way through: {type(error).__name__}: {error}",
        }
    return {"op": "list", "ok": True, "files": files}


def _find_entry(device, storage, parts: list[str]):
    """The `ListEntry` for one device path, via the uncached lookup, or None."""
    if not parts:
        return None
    try:
        entries = device.list_folder_by_name(storage, *parts[:-1])
    except Exception:
        return None
    for entry in entries:
        if entry.name == parts[-1] and not entry.is_folder:
            return entry
    return None


def _op_get(device, op: dict) -> dict:
    storage = _storage(device)
    parts = _split(op["path"])
    local = op["local"]
    parent = os.path.dirname(local)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        with open(local, "wb") as outfile:
            device.get_file_by_name(outfile, storage, *parts)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(local)
        # Only pay for the existence probe once the fetch has already failed, so the
        # common path stays one round trip.
        if _find_entry(device, storage, parts) is None:
            return {
                "op": "get",
                "ok": False,
                "code": "not_found",
                "error": f"{op['path']} is not on the device",
            }
        raise
    return {"op": "get", "ok": True, "size": os.path.getsize(local)}


def _op_put(device, op: dict) -> dict:
    """Write a local file to the device and report the size THE DEVICE gives back.

    Reporting `os.path.getsize(local)` would be a lie dressed as a measurement: a
    short write would report as complete and be indistinguishable from a good book.
    There is no staging primitive to make the write atomic (FIRST-RUN VERIFICATION 6),
    so the honest thing available is to re-read what actually landed.
    """
    storage = _storage(device)
    parts = _split(op["path"])
    if not parts:
        raise ValueError("put needs a file path, not the device root")
    local = op["local"]
    local_size = os.path.getsize(local)
    parent = device.ensure_parent(storage, parts)
    with open(local, "rb") as stream:
        device.put_file(parent, parts[-1], stream, local_size)

    written = _find_entry(device, storage, parts)
    result = {
        "op": "put",
        "ok": True,
        "size": int(written.size or 0) if written is not None else None,
        "local_size": local_size,
    }
    if written is None:
        result["note"] = "the device did not report the written file back, so its size is unknown"
    return result


def _op_rm(device, op: dict) -> dict:
    storage = _storage(device)
    parts = _split(op["path"])
    if not parts:
        raise ValueError("rm needs a path, not the device root")
    target = storage.find_path(parts)
    if target is None:
        # See FIRST-RUN VERIFICATION 8: `find_path` walks the CACHED tree, which omits
        # `*.sdr` folders and `system/`, so "not found" here does not prove the path is
        # absent. The discriminator is what lets a caller tell "this class of path
        # cannot be deleted over MTP" from "the file is already gone" without
        # substring-matching English prose.
        return {
            "op": "rm",
            "ok": False,
            "code": "not_in_cached_tree",
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
    # `ensure_parent` creates every component but the last, so a sentinel is appended
    # to have the whole requested path created. See FIRST-RUN VERIFICATION 7.
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


def _run_ops(device, ops: list) -> list:
    results: list = []
    for op in ops:
        name = op.get("op") if isinstance(op, dict) else None
        handler = _OPS.get(name)
        if handler is None:
            results.append(
                {"op": name, "ok": False, "code": "unknown_op", "error": f"unknown op: {name!r}"}
            )
            continue
        try:
            results.append(handler(device, op))
        except _HelperError:
            raise
        except Exception as error:
            is_write = name in _WRITE_OPS
            code = _classify(error, is_write=is_write)
            if code == EXIT_WRITE_PROTECTED:
                # A read-only device fails the whole batch, not just this op: every
                # remaining write would fail the same way. The results collected so far
                # ride along on the exception so the caller learns which writes landed.
                results.append(
                    {
                        "op": name,
                        "ok": False,
                        "code": "write_protected",
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
                raise _HelperError(
                    f"the device refused the write: {error}", code, results
                ) from error
            results.append({"op": name, "ok": False, "error": f"{type(error).__name__}: {error}"})
    return results


# --- entry point ----------------------------------------------------------------


def _emit_start() -> None:
    sys.stdout.write("\n" + START_MARKER + "\n")
    sys.stdout.flush()


def _emit(results: list, device: dict | None = None) -> None:
    """Marker and payload on ONE line. The leading newline guarantees the marker
    starts a line even if chatter left one open; sharing the line with the payload
    means nothing can be interleaved between the two."""
    payload = {"v": PROTOCOL_VERSION, "device": device or {}, "results": results}
    sys.stdout.write("\n" + RESULT_MARKER + json.dumps(payload) + "\n")
    sys.stdout.flush()


def _read_ops(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise _HelperError("the ops file must hold a JSON object", EXIT_FAILED)
    if payload.get("v") != PROTOCOL_VERSION:
        raise _HelperError(
            f"ops file protocol version {payload.get('v')!r}, expected {PROTOCOL_VERSION}",
            EXIT_FAILED,
        )
    if not isinstance(payload.get("ops"), list):
        raise _HelperError("the ops file must hold a list under 'ops'", EXIT_FAILED)
    return payload


def main(argv: list) -> int:
    _emit_start()
    if len(argv) != 1:
        sys.stderr.write("usage: calibre-debug -e kindle_mtp.py -- <ops file>\n")
        return EXIT_FAILED
    try:
        envelope = _read_ops(argv[0])
    except _HelperError as error:
        sys.stderr.write(str(error) + "\n")
        return error.code
    except Exception as error:
        sys.stderr.write(f"could not read the ops file: {error}\n")
        return EXIT_FAILED

    device = None
    info: dict = {}
    try:
        device = _open_device()
        info = _check_serial(device, envelope.get("serial"))
        _emit(_run_ops(device, envelope["ops"]), info)
        return EXIT_OK
    except _HelperError as error:
        if error.results:
            _emit(error.results, info)
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
