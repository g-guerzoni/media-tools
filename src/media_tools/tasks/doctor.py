"""doctor: is this machine ready to run media-tools.

Checks the interpreter, the package versions, the external tools every task depends on
(ffmpeg, Deno, Calibre), whether an OpenRouter key resolves for `ebook build`'s
LLM-assisted stages, and that the output root is writable. Everything it prints is a
*result*, so — like `formats` and `status` — it goes to stdout, never stderr: results
on stdout, progress and logs on stderr. The progress/log stream convention (stderr for
humans, stdout for `--json`) is for the multi-item file tasks, not for a one-shot
report like this one.

Calibre and the OpenRouter key are both WARNINGS, never failures: `compress`,
`convert` (for non-ebook formats), `split`, `download`, `formats` and `status` need
neither. Each check's message says which command actually needs it.

The same rule covers the two Kindle checks (`kindle-mtp-driver`, `kindle-device`), and
covers them absolutely: a machine with no Kindle attached is not a broken machine, and
nothing outside `media-tools ebook kindle ...` ever touches one. Neither check may
report `"missing"` — that status is what moves `doctor`'s own exit code to 3, which
would turn "no Kindle plugged in right now" into a failed environment check at every
session start.

`doctor` must never print a secret: the OpenRouter key check goes through
`integrations.openrouter.key_present` (RULING RB2) — the same lookup `ebook build`
itself uses (a literal `OPENROUTER_API_KEY`, an `op://vault/item/field` reference, or
a named `--op-item`) — and reports only whether a key resolves, never the value.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from media_tools.core.events import EXIT_DEPENDENCY, EXIT_OK, EXIT_USAGE
from media_tools.core.ffmpeg import ffmpeg_exe
from media_tools.core.paths import output_root
from media_tools.integrations import calibre, openrouter
from media_tools.tasks.ebook.kindle import detect as kindle_detect
from media_tools.tasks.ebook.kindle import mtp as kindle_mtp

NAME = "doctor"
HELP = "Check the environment: ffmpeg, Calibre, Deno, output root, and more."

MIN_PYTHON = (3, 11)
# The pip packages whose installed version is worth reporting (and, with
# --check-updates, comparing against PyPI). "deno" here is the pip *package* that
# vendors the Deno binary; the separate "deno-runtime" check below verifies the binary
# itself actually runs, which importlib.metadata cannot tell us.
PACKAGES = ["media-tools", "yt-dlp", "yt-dlp-ejs", "deno", "imageio-ffmpeg"]
# "media-tools" is excluded from the PyPI comparison: this project is not published
# under that name, and PyPI already has an unrelated package called "media-tools" -
# comparing this checkout's version against it would just be a name collision, not a
# real update signal. `--update` never touches it either, for the same reason.
UPDATE_CHECK_PACKAGES = [name for name in PACKAGES if name != "media-tools"]
OPENROUTER_ENV = "OPENROUTER_API_KEY"
UPDATE_CACHE_TTL_S = 24 * 60 * 60
# Minor finding: `openrouter.resolve_key`'s own default (30s x up to 6 field labels,
# ~3 minutes worst case) is fine for a real `ebook build` that is about to spend
# minutes converting books anyway, but `doctor` is a quick health check — a named
# `--op-item` that never resolves must not make it hang for anywhere near that long.
_OP_ITEM_LOOKUP_TIMEOUT_S = 5.0
_STATUS_MARK = {"ok": "✓", "warn": "!", "missing": "✗"}


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # "ok" | "warn" | "missing"
    detail: str
    hint: str | None = None


def register(subparsers):
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP)
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="Output root to check for writability "
        "(default: MEDIA_TOOLS_OUT, the repo's media/, or ./media).",
    )
    parser.add_argument("--json", action="store_true", dest="json_mode")
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Only the summary and any problems."
    )
    parser.add_argument(
        "--check-updates",
        action="store_true",
        help="Compare installed package versions against PyPI (cached for 24h).",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Upgrade pip dependencies, then re-check in a fresh process.",
    )
    parser.add_argument(
        "--op-item",
        default=None,
        metavar="NAME",
        help="Named 1Password item to check the OpenRouter key against "
        "(same flag `media-tools ebook` accepts).",
    )
    return parser


# -- individual checks ---------------------------------------------------------------


def _python_check() -> Check:
    info = sys.version_info
    detail = f"{info.major}.{info.minor}.{info.micro}"
    if (info.major, info.minor) >= MIN_PYTHON:
        return Check("python", "ok", detail)
    return Check(
        "python",
        "missing",
        detail,
        hint=f"media-tools needs Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
    )


def _venv_check() -> Check:
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if in_venv:
        return Check("venv", "ok", sys.prefix)
    return Check(
        "venv",
        "warn",
        "not running inside a virtualenv",
        # Matches README.md's clone+venv instructions and .claude/settings.json's
        # own hook message, all three of which name NO minor version. A pinned one
        # (this used to say python3.13) drifts the moment Homebrew moves, and then
        # three files and a test disagree about a number none of them needs: the
        # project supports >= 3.11 and cares about nothing beyond that.
        hint="python3 -m venv .venv && .venv/bin/pip install -e . --group dev",
    )


def _package_check(name: str) -> Check:
    try:
        version = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return Check(name, "warn", "not installed", hint=f"pip install -e . (needs {name})")
    return Check(name, "ok", version)


def _run_version(argv: list[str], *, timeout: float = 10.0) -> tuple[bool, str]:
    """Best-effort `<tool> --version`-style probe. Never raises: a missing binary, a
    timeout or any other OSError just means "it does not run"."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    combined = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        return False, combined
    first_line = combined.splitlines()[0] if combined else ""
    return True, first_line


