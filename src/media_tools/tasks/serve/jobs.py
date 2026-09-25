"""Job state, the worker pool that runs jobs, and the janitor that bounds the volume.

Layout under the data root (`/data` in the container):

    in/<caller>/        the caller's inputs; the only place its jobs may read
    out/<caller>/       its output root: batches, run.json, its own .cache/
    jobs/<id>/          job.json (the record), events.jsonl (the CLI's stdout), stderr.log
    tmp/<id>/           the job's TMPDIR and HOME, removed when the job ends

A job is a subprocess of the real CLI (`python -m media_tools ...`), so the CLI stays
the single source of truth and its JSON Lines events are the API's events, unchanged.
The job gets its own session, and cancelling it sends SIGINT to the whole group, as
Ctrl+C at a terminal would: the tool's own exit-130 path runs, and ffmpeg/Calibre
children stop with it.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from media_tools.core.inputs import INPUT_ROOT_ENV
from media_tools.core.paths import fsync_replace

FINISHED = ("done", "failed", "cancelled", "timed_out", "interrupted")
KILL_GRACE_S = 30
EVENTS_PAGE = 1000


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_s(stamp: str | None) -> float:
    if not stamp:
        return 0.0
    then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (datetime.now(UTC) - then).total_seconds()


@dataclass
class Settings:
    data: Path
    max_jobs: int = 1
    max_workers: int = 1
    job_timeout_s: float = 6 * 3600
    retention_s: float = 30 * 24 * 3600
    max_data_bytes: int = 30 * 10**9
    sweep_s: float = 600


@dataclass
class Store:
    settings: Settings
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _queue: queue.Queue = field(default_factory=queue.Queue)
    _procs: dict[str, subprocess.Popen] = field(default_factory=dict)
    _cancelled: set[str] = field(default_factory=set)
    usage_bytes: int = 0
    janitor_last_run: float | None = None

    # -- paths ---------------------------------------------------------------------

    def input_root(self, caller: str) -> Path:
        return self.settings.data / "in" / caller

    def output_root(self, caller: str) -> Path:
        return self.settings.data / "out" / caller

    def job_dir(self, job_id: str) -> Path:
        return self.settings.data / "jobs" / job_id

    def scratch(self, job_id: str) -> Path:
        return self.settings.data / "tmp" / job_id

    def prepare_caller(self, caller: str) -> None:
        self.input_root(caller).mkdir(parents=True, exist_ok=True)
        self.output_root(caller).mkdir(parents=True, exist_ok=True)

    # -- records -------------------------------------------------------------------

    def read(self, job_id: str) -> dict | None:
        if not job_id.isalnum():
            return None
        try:
            return json.loads((self.job_dir(job_id) / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, record: dict) -> None:
        target = self.job_dir(record["id"]) / "job.json"
        temp = target.with_suffix(".json.partial")
        temp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        fsync_replace(temp, target)

    def _update(self, job_id: str, **changes) -> dict:
        with self._lock:
            record = self.read(job_id) or {}
            record.update(changes)
            self._write(record)
            return record

    def all_records(self) -> list[dict]:
        root = self.settings.data / "jobs"
        records = [self.read(d.name) for d in root.iterdir()] if root.is_dir() else []
        return [r for r in records if r]

    def submit(self, caller: str, request: dict, argv: list[str]) -> dict:
        job_id = secrets.token_hex(8)
        self.job_dir(job_id).mkdir(parents=True)
        record = {
            "id": job_id,
            "caller": caller,
            "request": request,
            "argv": argv,
            "status": "queued",
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "result": None,
        }
        with self._lock:
            self._write(record)
        self._queue.put(job_id)
        return record

    def events(self, job_id: str, after: int) -> tuple[list[dict], int]:
        try:
            lines = (self.job_dir(job_id) / "events.jsonl").read_text(encoding="utf-8")
        except OSError:
            return [], after
        rows = [line for line in lines.splitlines() if line.strip()]
        page = rows[after : after + EVENTS_PAGE]
        events = []
        for line in page:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue  # a line still being written
        return events, after + len(page)

    # -- lifecycle -----------------------------------------------------------------

    def recover(self) -> None:
        """At startup: a job found `running` died with the previous server, so it is
        `interrupted` (re-submitting the same request resumes its batch); a job still
        `queued` is queued again."""
        for record in sorted(self.all_records(), key=lambda r: r["created_at"]):
            if record["status"] == "running":
                self._update(record["id"], status="interrupted", finished_at=_now())
                shutil.rmtree(self.scratch(record["id"]), ignore_errors=True)
            elif record["status"] == "queued":
                self._queue.put(record["id"])

    def start_workers(self) -> None:
        for _ in range(self.settings.max_jobs):
            threading.Thread(target=self._worker, daemon=True).start()

    def cancel(self, job_id: str) -> dict | None:
        with self._lock:
            record = self.read(job_id)
            if record is None or record["status"] in FINISHED:
                return record
            self._cancelled.add(job_id)
            proc = self._procs.get(job_id)
            if record["status"] == "queued":
                record.update(status="cancelled", finished_at=_now())
                self._write(record)
                return record
        if proc is not None:
            _signal_group(proc, signal.SIGINT)
        return self.read(job_id)

    def _worker(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._run(job_id)
            except Exception as error:  # keep the worker alive; record what happened
                self._update(job_id, status="failed", finished_at=_now(), error=repr(error))

    def _run(self, job_id: str) -> None:
        with self._lock:
            record = self.read(job_id)
            if record is None or record["status"] != "queued" or job_id in self._cancelled:
                return
        caller = record["caller"]
        scratch = self.scratch(job_id)
        scratch.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "TMPDIR": str(scratch),
            "HOME": str(scratch),
            INPUT_ROOT_ENV: str(self.input_root(caller)),
        }
        env.pop("MEDIA_TOOLS_OUT", None)
        job_dir = self.job_dir(job_id)
        with (
            open(job_dir / "events.jsonl", "wb") as stdout,
            open(job_dir / "stderr.log", "wb") as stderr,
        ):
            proc = subprocess.Popen(
                [sys.executable, "-m", "media_tools", *record["argv"]],
                stdout=stdout,
                stderr=stderr,
                stdin=subprocess.DEVNULL,
                cwd=self.input_root(caller),
                env=env,
                start_new_session=True,
            )
            with self._lock:
                self._procs[job_id] = proc
            self._update(job_id, status="running", started_at=_now(), pid=proc.pid)
            timed_out = False
            try:
                proc.wait(timeout=self.settings.job_timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                _signal_group(proc, signal.SIGINT)
                try:
                    proc.wait(timeout=KILL_GRACE_S)
                except subprocess.TimeoutExpired:
                    _signal_group(proc, signal.SIGKILL)
                    proc.wait()
        with self._lock:
            self._procs.pop(job_id, None)
            cancelled = job_id in self._cancelled
        shutil.rmtree(scratch, ignore_errors=True)
        events, _ = self.events(job_id, 0)
        result = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if timed_out:
            status = "timed_out"
        elif cancelled:
            status = "cancelled"
        else:
            status = "done" if proc.returncode == 0 else "failed"
        self._update(
            job_id,
            status=status,
            finished_at=_now(),
            exit_code=proc.returncode,
            result=result,
        )

    # -- the janitor: retention and the size bound ----------------------------------

    def sweep(self) -> None:
        """Delete what has outlived retention, then re-measure the volume.

        An expired job takes its record and its batch with it, unless a job still
        inside retention names the same batch (re-submitting a request reuses its
        batch). A caller with a job queued or running keeps every batch this sweep: that
        job has no result yet, so which batch it will write is not known. Inputs older
        than retention go too. `.cache/` is left alone: it is bounded by the size cap,
        not by age."""
        keep = self.settings.retention_s
        records = self.all_records()
        busy = {r["caller"] for r in records if r["status"] not in FINISHED}
        live_batches = {
            _batch_of(r) for r in records if not (r["status"] in FINISHED and _expired(r, keep))
        }
        for record in records:
            if record["status"] not in FINISHED or not _expired(record, keep):
                continue
            batch = _batch_of(record)
            out = self.output_root(record["caller"])
            if (
                batch
                and batch not in live_batches
                and record["caller"] not in busy
                and _inside(batch, out)
            ):
                shutil.rmtree(batch, ignore_errors=True)
            shutil.rmtree(self.job_dir(record["id"]), ignore_errors=True)
            shutil.rmtree(self.scratch(record["id"]), ignore_errors=True)
        _expire_inputs(self.settings.data / "in", keep)
        self.usage_bytes = _usage(self.settings.data)
        self.janitor_last_run = time.time()

    def over_cap(self) -> bool:
        if self.usage_bytes <= self.settings.max_data_bytes:
            return False
        self.sweep()
        return self.usage_bytes > self.settings.max_data_bytes

    def start_janitor(self) -> None:
        def loop() -> None:
            while True:
                try:
                    self.sweep()
                except Exception as error:  # a failed sweep must show up in /healthz
                    print(f"janitor: sweep failed: {error!r}", file=sys.stderr)
                time.sleep(self.settings.sweep_s)

        threading.Thread(target=loop, daemon=True).start()

    def janitor_fresh(self) -> bool:
        last = self.janitor_last_run
        return last is not None and time.time() - last < 2 * self.settings.sweep_s


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


def _expired(record: dict, keep_s: float) -> bool:
    return _age_s(record.get("finished_at") or record.get("created_at")) > keep_s


def _batch_of(record: dict) -> Path | None:
    run_file = (record.get("result") or {}).get("run_file")
    return Path(run_file).parent if run_file else None


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return path.resolve() != parent.resolve()


def _expire_inputs(root: Path, keep_s: float) -> None:
    if not root.is_dir():
        return
    cutoff = time.time() - keep_s
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            path = Path(dirpath) / name
            try:
                if path.lstat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass
        # Empty folders go too, but never a caller's own input root.
        here = Path(dirpath)
        if here != root and here.parent != root and not os.listdir(dirpath):
            with contextlib.suppress(OSError):
                os.rmdir(dirpath)


def _usage(root: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            with contextlib.suppress(OSError):
                total += (Path(dirpath) / name).lstat().st_size
    return total
