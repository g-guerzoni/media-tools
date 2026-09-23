"""The MTP Kindle backend: a 2024-or-later model (or a Scribe) exposes no disk at
all, so every operation goes through Calibre's own MTP driver.

Calibre's driver only runs inside Calibre's interpreter, so this backend does not
import it. It writes an ops file and runs `integrations/kindle_mtp.py` under
`calibre-debug` instead — that helper is the only Calibre-aware part, and the
`runner` seam here is what every test injects in its place. Nothing in this module
may import the helper: it is a standalone script for a different interpreter.

**Batching is the performance contract, not an optimisation.** Every helper
invocation re-opens and re-scans the device, so one invocation per file is unusable
on a real library. `run_ops` is the entry point that matters; the
`DeviceBackend` methods are thin single-op wrappers over it, and a bulk caller
(backup, add, sync) is expected to build its own ops list and call `run_ops` once.
`list_files` additionally caches the whole device listing for this backend's
lifetime, invalidated by `write`/`remove`/`close` — but NEVER caches a listing the
helper flagged as incomplete, because a cached empty listing is how a backup writes
nothing and calls itself a success.

The exclusion rules (`audible/`, everything under `system/` except `thumbnails/`) are
imported from `massstorage`, not restated here, so the two backends' listings cannot
drift apart. `backend.DeviceBackend`'s docstring is the contract both conform to.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from media_tools.integrations.calibre import CALIBRE_DEBUG, CalibreError, config_env
from media_tools.tasks.ebook.kindle.backend import (
    PROTECTED_DIRS,
    RESTRICTED_EXCEPTION,
    RESTRICTED_PARENT,
    DeviceFile,
    DeviceWriteProtected,
    is_volume_litter,
    prefix_targets_a_forbidden_system_child,
    validate_writable_path,
)
from media_tools.tasks.ebook.kindle.detect import Device, DeviceBusy, DeviceNotFound

# The helper's own copies of these are authoritative — this module cannot import it
# (different interpreter, and it imports `calibre`), so the constants are duplicated
# and `tests/unit/test_kindle_mtp.py` pins the two copies together.
PROTOCOL_VERSION = 1
START_MARKER = "@@media-tools-mtp-start-v1@@"
RESULT_MARKER = "@@media-tools-mtp-result-v1@@"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_DEVICE = 2
EXIT_BUSY = 3
EXIT_WRITE_PROTECTED = 4

# A book over MTP transfers at a few MB/s and a batch may hold many of them, so this
# is generous by design; it exists to stop a wedged session hanging forever, not to
# bound normal work.
DEFAULT_TIMEOUT = 1800

_HELPER = Path(__file__).resolve().parents[3] / "integrations" / "kindle_mtp.py"

# The GUI's own executable names, matched against the EXECUTABLE of each process, not
# against the whole command line. A substring match would be wrong in both directions:
# `calibre-debug` (this backend's own helper) and `calibre-parallel` (Calibre's worker
# processes, which `ebook-convert` spawns routinely) both start with "calibre", and
# neither one holds the device — reporting them as the GUI would block the user with a
# `DeviceBusy` they cannot act on.
_GUI_EXECUTABLES = frozenset({"calibre", "calibre-gui", "calibre.exe"})

# Exit 2 covers three different situations and the user needs to know which: "plug it
# in" and "you have the wrong device attached" call for opposite actions. The helper
# reports which one in the result payload's `device.reason`, so this never has to
# substring-match the helper's own prose.
_NO_DEVICE_REASONS = {
    "no_device": "no Kindle found over MTP: connect one over USB and unlock it.",
    "different_device": (
        "the MTP device that answered is not the Kindle media-tools detected — its "
        "serial does not match. Nothing on it was touched. Disconnect the other MTP "
        "device, or re-run detection."
    ),
    "no_storage": (
        "the Kindle answered over MTP but reported no storage — it is usually still "
        "locked. Unlock the screen and try again."
    ),
}
_NO_DEVICE_UNKNOWN = (
    "no usable Kindle over MTP. One of three things: none is connected, the device "
    "that answered is not the one detected, or it reported no storage (usually a "
    "locked screen)."
)


class MtpPathNotInCachedTree(FileNotFoundError):
    """`rm` could not reach this path.

    Calibre's `delete_file_or_folder` needs a `FileOrFolder` from the CACHED device
    tree, which omits `*.sdr` folders and everything under `system/`, and Calibre 9.15
    exposes no delete-by-name primitive. So this means either "the file is already
    gone" or "this class of path cannot be deleted over MTP at all", and the driver
    cannot tell the two apart.

    It subclasses `FileNotFoundError` so a caller written to `DeviceBackend`'s contract
    ("remove of an absent path raises FileNotFoundError") works unchanged against both
    backends, while a caller that cares — `cli.run_remove`, which reports it as the
    `sidecar_not_removed` warning on a book that is otherwise removed, rather than as
    a failure — can catch this class specifically instead of substring-matching
    English prose.
    """


def _stderr_tail(stderr: str, lines: int = 10) -> str:
    return "\n".join((stderr or "").splitlines()[-lines:]).strip()


def busy_hint() -> str:
    """Who is probably holding the device, and what the user — not this tool — has to
    do about it. MTP allows exactly one holder, and the fix is always to release it
    somewhere else: media-tools never kills a process, unmounts a volume or changes a
    system setting on the user's behalf."""
    if platform.system() == "Darwin":
        return (
            "Another program is holding the device. On macOS the usual culprits are "
            "ptpcamerad (macOS's own camera daemon, visible in Activity Monitor), "
            "OpenMTP, Android File Transfer and Send to Kindle. Quit whichever is "
            "running, unplug and replug the Kindle, then try again. media-tools never "
            "quits a process or changes a system setting for you."
        )
    return (
        "Another program is holding the device. On Linux it is usually already mounted "
        "through GVFS — release it yourself with `gio mount -u mtp://...` (run "
        "`gio mount -l` to find the exact URI), then try again. media-tools never "
        "unmounts anything for you."
    )