def _ffmpeg_check() -> Check:
    try:
        exe = ffmpeg_exe()
    except Exception as error:
        return Check("ffmpeg", "missing", "not resolvable", hint=str(error))
    ok, detail = _run_version([exe, "-version"])
    if not ok:
        return Check(
            "ffmpeg",
            "missing",
            f"found at {exe} but it did not run",
            hint="reinstall: pip install -e . (ffmpeg ships with imageio-ffmpeg)",
        )
    return Check("ffmpeg", "ok", detail or exe)


def _deno_exe() -> str | None:
    on_path = shutil.which("deno")
    if on_path:
        return on_path
    try:
        import deno as deno_pkg

        return deno_pkg.find_deno_bin()
    except Exception:
        return None


def _deno_check() -> Check:
    exe = _deno_exe()
    if exe is None:
        return Check(
            "deno-runtime",
            "missing",
            "not found",
            hint="reinstall: pip install -e . (deno ships with yt-dlp[deno])",
        )
    ok, detail = _run_version([exe, "--version"])
    if not ok:
        return Check(
            "deno-runtime",
            "missing",
            f"found at {exe} but it did not run",
            hint="reinstall: pip install -e . (deno ships with yt-dlp[deno])",
        )
    return Check("deno-runtime", "ok", detail or exe)


#: The three Calibre command-line tools `media-tools` shells out to: `ebook-convert`
#: and `ebook-meta` for `media-tools convert`'s ebook engine, and all three for
#: `media-tools ebook build` (metadata reads, conversion, and online cover lookup).
CALIBRE_TOOLS = ("ebook-convert", "ebook-meta", "fetch-ebook-metadata")
_CALIBRE_HINT = (
    "brew install --cask calibre -- ebook-convert/ebook-meta are needed by "
    "`media-tools convert` for ebook formats; all three tools are needed by "
    "`media-tools ebook build`"
)


def _calibre_check() -> Check:
    # I6: use the same lookup `ebook build`/`convert` themselves use
    # (`calibre.find_tool`, which also searches e.g.
    # /Applications/calibre.app/Contents/MacOS on macOS), not a bare `shutil.which`
    # — otherwise a .dmg install makes doctor warn "not found" while the tools it is
    # reporting on work fine for every other command. Same defect class as RB2's fix
    # for the OpenRouter key check below.
    paths = {name: calibre.find_tool(name) for name in CALIBRE_TOOLS}
    missing = [name for name in CALIBRE_TOOLS if not paths[name]]
    if missing:
        return Check("calibre", "warn", f"{', '.join(missing)} not found", hint=_CALIBRE_HINT)

    versions = []
    for name in CALIBRE_TOOLS:
        ok, detail = _run_version([paths[name], "--version"])
        versions.append(detail if ok and detail else f"{name}: --version failed")
    return Check("calibre", "ok", "; ".join(versions), hint=_CALIBRE_HINT)


