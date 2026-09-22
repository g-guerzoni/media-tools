"""run.json: the batch's book-keeping, and the lock that keeps two runs apart."""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import UTC, datetime
from pathlib import Path

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
    def open(cls, batch_dir: Path, *, task: str, options: dict, inputs: list[str]) -> RunState:
        batch_dir = Path(batch_dir)
        batch_dir.mkdir(parents=True, exist_ok=True)
        run_file = batch_dir / "run.json"

        if run_file.is_file():
            existing = json.loads(run_file.read_text(encoding="utf-8"))
            if existing.get("task") != task:
                raise BatchTaskMismatch(
                    f"batch {batch_dir.name!r} belongs to task {existing.get('task')!r}; "
                    f"choose another --batch"
                )

        cls._acquire(batch_dir)
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
                "input": str(input),
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
        item = self.data["items"][item_id - 1]
        item.update(fields)
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
        self._write(force=True)

    # -- persistence ------------------------------------------------------
    def _touch(self) -> None:
        self._dirty_items += 1
        elapsed = time.monotonic() - self._last_write
        if self._dirty_items >= WRITE_EVERY_ITEMS or elapsed >= WRITE_EVERY_SECONDS:
            self._write()

    def _write(self, force: bool = False) -> None:
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
