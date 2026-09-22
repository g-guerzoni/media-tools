"""Finding ffmpeg, reading a file's duration without ffprobe, and running with progress.

The bundled ffmpeg build (via `imageio-ffmpeg`) ships no `ffprobe` binary, so duration
and bitrate are read by parsing the header `ffmpeg -i <path>` prints to stderr before it
complains that no output file was given (exit code 1) — never by invoking a non-existent
`ffprobe`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from media_tools.core.engine import Dependency

_DURATION = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d\.\d+)")
_BITRATE = re.compile(r"bitrate:\s*(\d+)\s*kb/s")
_OUT_TIME = re.compile(r"out_time_us=(\d+)")


def ffmpeg_exe() -> str:
    """The ffmpeg binary to run: env override, then PATH, then the bundled build."""
    from_env = os.environ.get("MEDIA_TOOLS_FFMPEG")
    if from_env:
        return from_env
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


FFMPEG = Dependency(
    name="ffmpeg",
    locate=lambda: ffmpeg_exe(),
    install_hint="reinstall the package: pip install -e . (ffmpeg ships with imageio-ffmpeg)",
)


@dataclass
class Probe:
    duration_s: float | None
    bitrate_bps: int | None


def probe(path: Path) -> Probe:
    """Read duration and bitrate from `ffmpeg -i`'s stderr.

    ffmpeg exits 1 here because no output file was given — that is expected, not a
    failure. A file ffmpeg cannot open at all (not media, or corrupt) simply has no
    `Duration:` line to match, so both fields come back None rather than raising.
    """
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True,
        text=True,
    )
    text = proc.stderr

    duration = None
    match = _DURATION.search(text)
    if match:
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    bitrate = None
    match = _BITRATE.search(text)
    if match:
        bitrate = int(match.group(1)) * 1000

    return Probe(duration_s=duration, bitrate_bps=bitrate)


def run_ffmpeg(
    argv: list[str],
    *,
    total_s: float | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> tuple[int, str]:
    """Run ffmpeg, optionally reporting progress as a percentage of `total_s`.

    Returns (returncode, stderr tail) — the last few lines of stderr, useful for an
    error report without dumping the whole log.
    """
    command = [*argv[:1], "-hide_banner", "-nostdin", *argv[1:]]
    if on_progress is not None:
        command = [*command, "-progress", "pipe:1", "-nostats"]

    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE if on_progress is not None else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Drain stderr on its own thread: on a real (possibly noisy) file, reading only
    # stdout's progress lines while stderr fills its OS pipe buffer would deadlock the
    # process against us reading it.
    stderr_lines: list[str] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)

    drainer = threading.Thread(target=_drain_stderr, daemon=True)
    drainer.start()

    if on_progress is not None and proc.stdout is not None:
        for line in proc.stdout:
            match = _OUT_TIME.match(line.strip())
            if match and total_s:
                percent = min(100.0, (int(match.group(1)) / 1_000_000) / total_s * 100)
                on_progress(percent)

    proc.wait()
    drainer.join()
    tail = "\n".join("".join(stderr_lines).splitlines()[-10:])
    return proc.returncode, tail