def _openrouter_check(op_item: str | None = None, *, env=None, runner=None) -> Check:
    # RULING RB2: ask the ebook task's own key-lookup rules (a literal value, an
    # op://vault/item/field reference, or a named --op-item) instead of re-reading
    # OPENROUTER_API_KEY here - doctor's own lookup would miss the last two and
    # report a key "missing" that `media-tools ebook build` reads just fine.
    # `key_present` never returns the value itself, so there is nothing here for
    # --json or the human table to leak.
    hint = (
        f"export {OPENROUTER_ENV}=... (a literal key or an op://vault/item/field "
        "reference) or pass --op-item NAME; needed by `media-tools ebook build`'s "
        "LLM-assisted normalize/dedup stages, or pass --no-llm to skip them"
    )
    if openrouter.key_present(op_item, env=env, runner=runner, timeout=_OP_ITEM_LOOKUP_TIMEOUT_S):
        return Check(
            "openrouter-key",
            "ok",
            "configured",
            hint="used by `media-tools ebook build`'s LLM-assisted normalize/dedup stages",
        )
    return Check("openrouter-key", "warn", "not configured", hint=hint)


# -- the Kindle section: never a failure, and never a warn with no action ------------
#
# `media-tools ebook kindle ...` is the only thing on this list that needs a Kindle, and
# only its MTP half needs Calibre's device driver. Neither check may ever report
# "missing" — that is the status that moves `doctor`'s exit code.
#
# Neither may report "warn" for a state with nothing to DO about it either, and that is
# a separate rule worth stating on its own. The session-start hook runs `doctor
# --quiet`, which prints every non-"ok" row; every other warn in that report is an
# actionable environment defect, so "no Kindle plugged in" sitting there permanently
# would train everyone reading it to skim the warn level — hiding the rows that really
# do block a task. So: no Kindle connected is "ok". A Kindle that IS connected and
# needs something (an MTP one with no Calibre, or one Calibre's GUI is holding) is
# "warn", because there is then something to do.
#
# That last case is checked HERE, the same way `ebook kindle status` checks it
# (`mtp.calibre_gui_is_running`), and not inferred from an exception: `detect.find_device`
# returns a device or raises `DeviceNotFound`, full stop — it never raises `DeviceBusy`,
# which only the two BACKENDS raise. A `doctor` that waited for an exception detection
# cannot produce would report "ok, connected (MTP)" on a machine where every single
# `ebook kindle` command fails `device_busy`/exit 3, and telling the user the opposite
# of what the tool does is worse than telling them less.
#
# The MTP driver probe is GATED on an MTP device actually being found, which is what
# keeps a `calibre-debug` subprocess off the session-start path for everyone else.

#: What the probe below prints on success. Checked for in the probe's OUTPUT rather
#: than trusting its exit code: `calibre-debug` exits 0 for plenty of invocations that
#: never reached the driver, and a silent success is exactly the answer this check
#: must not give.
_MTP_PROBE_MARKER = "media-tools-mtp-driver-ok"
#: Imports only — nothing here scans, opens or otherwise touches a device, so this is
#: safe to run at session start with a Kindle attached and mid-transfer.
_MTP_PROBE = (
    "from calibre.devices.mtp.driver import MTP_DEVICE; "
    "from calibre.devices.scanner import DeviceScanner; "
    f"print({_MTP_PROBE_MARKER!r})"
)
_MTP_PROBE_TIMEOUT_S = 30.0
_MTP_DRIVER_HINT = (
    "needed only by `media-tools ebook kindle ...` against a 2024-or-later Kindle "
    "(or a Scribe), which speaks MTP and has no disk to mount: install Calibre "
    "(brew install --cask calibre). A mass-storage Kindle needs none of this"
)
_KINDLE_DEVICE_HINT = (
    "only `media-tools ebook kindle ...` needs one; connect a Kindle over USB and "
    "unlock it. A 2024-or-later model (or a Scribe) shows no disk — that is expected, "
    "it speaks MTP"
)
#: Stable, human-readable labels for `detect.Device.mode`. Deliberately not
#: `device.mode` itself: that is a JSON literal in `ebook kindle status`'s payload, and
#: this line is prose a person reads.
_KINDLE_MODE_LABELS = {"mass_storage": "mass storage", "mtp": "MTP"}


