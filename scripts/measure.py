#!/usr/bin/env python3
"""Measure one media-tools job inside the container, from the container's own cgroup.

Sizing method (docs/specs/2026-09-25-media-tools-container-design.md, "Resources"):

  step 1  run with the prod CPU cap and NO memory cap; sample the cgroup
  step 3  re-run with the candidate memory cap; it stands only if the job completes,
          OOMKilled is false, and wall time stays within 1.5x of step 1

`memory.stat` reports current values, not peaks, so anon/file/shmem are sampled every
SAMPLE_S and their maxima kept. `memory.peak`, `memory.swap.peak` and `pids.peak` are
kernel-kept watermarks, read once at the end. Everything goes to stdout as one JSON
object, so a run is a row in the spec's table.

    scripts/measure.py --image media-tools:local --volume mt-measure \\
        [--memory 768m] -- compress /data/in/clip.mp4 --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SAMPLE_S = 0.1
CGROUP_ROOT = Path("/sys/fs/cgroup/system.slice")
STAT_KEYS = ("anon", "file", "shmem")


def _docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _stat(cgroup: Path) -> dict[str, int]:
    values = {}
    try:
        for line in (cgroup / "memory.stat").read_text().splitlines():
            key, _, value = line.partition(" ")
            if key in STAT_KEYS:
                values[key] = int(value)
    except OSError:
        pass
    return values


def _parse_time(stamp: str) -> datetime:
    # Docker reports nanoseconds; fromisoformat takes at most microseconds.
    head, _, frac = stamp.rstrip("Z").partition(".")
    return datetime.fromisoformat(f"{head}.{(frac + '000000')[:6]}+00:00")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True)
    parser.add_argument("--volume", required=True, help="named volume mounted at /data")
    parser.add_argument("--memory", help="memory cap (step 3); memswap is set equal")
    parser.add_argument("--cpus", default="1.0")
    parser.add_argument("--label", default="")
    parser.add_argument("job", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    job = args.job[1:] if args.job[:1] == ["--"] else args.job

    # A named container is attributable while it runs: anyone auditing the box sees a
    # measurement, not something unexplained.
    slug = "".join(c if c.isalnum() else "-" for c in (args.label or job[0]).lower()).strip("-")
    name = f"media-tools-measure-{slug}-{int(time.time())}"
    run = [
        "run", "-d", "--name", name,
        "--read-only", "--tmpfs", "/tmp:size=64m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--cpus", args.cpus,
        "-v", f"{args.volume}:/data",
        "-e", "TMPDIR=/data/tmp", "-e", "HOME=/data/tmp",
    ]  # fmt: skip
    if args.memory:
        run += ["--memory", args.memory, "--memory-swap", args.memory]
    cid = _docker(*run, args.image, *job)
    cgroup = CGROUP_ROOT / f"docker-{cid}.scope"

    peaks = dict.fromkeys(STAT_KEYS, 0)
    watermarks: dict[str, int | None] = {}
    while True:
        for key, value in _stat(cgroup).items():
            peaks[key] = max(peaks[key], value)
        # Read the watermarks while the cgroup still exists; the last good read wins.
        for name in ("memory.peak", "memory.swap.peak", "pids.peak"):
            value = _read_int(cgroup / name)
            if value is not None:
                watermarks[name] = value
        if _docker("inspect", "--format", "{{.State.Running}}", cid) != "true":
            break
        time.sleep(SAMPLE_S)

    state = json.loads(_docker("inspect", "--format", "{{json .State}}", cid))
    logs = subprocess.run(["docker", "logs", cid], capture_output=True, text=True).stdout
    _docker("rm", cid)
    wall = (_parse_time(state["FinishedAt"]) - _parse_time(state["StartedAt"])).total_seconds()

    last = logs.strip().splitlines()[-1] if logs.strip() else ""
    try:
        result = json.loads(last)
    except json.JSONDecodeError:
        result = {}
    mib = 1024 * 1024
    row = {
        "label": args.label,
        "job": " ".join(job),
        "memory_cap": args.memory,
        "cpus": args.cpus,
        "exit_code": state["ExitCode"],
        "oom_killed": state["OOMKilled"],
        "result_ok": result.get("ok"),
        "wall_s": round(wall, 1),
        "anon_peak_mib": round(peaks["anon"] / mib, 1),
        "file_peak_mib": round(peaks["file"] / mib, 1),
        "shmem_peak_mib": round(peaks["shmem"] / mib, 1),
        "memory_peak_mib": round((watermarks.get("memory.peak") or 0) / mib, 1),
        "swap_peak_mib": round((watermarks.get("memory.swap.peak") or 0) / mib, 1),
        "pids_peak": watermarks.get("pids.peak"),
    }
    print(json.dumps(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
