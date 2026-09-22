"""run.json: the batch's book-keeping, and the lock that keeps two runs apart."""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import UTC, datetime
from pathlib import Path

from media_tools.core.redact import redact

WRITE_EVERY_SECONDS = 5.0
WRITE_EVERY_ITEMS = 25


class BatchInUse(RuntimeError):
    """Another live run owns this batch."""


class BatchTaskMismatch(RuntimeError):
    """This batch belongs to a different task."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


class RunState:
    def __init__(self, batch_dir: Path, data: dict) -> None:
        self.batch_dir = batch_dir
        self.data = data
        self._dirty_items = 0
        self._last_write = 0.0

    # -- lifecycle --------------------------------------------------------
    @classmethod
    def open(
        cls,
        batch_dir: Path,
        *,
        task: str,
        options: dict,
        inputs: list[str],
        force: bool = False,
    ) -> RunState:
        batch_dir = Path(batch_dir)
        batch_dir.mkdir(parents=True, exist_ok=True)
        run_file = batch_dir / "run.json"

        if run_file.is_file():
            try:
                existing = json.loads(run_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                # A corrupt run.json must not crash the task with a raw JSONDecodeError
                # (or an OSError reading it) — it makes the batch unusable, same as a
                # real task mismatch, and the caller already turns this into a clean
                # `error` + `result` pair and exit 2.
                raise BatchTaskMismatch(
                    f"batch {batch_dir.name!r}'s run.json is unreadable ({error}); "
                    f"pass --batch with another name, or delete {run_file} and re-run"
                ) from error
            if not isinstance(existing, dict):
                # Valid JSON (e.g. `[]`, `"hello"`, `null`, `42`) but not the object
                # run.json is supposed to hold — just as unusable as a parse failure.
                raise BatchTaskMismatch(
                    f"batch {batch_dir.name!r}'s run.json did not contain a JSON object "
                    f"(got {type(existing).__name__}); pass --batch with another name, "
                    f"or delete {run_file} and re-run"
                )
            if existing.get("task") != task:
                raise BatchTaskMismatch(
                    f"batch {batch_dir.name!r} belongs to task {existing.get('task')!r}; "
                    f"choose another --batch"
                )
            if existing.get("engine_options") != options and not force:
                raise BatchTaskMismatch(
                    f"batch {batch_dir.name!r} already ran with different options "
                    f"({existing.get('engine_options')!r}) than this run's "
                    f"({options!r}); pass --force to reuse it anyway (this updates the "
                    f"recorded options), or choose another --batch"
                )

        cls._acquire(batch_dir)
        # Rebuild from scratch on each run; resumption uses disk outputs, not item list.
        data = {
            "v": 1,
            "task": task,
            "engine_options": options,
            "batch": batch_dir.name,
            "output_dir": str(batch_dir),
            "status": "running",
            "owner": {"pid": os.getpid(), "host": socket.gethostname(), "since": _now()},
            "created_at": _now(),
            "updated_at": _now(),
            "inputs": list(inputs),
            "counts": {"total": 0, "done": 0, "skipped": 0, "failed": 0, "pending": 0},
            "items": [],
        }
        state = cls(batch_dir, data)
        state._write()
        return state

    @staticmethod
    def _acquire(batch_dir: Path) -> None:
        lock = batch_dir / ".lock"
        payload = json.dumps({"pid": os.getpid(), "host": socket.gethostname()})
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                held = json.loads(lock.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                held = {}
            if _pid_alive(int(held.get("pid", -1))) and held.get("host") == socket.gethostname():
                msg = f"batch {batch_dir.name!r} is in use by pid {held.get('pid')}"
                raise BatchInUse(msg) from None
            lock.unlink(missing_ok=True)
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)

    def release(self) -> None:
        (self.batch_dir / ".lock").unlink(missing_ok=True)

    def __enter__(self) -> RunState:
        return self

    def __exit__(self, *exc) -> None:
        if self.data["status"] == "running":
            self.finish("interrupted" if exc[0] is KeyboardInterrupt else "failed")
        self.release()

    # -- items ------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self.batch_dir / "run.json"

    def add_item(self, input: str) -> int:
        item_id = len(self.data["items"]) + 1
        self.data["items"].append(
            {
                "id": item_id,
                "input": redact(str(input)),
                "status": "pending",
                "reason": None,
                "outputs": [],
                "bytes_in": None,
                "elapsed_s": None,
                "warnings": [],
                "data": {},
            }
        )
        self._touch()
        return item_id

    def update(self, item_id: int, **fields) -> None:
        if item_id < 1:
            raise ValueError(f"item_id must be >= 1, got {item_id}")
        item = self.data["items"][item_id - 1]
        item.update(redact(fields))
        self._touch()

    def counts(self) -> dict:
        counts = {
            "total": len(self.data["items"]),
            "done": 0,
            "skipped": 0,
            "failed": 0,
            "pending": 0,
        }
        for item in self.data["items"]:
            counts[item["status"]] += 1
        return counts

    def finish(self, status: str) -> None:
        self.data["status"] = status
        self.data["owner"] = None
        self._write()
        self.release()

    # -- persistence ------------------------------------------------------
    def _touch(self) -> None:
        self._dirty_items += 1
        elapsed = time.monotonic() - self._last_write
        if self._dirty_items >= WRITE_EVERY_ITEMS or elapsed >= WRITE_EVERY_SECONDS:
            self._write()

    def _write(self) -> None:
        self.data["counts"] = self.counts()
        self.data["updated_at"] = _now()
        temp = self.path.parent / ".run.json.partial"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)
        self._dirty_items = 0
        self._last_write = time.monotonic()


def _peek_run_json(batch_dir: Path) -> tuple[bool, dict | None]:
    """Look at `batch_dir`'s run.json without opening/locking the batch.

    Returns `(exists, data)`: `exists` is False only when there is no run.json file at
    all (a genuinely free name). `exists=True, data=None` means a run.json is present
    but unreadable (corrupt JSON, or valid JSON that is not an object) — occupied, but
    with nothing to compare against, so it counts as "does not match" rather than
    "free"."""
    run_file = batch_dir / "run.json"
    if not run_file.is_file():
        return False, None
    try:
        data = json.loads(run_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, None
    return True, (data if isinstance(data, dict) else None)


def resolve_batch_name(root: Path, base_name: str, *, task: str, options: dict) -> str:
    """The name to use for an AUTO-DERIVED batch (no explicit `--batch`): `base_name`
    itself when it is free or already belongs to a run with this task and these options
    (a resumable batch), else `f"{base_name}-2"`, `-3`, ... until one of those two is
    true (spec 7.1: "If a generated name exists but its run.json does not match this
    run's task and options, -2, -3, ... is appended."). An explicit `--batch NAME` never
    goes through this — reusing it with different options is `RunState.open`'s job to
    refuse (or, with `--force`, to update in place)."""
    root = Path(root)
    name = base_name
    suffix = 1
    while True:
        exists, data = _peek_run_json(root / name)
        if not exists:
            return name
        if data is not None and data.get("task") == task and data.get("engine_options") == options:
            return name
        suffix += 1
        name = f"{base_name}-{suffix}"