def _kindle_mtp_driver_check(
    device: kindle_detect.Device | None, skip_reason: str = "no MTP Kindle connected"
) -> Check:
    """Whether Calibre's own MTP driver imports inside Calibre's interpreter.

    `tasks.ebook.kindle.mtp` can never import that driver from this process — it only
    loads under Calibre's own Python — so the only honest check is to ask
    `calibre-debug` to import it and say so. The probe imports and prints; it never
    constructs a driver, scans the USB bus or opens anything, so running it costs
    nothing and cannot disturb a device that is mid-transfer.

    **It only runs when an MTP Kindle is actually connected.** Nothing else on this
    machine needs the driver — a mass-storage Kindle does not, and neither does a
    machine with no Kindle at all — so probing regardless would spend a subprocess at
    every session start to produce a warn nobody can act on. `device` is what
    `_kindle_device_check` already found, and `skip_reason` is that same function's
    account of WHY there is nothing to probe. The detail then says the probe was
    skipped, and for which of three different reasons, rather than claiming a driver
    was verified — "no MTP Kindle connected" on a run where detection itself FAILED
    would be a false statement on an "ok" row, since nothing is then known about what
    is attached.
    """
    if device is None or device.mode != "mtp":
        return Check(
            "kindle-mtp-driver",
            "ok",
            f"not probed: {skip_reason}",
            hint=_MTP_DRIVER_HINT,
        )
    exe = calibre.find_tool("calibre-debug")
    if exe is None:
        return Check("kindle-mtp-driver", "warn", "calibre-debug not found", hint=_MTP_DRIVER_HINT)
    try:
        proc = subprocess.run(
            [exe, "-c", _MTP_PROBE],
            capture_output=True,
            text=True,
            timeout=_MTP_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return Check(
            "kindle-mtp-driver",
            "warn",
            f"found at {exe} but it did not run ({error})",
            hint=_MTP_DRIVER_HINT,
        )
    if _MTP_PROBE_MARKER in (proc.stdout or ""):
        return Check("kindle-mtp-driver", "ok", "calibre.devices.mtp.driver imports")
    tail = ((proc.stderr or proc.stdout or "").strip().splitlines() or [""])[-1]
    return Check(
        "kindle-mtp-driver",
        "warn",
        f"calibre.devices.mtp.driver did not import{f' ({tail})' if tail else ''}",
        hint=_MTP_DRIVER_HINT,
    )


def _mtp_gui_holds_the_device() -> bool:
    """Whether Calibre's GUI has an MTP device, by the same check `ebook kindle status`
    reports as `data.device.held_by` and `mtp.MtpBackend._preflight` refuses on. One
    process-table read, on a path that no longer spawns `calibre-debug`; it already
    swallows its own subprocess failures, and the guard here covers the rest, because a
    health check must not die over a question it asked out of helpfulness."""
    try:
        return kindle_mtp.calibre_gui_is_running()
    except Exception:  # noqa: BLE001 - see this function's own docstring
        return False


def _kindle_device_check() -> tuple[Check, kindle_detect.Device | None, str]:
    """`(the check, the device it found or None, why the driver probe should skip)`.
    Whether a Kindle is connected, in which of the two modes, and whether anything else
    already has it.

    The device comes back as well as the check because `_kindle_mtp_driver_check` is
    gated on it — detection runs exactly once per `doctor` run, and the driver probe
    only when there is something that needs the driver. The third value is this
    function's account of why there is nothing to probe, produced here because this is
    the only place that knows which of the three reasons applies.

    **No Kindle connected is `"ok"`, not `"warn"`.** There is nothing to do about it
    unless the user is trying to drive `ebook kindle`, and a warn with no action
    attached is noise in a report whose other warns all have one. The hint still says
    what to do if a Kindle WAS expected.

    **An MTP Kindle that Calibre's GUI is holding IS a warn**, and it is asked about
    directly rather than waited for as an exception: `find_device` returns a device or
    raises `DeviceNotFound` and nothing else, so a `doctor` built around catching
    `DeviceBusy` would never fire — while on that same machine every `ebook kindle`
    command fails `device_busy`/exit 3 at `MtpBackend._preflight`. The device is still
    returned, so the driver probe still runs: whether the driver imports is a separate
    question from who is holding the device, and the probe never opens one.

    Never raises and never reports `"missing"`. Detection shells out to `ioreg` on
    macOS and reads `/sys` on Linux, so the failure modes are real; a broad catch is
    deliberate, because a health check that dies halfway through its own report is
    worse than one that says it could not tell. No serial is ever printed: it
    identifies one physical device, and nothing here needs it — `ebook kindle status`
    is where a user asks for that, deliberately and one command at a time.
    """
    try:
        device = kindle_detect.find_device()
    except kindle_detect.DeviceNotFound:
        return (
            Check("kindle-device", "ok", "no Kindle connected", hint=_KINDLE_DEVICE_HINT),
            None,
            "no Kindle connected",
        )
    except Exception as error:  # noqa: BLE001 - a health check must never die reporting
        return (
            Check(
                "kindle-device",
                "warn",
                f"could not tell whether a Kindle is connected ({error})",
                hint=_KINDLE_DEVICE_HINT,
            ),
            None,
            # NOT "no MTP Kindle connected": nothing at all is known here, and saying
            # otherwise would be a false statement on an "ok" row.
            "no device detected",
        )
    label = _KINDLE_MODE_LABELS.get(device.mode, device.mode)
    skip_reason = f"the connected Kindle is {label}, which needs no MTP driver"
    if device.mode == "mtp" and _mtp_gui_holds_the_device():
        return (
            Check(
                "kindle-device",
                "warn",
                f"connected ({label}), but Calibre's GUI is holding it",
                hint="close Calibre and try again: an MTP device allows exactly one "
                "holder, so every `media-tools ebook kindle` command will fail "
                "device_busy until it is closed",
            ),
            device,
            skip_reason,
        )
    return (
        Check(
            "kindle-device",
            "ok",
            f"connected ({label})",
            hint="`media-tools ebook kindle status` reports it in full",
        ),
        device,
        skip_reason,
    )


def _output_root_check(root: Path) -> Check:
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        return Check("output-root", "missing", str(root), hint=str(error))
    return Check("output-root", "ok", str(root))


# -- --check-updates: PyPI, cached for 24h -------------------------------------------


def _fetch_pypi_version(name: str, *, timeout: float = 5.0) -> str | None:
    """Best-effort: never raises. None means "could not determine"."""
    try:
        url = f"https://pypi.org/pypi/{name}/json"
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            data = json.loads(response.read().decode("utf-8"))
        return data["info"]["version"]
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, OSError):
        return None