def gui_is_running_in(ps_output: str) -> bool:
    """Whether any line of `ps -Ao command` output is Calibre's GUI. Split out from
    the `ps` call itself so the matching rule is testable without a process table."""
    for line in (ps_output or "").splitlines():
        command = line.strip().split()
        if not command:
            continue
        if PurePosixPath(command[0]).name in _GUI_EXECUTABLES:
            return True
    return False


def calibre_gui_is_running() -> bool:
    """Whether Calibre's GUI holds (or is about to grab) the device. Deliberately a
    plain `ps` scan behind a seam the backend takes as a parameter, so a test can
    drive both branches without a real process table — and so a false negative here
    costs nothing worse than the helper reporting a busy device itself."""
    try:
        proc = subprocess.run(["ps", "-Ao", "command"], capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return False
    return gui_is_running_in(proc.stdout or "")


def _payload_from(stdout: str) -> dict | None:
    """The last line that STARTS WITH the result marker, parsed.

    The helper puts the marker and the payload on one line precisely so this can be a
    line-level match rather than a substring search: `calibre-debug` and Calibre's
    plugins print freely both before and after the result, and chatter may legitimately
    contain the marker string itself.
    """
    for line in reversed((stdout or "").splitlines()):
        stripped = line.strip()
        if not stripped.startswith(RESULT_MARKER):
            continue
        try:
            parsed = json.loads(stripped[len(RESULT_MARKER) :])
        except json.JSONDecodeError:
            # A bare marker line, or one a plugin garbled, must not fail an otherwise
            # successful invocation — keep looking further back.
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _results_of(payload: dict | None) -> list | None:
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    return results if isinstance(results, list) else None


def parse_helper_output(stdout: str, stderr: str, returncode: int) -> dict:
    """Turn one `calibre-debug` invocation into the helper's result object, or into
    the exception its exit code stands for."""
    tail = _stderr_tail(stderr)
    said = f" Calibre said: {tail}" if tail else ""
    payload = _payload_from(stdout)

    # A non-zero exit with no start marker means calibre-debug never reached the
    # helper at all: the INVOCATION is broken (a wrong `-e ... --` convention, a
    # Calibre that will not start), which has nothing to do with whether a Kindle is
    # attached. Reporting it as DeviceNotFound would send the user hunting for a cable.
    if returncode != EXIT_OK and START_MARKER not in (stdout or ""):
        raise CalibreError(
            "calibre-debug never reached the MTP helper — no start marker in its "
            f"output, so the invocation itself failed (exit {returncode}), not the "
            f"device. Check that `calibre-debug -e {_HELPER} -- <ops file>` runs." + said
        )

    if returncode == EXIT_NO_DEVICE:
        reason = (payload or {}).get("device", {}).get("reason")
        raise DeviceNotFound(_NO_DEVICE_REASONS.get(reason, _NO_DEVICE_UNKNOWN) + said)
    if returncode == EXIT_BUSY:
        raise DeviceBusy(busy_hint() + said)
    if returncode == EXIT_WRITE_PROTECTED:
        # The helper emits whatever it completed before aborting, and that record used
        # to be attached to the exception as `.results` for "a caller that needs to
        # tell which writes landed". No caller ever read it: every one of them reacts
        # to this by reporting `device_write_protected` and stopping, and what landed
        # is established by the verify stage against the DEVICE, not by trusting a
        # report from the invocation that just failed.
        raise DeviceWriteProtected("the Kindle refused the write as read-only." + said)
    if returncode != EXIT_OK:
        raise CalibreError(tail or f"calibre-debug exited {returncode}")

    if payload is None:
        raise CalibreError(
            "could not parse the MTP helper's result object — no line starting with "
            "the result marker, or malformed JSON after it." + said
        )
    if payload.get("v") != PROTOCOL_VERSION:
        raise CalibreError(
            f"the MTP helper spoke protocol version {payload.get('v')!r}, "
            f"expected {PROTOCOL_VERSION}"
        )
    if _results_of(payload) is None:
        raise CalibreError("the MTP helper's result object carries no 'results' list")
    return payload


class CalibreDebugRunner:
    """The default `runner`: write the ops file, spawn `calibre-debug`, parse what
    comes back. Runs with `config_env(cache_dir)` like every other Calibre call in
    this project, so the user's own Calibre configuration is neither read nor
    written. `serial` rides in the envelope so the helper can refuse a device that is
    not the one detection found."""

    def __init__(
        self, cache_dir: Path, *, serial: str | None = None, timeout: int = DEFAULT_TIMEOUT
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.serial = serial
        self.timeout = timeout

    def __call__(self, ops: list[dict]) -> dict:
        tool = CALIBRE_DEBUG.locate()
        if tool is None:
            raise CalibreError(f"calibre-debug not found. {CALIBRE_DEBUG.install_hint}")
        if not _HELPER.exists():
            raise CalibreError(f"the MTP helper is missing from the install: {_HELPER}")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(suffix=".json", prefix="mtp-ops-", dir=self.cache_dir)
        ops_file = Path(name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump({"v": PROTOCOL_VERSION, "serial": self.serial, "ops": ops}, stream)
            try:
                proc = subprocess.run(
                    [tool, "-e", str(_HELPER), "--", str(ops_file)],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    env=config_env(self.cache_dir),
                )
            except (subprocess.SubprocessError, OSError) as error:
                raise CalibreError(f"calibre-debug failed: {error}") from error
        finally:
            ops_file.unlink(missing_ok=True)
        return parse_helper_output(proc.stdout, proc.stderr, proc.returncode)


class MtpBackend:
    """Talks to a Kindle in MTP mode (a `Device` from `detect.find_device` with
    `mode="mtp"`). Implements `backend.DeviceBackend` — see that protocol's docstring
    for the contract this shares with the mass-storage backend."""

    def __init__(
        self,
        device: Device,
        *,
        runner: Callable[[list[dict]], dict] | None = None,
        cache_dir: Path,
        gui_check: Callable[[], bool] | None = None,
    ) -> None:
        self.device = device
        self.cache_dir = Path(cache_dir)
        self._runner = runner or CalibreDebugRunner(self.cache_dir, serial=device.serial)
        self._gui_check = gui_check or calibre_gui_is_running
        self._listing: list[DeviceFile] | None = None

    # -- the batching entry point -------------------------------------------------

    def run_ops(self, ops: list[dict]) -> list[dict]:
        """Run a whole batch of operations in ONE helper invocation, returning the
        helper's per-op result objects in the order given.

        This is what a bulk caller should use: each invocation re-opens and re-scans
        the device, so splitting a library's worth of work into one invocation per
        file is not a slower version of this — it is unusable.

        **The caller must check each result's `ok` field.** Unlike the
        `DeviceBackend` methods, which raise, this hands back raw per-op results: an
        exception here means the whole invocation failed, and a batch that ran fine
        can still contain individual failures. A result that is not `ok` carries
        `error` (human-readable) and usually `code` — `not_in_cached_tree`,
        `not_found`, `write_protected`, `list_failed`, `list_partial`, `unknown_op`.
        A `list` result may also carry `missing` (the prefix is not on the device) or
        `partial` (the listing is incomplete and must not be treated as authoritative).
        """
        if not ops:
            return []
        self._preflight()
        payload = self._runner(list(ops))
        results = _results_of(payload)
        if results is None:
            raise CalibreError("the MTP helper's result object carries no 'results' list")
        if len(results) != len(ops):
            raise CalibreError(
                f"the MTP helper returned {len(results)} results for {len(ops)} operations"
            )
        # The exclusions are applied HERE, not only in `list_files`, because this is
        # the entry point bulk callers are pointed at: a caller batching `list` ops
        # through `run_ops` must not get `audible/` or `system/wifi/` back, which is
        # exactly the data the exclusion exists to keep off the host.
        return [_without_excluded_files(result) for result in results]

    def _preflight(self) -> None:
        """MTP allows exactly one holder, and Calibre's GUI grabs a connected device
        the moment it sees one — so it is checked before every invocation, not once
        per backend: the GUI can be started while a long run is in flight."""
        if self._gui_check():
            raise DeviceBusy(
                "Calibre's GUI is running and an MTP device allows exactly one "
                "holder. Close Calibre and run this again. media-tools never quits a "
                "program for you."
            )

    def _one(self, op: dict) -> dict:
        result = self.run_ops([op])[0]
        if not isinstance(result, dict):
            raise CalibreError(f"the MTP helper returned a malformed result: {result!r}")
        if result.get("ok"):
            return result
        raise _error_for(result, op)

    # -- the DeviceBackend protocol -----------------------------------------------

    def list_files(self, prefix: str = "") -> list[DeviceFile]:
        parts = tuple(p for p in (prefix or "").strip("/").split("/") if p)
        if PROTECTED_DIRS & set(parts) or prefix_targets_a_forbidden_system_child(parts):
            return []
        if self._listing is None:
            files, complete, _missing = self._files_under("")
            if not complete:
                # A BACKSTOP, not the mechanism. An incomplete listing must never
                # become this backend's idea of the device for the rest of its life —
                # but what enforces that today is one layer down: `_op_list` answers
                # `ok: false` for a walk that failed part-way and for ANY failure at
                # the device root, and `_one` raises on a result that is not `ok`, so
                # this line is not reached. The only `complete: false` the real helper
                # produces is a MISSING prefix, which cannot happen at the root asked
                # for here. Left in place because the rule it states is the one that
                # matters, and a future helper answering differently must not silently
                # start caching a short listing.
                return _under_prefix(files, prefix)
            self._listing = files
        return _under_prefix(self._listing, prefix)

    def read(self, path: str, dest: Path) -> None:
        self._one({"op": "get", "path": path, "local": str(Path(dest).resolve())})

    def read_many(self, items: list[tuple[str, Path]]) -> None:
        """`read` for a whole batch, in ONE helper invocation — see
        `backend.DeviceBackend` for the contract, and `run_ops` for why the batching
        is the point rather than an optimisation: a per-file invocation re-opens and
        re-scans the device every time, which a real library cannot afford.

        `run_ops` hands back raw per-op results, so the failure mapping the other
        protocol methods get from `_one` is applied here by hand: the FIRST failed op
        raises what `read` would have raised for it, and whatever already transferred
        stays on disk.
        """
        ops = [
            {"op": "get", "path": path, "local": str(Path(dest).resolve())} for path, dest in items
        ]
        if not ops:
            return
        results = self.run_ops(ops)
        for op, result in zip(ops, results, strict=True):
            if not isinstance(result, dict):
                raise CalibreError(f"the MTP helper returned a malformed result: {result!r}")
            if not result.get("ok"):
                raise _error_for(result, op)

    def write(self, local: Path, path: str) -> None:
        # Validated BEFORE anything else: `path` may be built from data this project
        # did not produce (a book's own EXTH records), and nothing else here stops
        # `..`/`audible/`/a forbidden `system/` child from ever reaching a real write.
        path = validate_writable_path(path)
        self._one({"op": "put", "path": path, "local": str(Path(local).resolve())})
        self._listing = None

    def remove(self, path: str) -> None:
        self._one({"op": "rm", "path": path})
        self._listing = None

    def exists(self, path: str) -> bool:
        if self._listing is not None:
            return any(entry.path == path for entry in self._listing)
        # Cold cache: list only the parent folder (root, for a top-level path) rather
        # than paying for a full device scan for one existence check.
        prefix = "" if "/" not in path else path.rsplit("/", 1)[0]
        files, complete, missing = self._files_under(prefix)
        if missing:
            # `missing` is a DEFINITE, verified answer — `kindle_mtp.py`'s own
            # docstring calls it the ONLY exception that means "this folder is not
            # on the device", not an incomplete listing. A path cannot exist under a
            # folder that itself does not exist, so this is "verified absent", not
            # "could not check": an MTP device with no `system/thumbnails/` at all
            # (which silently discards a sideloaded cover) must still read as
            # `rejected`, not `failed` — that is the exact case this whole check
            # exists to get right.
            return False
        if not complete:
            # The same BACKSTOP as in `list_files`, and reached by the same nothing:
            # the real helper's only `complete: false` is a missing prefix, which the
            # branch above already returned on, and a listing that failed part-way is
            # `ok: false` and raises inside `_one`. So the distinction this guard
            # exists for — a caller (verifying a just-written Kindle thumbnail) being
            # able to tell "verified absent" from "could not check" (I3) — is already
            # kept by the layer below; this states it at the layer that would notice
            # if that ever stopped being true.
            raise CalibreError(
                f"could not verify whether {path!r} exists: the MTP listing needed to "
                "check it was incomplete, so absence cannot be confirmed"
            )
        if not prefix:
            # A complete ROOT listing is exactly what `list_files` itself would
            # cache — keep that benefit for whatever calls `list_files`/`exists` next.
            self._listing = files
        return any(entry.path == path for entry in files)

    def free_space(self) -> int:
        """Bytes free on the device's main storage.

        A result carrying no usable number RAISES rather than answering `0` — the
        same C1/I3 rule `exists` follows above, and `backend.DeviceBackend`'s own:
        something that cannot be resolved into a definite answer must not answer as
        if it had checked. `0` means "this device is full", which a caller acts on;
        a driver that could not say is not that, and collapsing the two makes
        `ebook kindle add` refuse to copy anything onto a Kindle with gigabytes free
        — after its mandatory backup has already run.
        """
        free = self._one({"op": "free"}).get("free")
        if isinstance(free, bool) or not isinstance(free, int) or free < 0:
            raise CalibreError(
                "the MTP helper did not report this device's free space (it answered "
                f"{free!r}), so whether a book will fit cannot be answered"
            )
        return free

    def eject(self) -> None:
        # MTP has nothing to eject; the helper closes the session and the op exists
        # so a caller can treat both backends identically.
        self._one({"op": "eject"})

    def close(self) -> None:
        # Each invocation opens and closes its own session, so there is nothing held
        # open to release — only the listing cache, which must not outlive the
        # backend a caller just declared finished with.
        self._listing = None

    # -- internals ----------------------------------------------------------------

    def _files_under(self, prefix: str) -> tuple[list[DeviceFile], bool, bool]:
        """`(files, complete, missing)`. `complete` is False whenever the helper
        flagged the listing as incomplete (`partial`/`note`) OR as a missing prefix
        (`missing`) — none of the three is safe to CACHE as the state of the device.
        `missing` is called out separately because, unlike `partial`/`note`, it is
        the helper's own DEFINITE, verified answer — `kindle_mtp.py`'s own docstring
        calls it the only exception that means "this folder is not on the device",
        not merely "the listing could not be completed". A caller that can act on
        that distinction (`exists`, below) should; one that cannot (`list_files`)
        only needs `complete`.
        """
        result = self._one({"op": "list", "path": prefix})
        files = [
            DeviceFile(
                path=str(entry.get("path", "")),
                size=int(entry.get("size") or 0),
                mtime=float(entry.get("mtime") or 0.0),
            )
            for entry in result.get("files") or []
        ]
        missing = bool(result.get("missing"))
        complete = not (result.get("partial") or missing or result.get("note"))
        return files, complete, missing


def _error_for(result: dict, op: dict) -> Exception:
    """The exception a failed per-op result maps to, per `DeviceBackend`'s contract."""
    message = result.get("error") or f"the MTP helper failed the {op.get('op')} operation"
    code = result.get("code")
    if code == "not_in_cached_tree":
        return MtpPathNotInCachedTree(message)
    if code == "not_found":
        return FileNotFoundError(message)
    if code == "write_protected":
        return DeviceWriteProtected(message)
    return CalibreError(message)


def _without_excluded_files(result: dict) -> dict:
    """A `list` result with the excluded paths removed. Anything else is untouched."""
    if not isinstance(result, dict) or result.get("op") != "list":
        return result
    files = result.get("files")
    if not isinstance(files, list):
        return result
    return {
        **result,
        "files": [
            entry
            for entry in files
            if isinstance(entry, dict) and _is_listable(str(entry.get("path", "")))
        ],
    }


def _is_listable(path: str) -> bool:
    """The mass-storage backend's exclusion rules, applied to an MTP listing.

    `audible/` is off-limits at any depth and a `system/` directory exposes only its
    `thumbnails/` child. The MTP helper deliberately does NOT apply this — the uncached
    lookups exist precisely so `system/` is reachable at all — so the filter lives here,
    where the fake runner can drive it, using `massstorage`'s own constants so the two
    backends cannot diverge.
    """
    parts = path.split("/")
    directories = parts[:-1]
    if any(part in PROTECTED_DIRS for part in directories):
        return False
    for index, part in enumerate(directories):
        if part == RESTRICTED_PARENT and parts[index + 1] != RESTRICTED_EXCEPTION:
            return False
    return not any(is_volume_litter(part) for part in parts)


def _under_prefix(files: list[DeviceFile], prefix: str) -> list[DeviceFile]:
    cleaned = (prefix or "").strip("/")
    if not cleaned:
        return list(files)
    return [entry for entry in files if entry.path.startswith(cleaned + "/")]
