"""The MTP Kindle backend: a 2024-or-later model (or a Scribe) exposes no disk at
all, so every operation goes through Calibre's own MTP driver.

Calibre's driver only runs inside Calibre's interpreter, so this backend does not
import it. It writes an ops file and runs `integrations/kindle_mtp.py` under
`calibre-debug` instead — that helper is the only Calibre-aware part, and the
`runner` seam here is what every test injects in its place. Nothing in this module
may import the helper: it is a standalone script for a different interpreter.

**Batching is the performance contract, not an optimisation.** Every helper
invocation re-opens and re-scans the device, so one invocation per file is unusable
on a real library. `run_ops` is the entry point that matters; the eight
`DeviceBackend` methods are thin single-op wrappers over it, and a bulk caller
(backup, add, sync) is expected to build its own ops list and call `run_ops` once.
`list_files` additionally caches the whole device listing for this backend's
lifetime, invalidated by `write`/`remove`, so a command that lists and then acts does
not pay for a second scan.
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
from media_tools.tasks.ebook.kindle.backend import DeviceFile, DeviceWriteProtected
from media_tools.tasks.ebook.kindle.detect import Device, DeviceBusy, DeviceNotFound

# The helper's own copies of these are authoritative — this module cannot import it
# (different interpreter, and it imports `calibre`), so the constants are duplicated
# and `tests/unit/test_kindle_mtp.py` pins the two copies together.
PROTOCOL_VERSION = 1
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


def parse_helper_output(stdout: str, stderr: str, returncode: int) -> dict:
    """Turn one `calibre-debug` invocation into the helper's result object, or into
    the exception its exit code stands for.

    `calibre-debug` writes its own banner and chatter to stdout, and a Calibre plugin
    may print more at any point, so the result is found by the LAST occurrence of the
    marker — never by assuming stdout is only the JSON, and never by the first
    occurrence, which chatter could legitimately contain.
    """
    tail = _stderr_tail(stderr)
    if returncode == EXIT_NO_DEVICE:
        raise DeviceNotFound(
            "no Kindle found over MTP: connect one over USB and unlock it."
            + (f" Calibre said: {tail}" if tail else "")
        )
    if returncode == EXIT_BUSY:
        raise DeviceBusy(busy_hint() + (f" Calibre said: {tail}" if tail else ""))
    if returncode == EXIT_WRITE_PROTECTED:
        raise DeviceWriteProtected(
            "the Kindle refused the write as read-only."
            + (f" Calibre said: {tail}" if tail else "")
        )
    if returncode != EXIT_OK:
        raise CalibreError(tail or f"calibre-debug exited {returncode}")

    marker_at = (stdout or "").rfind(RESULT_MARKER)
    if marker_at < 0:
        raise CalibreError(
            "the MTP helper printed no result marker — calibre-debug may have failed "
            "before the helper ran." + (f" Calibre said: {tail}" if tail else "")
        )
    remainder = stdout[marker_at + len(RESULT_MARKER) :]
    payload = _first_json_object(remainder)
    if payload is None:
        raise CalibreError(
            "could not parse the MTP helper's result object."
            + (f" Calibre said: {tail}" if tail else "")
        )
    if payload.get("v") != PROTOCOL_VERSION:
        raise CalibreError(
            f"the MTP helper spoke protocol version {payload.get('v')!r}, "
            f"expected {PROTOCOL_VERSION}"
        )
    if not isinstance(payload.get("results"), list):
        raise CalibreError("the MTP helper's result object carries no 'results' list")
    return payload


def _first_json_object(text: str) -> dict | None:
    """The helper prints the object on one line, but anything may follow it, so take
    the first non-empty line after the marker; fall back to the whole remainder in
    case a future helper pretty-prints it."""
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            break
        return parsed if isinstance(parsed, dict) else None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class CalibreDebugRunner:
    """The default `runner`: write the ops file, spawn `calibre-debug`, parse what
    comes back. Runs with `config_env(cache_dir)` like every other Calibre call in
    this project, so the user's own Calibre configuration is neither read nor
    written."""

    def __init__(self, cache_dir: Path, *, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.cache_dir = Path(cache_dir)
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
                json.dump({"v": PROTOCOL_VERSION, "ops": ops}, stream)
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
    `mode="mtp"`). Implements `backend.DeviceBackend`; every path it takes or returns
    is device-relative and POSIX-style, exactly as the mass-storage backend's are."""

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
        self._runner = runner or CalibreDebugRunner(self.cache_dir)
        self._gui_check = gui_check or calibre_gui_is_running
        self._listing: list[DeviceFile] | None = None

    # -- the batching entry point -------------------------------------------------

    def run_ops(self, ops: list[dict]) -> list[dict]:
        """Run a whole batch of operations in ONE helper invocation, returning the
        helper's per-op result objects in the order given. This is what a bulk caller
        should use: each invocation re-opens and re-scans the device, so splitting a
        library's worth of work into one invocation per file is not a slower version
        of this — it is unusable."""
        if not ops:
            return []
        self._preflight()
        payload = self._runner(list(ops))
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise CalibreError("the MTP helper's result object carries no 'results' list")
        if len(results) != len(ops):
            raise CalibreError(
                f"the MTP helper returned {len(results)} results for {len(ops)} operations"
            )
        return results

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
        if not result.get("ok"):
            raise CalibreError(
                result.get("error") or f"the MTP helper failed the {op['op']} operation"
            )
        return result

    # -- the DeviceBackend protocol -----------------------------------------------

    def list_files(self, prefix: str = "") -> list[DeviceFile]:
        if self._listing is None:
            self._listing = self._files_under("")
        return _under_prefix(self._listing, prefix)

    def read(self, path: str, dest: Path) -> None:
        self._one({"op": "get", "path": path, "local": str(Path(dest).resolve())})

    def write(self, local: Path, path: str) -> None:
        self._one({"op": "put", "path": path, "local": str(Path(local).resolve())})
        self._listing = None

    def remove(self, path: str) -> None:
        self._one({"op": "rm", "path": path})
        self._listing = None

    def exists(self, path: str) -> bool:
        if self._listing is not None:
            return any(entry.path == path for entry in self._listing)
        # Cold cache: list only the parent folder rather than the whole device, so a
        # lone existence check does not pay for a full scan.
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        return any(entry.path == path for entry in self._files_under(parent))

    def free_space(self) -> int:
        return int(self._one({"op": "free"}).get("free") or 0)

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

    def _files_under(self, prefix: str) -> list[DeviceFile]:
        result = self._one({"op": "list", "path": prefix})
        return [
            DeviceFile(
                path=str(entry.get("path", "")),
                size=int(entry.get("size") or 0),
                mtime=float(entry.get("mtime") or 0.0),
            )
            for entry in result.get("files") or []
        ]


def _under_prefix(files: list[DeviceFile], prefix: str) -> list[DeviceFile]:
    cleaned = (prefix or "").strip("/")
    if not cleaned:
        return list(files)
    return [entry for entry in files if entry.path.startswith(cleaned + "/")]