def _cache_path(root: Path) -> Path:
    return root / ".cache" / "updates.json"


def _load_fresh_cache(cache_file: Path) -> dict | None:
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - data.get("checked_at", 0) > UPDATE_CACHE_TTL_S:
        return None
    return data.get("latest")


def _save_cache(cache_file: Path, latest: dict) -> None:
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"checked_at": time.time(), "latest": latest}
        cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # a cache write failure must never break `doctor` itself


def _fetch_all_pypi_versions(names: list[str]) -> dict[str, str | None]:
    """Fetch every package concurrently, so a cold cache costs one network round trip
    worth of latency (bounded by `_fetch_pypi_version`'s own timeout) instead of one per
    package - this runs at session start (via --check-updates), where a sequential,
    several-times-5-second worst case would be a real, noticeable delay."""
    with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
        results = pool.map(_fetch_pypi_version, names)
    return dict(zip(names, results, strict=True))


def _update_checks(root: Path, installed: dict[str, Check]) -> list[Check]:
    cache_file = _cache_path(root)
    latest = _load_fresh_cache(cache_file)
    if latest is None:
        latest = _fetch_all_pypi_versions(UPDATE_CHECK_PACKAGES)
        _save_cache(cache_file, latest)

    checks = []
    for name in UPDATE_CHECK_PACKAGES:
        current = installed[name]
        newest = latest.get(name)
        if not newest or current.status != "ok" or newest == current.detail:
            continue
        checks.append(
            Check(
                f"{name}-update",
                "warn",
                f"{current.detail} -> {newest} available",
                hint="media-tools doctor --update",
            )
        )
    return checks


# -- assembling everything -----------------------------------------------------------


def check_all(
    *, check_updates: bool, output_dir: Path | None = None, op_item: str | None = None
) -> list[Check]:
    root = output_root(output_dir)
    package_checks = {name: _package_check(name) for name in PACKAGES}
    kindle_device_check, kindle_device, kindle_skip_reason = _kindle_device_check()

    checks = [
        _python_check(),
        _venv_check(),
        *package_checks.values(),
        _ffmpeg_check(),
        _deno_check(),
        _calibre_check(),
        _openrouter_check(op_item),
        # Detection first, because the driver probe is gated on what it found.
        kindle_device_check,
        _kindle_mtp_driver_check(kindle_device, kindle_skip_reason),
        _output_root_check(root),
    ]
    if check_updates:
        checks.extend(_update_checks(root, package_checks))
    return checks


# -- printing --------------------------------------------------------------------


def _print_table(checks: list[Check], *, quiet: bool) -> None:
    rows = [c for c in checks if not quiet or c.status != "ok"]
    for check in rows:
        line = f"{_STATUS_MARK[check.status]} {check.name}: {check.detail}"
        if check.status != "ok" and check.hint:
            line += f" -- {check.hint}"
        print(line)
    ok = sum(1 for c in checks if c.status == "ok")
    warn = sum(1 for c in checks if c.status == "warn")
    missing = sum(1 for c in checks if c.status == "missing")
    print(f"media-tools doctor: {ok} ok, {warn} warn, {missing} missing")


def _exit_code(checks: list[Check]) -> int:
    return EXIT_DEPENDENCY if any(c.status == "missing" for c in checks) else EXIT_OK


# -- --update: pip in a subprocess, then a fresh re-check ----------------------------


def _install_method() -> str | None:
    """ "pipx"/"uv" when installed by one of those isolated tool installers, else None
    for a plain pip/editable install. Detected from the interpreter's own prefix path,
    since both pipx and `uv tool install` give the tool its own venv under a
    recognisable directory."""
    prefix = str(Path(sys.prefix).resolve()).replace(os.sep, "/")
    if "/pipx/" in prefix:
        return "pipx"
    if "/uv/tools/" in prefix:
        return "uv"
    return None


def _print_update_result(message: str, exit_code: int, *, json_mode: bool) -> None:
    if json_mode:
        payload = {"v": 1, "type": "doctor", "exit_code": exit_code, "message": message}
        print(json.dumps(payload))
    else:
        print(message)


def _run_update(args) -> int:
    method = _install_method()
    if method is not None:
        upgrade_cmd = (
            "pipx upgrade media-tools" if method == "pipx" else "uv tool upgrade media-tools"
        )
        message = f"media-tools was installed with {method}; run: {upgrade_cmd}"
        _print_update_result(message, EXIT_USAGE, json_mode=args.json_mode)
        return EXIT_USAGE

    if not args.json_mode:
        print("upgrading dependencies with pip ...")
    # Always captured: in --json mode, pip's own progress text must never land on
    # stdout next to (or instead of) the JSON result — results on stdout, progress
    # and logs on stderr.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "yt-dlp[default,deno]",
            "imageio-ffmpeg",
        ],
        capture_output=True,
        text=True,
    )
    if not args.json_mode:
        # Calibre is a system tool, never touched by pip; the fix is manual.
        print("brew upgrade --cask calibre  # if Calibre itself needs updating")
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-10:])
        message = (
            f"pip failed to upgrade dependencies: {tail}"
            if tail
            else ("pip failed to upgrade dependencies")
        )
        _print_update_result(message, EXIT_DEPENDENCY, json_mode=args.json_mode)
        return EXIT_DEPENDENCY
    elif not args.json_mode and proc.stdout:
        print(proc.stdout, end="")

    # Re-run the checks in a fresh interpreter: importlib.metadata caches the versions
    # it read on first use, so re-checking in this same process could still report the
    # versions from before the upgrade. --quiet is propagated so `doctor --update
    # --quiet` doesn't suddenly print the full table for this half; --check-updates is
    # deliberately NOT propagated (the upgrade itself already answered that question).
    fresh_argv = [sys.executable, "-m", "media_tools", NAME]
    if args.json_mode:
        fresh_argv.append("--json")
    if getattr(args, "quiet", False):
        fresh_argv.append("--quiet")
    if getattr(args, "output_dir", None):
        fresh_argv += ["-o", str(args.output_dir)]
    if getattr(args, "op_item", None):
        fresh_argv += ["--op-item", args.op_item]
    fresh = subprocess.run(fresh_argv, capture_output=args.json_mode, text=True)
    if args.json_mode:
        if fresh.stdout:
            sys.stdout.write(fresh.stdout)
        if fresh.stderr:
            sys.stderr.write(fresh.stderr)
    return fresh.returncode


def run(args) -> int:
    if args.update:
        return _run_update(args)

    checks = check_all(
        check_updates=args.check_updates,
        output_dir=args.output_dir,
        op_item=getattr(args, "op_item", None),
    )
    exit_code = _exit_code(checks)

    if args.json_mode:
        payload = {
            "v": 1,
            "type": "doctor",
            "exit_code": exit_code,
            "checks": [asdict(c) for c in checks],
        }
        print(json.dumps(payload, ensure_ascii=False))
    else:
        _print_table(checks, quiet=args.quiet)

    return exit_code
