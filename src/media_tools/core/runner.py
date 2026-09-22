"""The per-item loop every file task shares."""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from media_tools.core.engine import Engine, select_engine
from media_tools.core.events import (
    EXIT_DEPENDENCY,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    Reporter,
)
from media_tools.core.inputs import Source
from media_tools.core.paths import mirror_output, temp_path
from media_tools.core.state import BatchInUse, BatchTaskMismatch, RunState


@dataclass
class Item:
    id: int
    source: Path
    root: Path | None
    outputs: list[Path]
    engine: Engine | None = None


@dataclass
class Context:
    batch_dir: Path
    reporter: Reporter
    dry_run: bool = False
    force: bool = False
    deps: dict[str, Any] = field(default_factory=dict)
    total_items: int = 0


@dataclass
class Outcome:
    status: str
    outputs: list[Path]
    bytes_out: int | None
    reason: str | None = None
    warnings: list[str] | None = None
    data: dict | None = None


def _plan(
    sources: list[Source], engines: list[Engine], to: str | None, args, batch_dir: Path
) -> list[tuple[Item, str | None]]:
    """Build items, pick each one's engine, and flag the ones that cannot run."""
    planned: list[tuple[Item, str | None]] = []
    claimed: dict[Path, Path] = {}
    for index, source in enumerate(sources, start=1):
        engine = select_engine(engines, source.path, to)
        if engine is None:
            planned.append(
                (
                    Item(id=index, source=source.path, root=source.root, outputs=[]),
                    "unsupported_input",
                )
            )
            continue
        names = engine.output_names(source.path, args)
        outputs = [mirror_output(source.path, source.root, batch_dir, name) for name in names]
        item = Item(id=index, source=source.path, root=source.root, outputs=outputs, engine=engine)
        problem = None
        for output in outputs:
            if output.resolve() == source.path.resolve():
                problem = "output_equals_input"
            elif output in claimed:
                problem = "output_collision"
            else:
                claimed[output] = source.path
        planned.append((item, problem))
    return planned


def _clear_partials(batch_dir: Path) -> None:
    """Remove any leftover `.partial` temp file left by a crashed or interrupted run."""
    for stale in batch_dir.rglob(".*.partial"):
        stale.unlink(missing_ok=True)


def build_result(state: RunState, batch_dir: Path, exit_code: int) -> dict:
    """The final `result` payload, built from whatever state holds right now. Public: it
    has nothing file-task-specific in it (just `state`/`batch_dir`/`exit_code`), so
    `download`'s own item loop — which does not use `run_items` but emits the same
    `result` shape by hand — shares this instead of hand-syncing its own copy."""
    return {
        "ok": exit_code == EXIT_OK,
        "exit_code": exit_code,
        "counts": state.counts(),
        "failed": [
            {"id": i["id"], "input": i["input"], "reason": i["reason"]}
            for i in state.data["items"]
            if i["status"] == "failed"
        ],
        "pending": [i["input"] for i in state.data["items"] if i["status"] == "pending"],
        "outputs": [str(batch_dir / o["path"]) for i in state.data["items"] for o in i["outputs"]],
        "run_file": state.path,
    }


def empty_result(exit_code: int) -> dict:
    """The `result` payload for a run that never got to own a batch (a batch conflict):
    no `run.json` was ever this run's to point at, so `run_file` stays null. Public for
    the same reason as `build_result` above."""
    return {
        "ok": False,
        "exit_code": exit_code,
        "counts": {"total": 0, "done": 0, "skipped": 0, "failed": 0, "pending": 0},
        "failed": [],
        "pending": [],
        "outputs": [],
        "run_file": None,
    }


def write_summary_json(summary_json: Path | None, result: dict) -> None:
    """`--summary-json PATH`: write the same object as the final `result` event to a
    file, as `{"v": 1, "type": "result", ...}`. A no-op when `summary_json` is None.
    Shared by `run_items` and `download`'s own loop so the two never drift apart."""
    if summary_json is None:
        return
    payload = {"v": 1, "type": "result", **result, "run_file": str(result["run_file"])}
    Path(summary_json).parent.mkdir(parents=True, exist_ok=True)
    Path(summary_json).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def run_items(
    sources: list[Source],
    *,
    task: str,
    engines: list[Engine],
    to: str | None = None,
    args: Any,
    reporter: Reporter,
    batch_dir: Path,
    stages: list[str],
    options: dict,
    dry_run: bool = False,
    force: bool = False,
    stop_on_error: bool = False,
    deps: dict[str, Any] | None = None,
    summary_json: Path | None = None,
) -> int:
    """Plan every source, then process it through its engine, updating state as it goes."""
    planned = _plan(sources, engines, to, args, batch_dir)
    reporter.start(
        tool=task,
        batch=batch_dir.name,
        output_dir=batch_dir,
        stages=stages,
        items=len(planned),
        options=options,
    )

    if dry_run:
        return _report_dry_run(planned, reporter)

    try:
        state_cm = RunState.open(
            batch_dir,
            task=task,
            options=options,
            inputs=[str(s.path) for s in sources],
            force=force,
        )
    except BatchInUse as error:
        reporter.error(code="batch_in_use", message=str(error))
        reporter.result(**empty_result(EXIT_USAGE))
        return EXIT_USAGE
    except BatchTaskMismatch as error:
        reporter.error(code="batch_task_mismatch", message=str(error))
        reporter.result(**empty_result(EXIT_USAGE))
        return EXIT_USAGE
    except OSError as error:
        reporter.error(
            code="output_not_writable",
            message=f"cannot create batch directory {batch_dir}: {error}",
            hint="pass -o/--output-dir to a writable location, or fix permissions on this one",
        )
        reporter.result(**empty_result(EXIT_DEPENDENCY))
        return EXIT_DEPENDENCY

    ctx = Context(
        batch_dir=batch_dir,
        reporter=reporter,
        dry_run=dry_run,
        force=force,
        deps=deps or {},
        total_items=len(planned),
    )
    exit_code = EXIT_OK
    # `stages` (declared in `start`) is at least [scan/plan-phase, processing-phase];
    # emit both here since both are generic to every file task. A task with more than
    # two declared stages (only `split`, today: scan/split/verify) gets its remaining
    # ones emitted after the per-item loop below, in the same declared order.
    reporter.stage(stage=stages[0], index=1, count=len(stages))
    if len(stages) > 1:
        reporter.stage(stage=stages[1], index=2, count=len(stages))
    with state_cm as state:
        _clear_partials(batch_dir)
        # Register every planned item up front (as "pending") so that items never reached
        # because of --stop-on-error, or because Ctrl+C landed before them, stay pending
        # in run.json instead of being missing from it.
        for item, _ in planned:
            state.add_item(str(item.source))

        try:
            for item, problem in planned:
                bytes_in = item.source.stat().st_size if item.source.exists() else None

                if problem is not None:
                    state.update(item.id, status="failed", reason=problem, bytes_in=bytes_in)
                    reporter.item(
                        id=item.id,
                        status="failed",
                        input=item.source,
                        outputs=[],
                        bytes_in=bytes_in,
                        reason=problem,
                    )
                    exit_code = EXIT_FAILED
                    if stop_on_error:
                        break
                    continue

                if not force and all(o.exists() for o in item.outputs):
                    state.update(
                        item.id,
                        status="skipped",
                        reason="exists",
                        bytes_in=bytes_in,
                        outputs=[
                            {"path": str(o.relative_to(batch_dir)), "bytes": o.stat().st_size}
                            for o in item.outputs
                        ],
                    )
                    reporter.item(
                        id=item.id,
                        status="skipped",
                        input=item.source,
                        outputs=item.outputs,
                        bytes_in=bytes_in,
                        reason="exists",
                    )
                    continue

                try:
                    for output in item.outputs:
                        output.parent.mkdir(parents=True, exist_ok=True)
                    outcome = item.engine.process(item, ctx)
                except Exception as error:
                    # A single broken engine (a vanished source, a library crash, ...) must
                    # never take the rest of the batch down with it.
                    outcome = Outcome(
                        status="failed",
                        outputs=[],
                        bytes_out=None,
                        reason="engine_error",
                        data={"error_type": type(error).__name__, "error_message": str(error)},
                    )

                if outcome.status == "failed":
                    for output in item.outputs:
                        # Best-effort: now that the mkdir above can itself be the reason
                        # this item failed (C1), `output.parent` may not exist as a
                        # directory at all (e.g. a mirrored subfolder that collided with
                        # a same-named file) — unlink can then raise NotADirectoryError,
                        # which `missing_ok` does not cover. Cleanup failing must never
                        # mask the real failure or crash the batch.
                        for candidate in (temp_path(output), output):
                            with contextlib.suppress(OSError):
                                candidate.unlink(missing_ok=True)
                    exit_code = EXIT_FAILED
                item_failed = outcome.status == "failed"
                try:
                    state.update(
                        item.id,
                        status=outcome.status,
                        reason=outcome.reason,
                        bytes_in=bytes_in,
                        warnings=outcome.warnings or [],
                        data=outcome.data or {},
                        outputs=[
                            {
                                "path": str(Path(o).relative_to(batch_dir)),
                                "bytes": Path(o).stat().st_size,
                            }
                            for o in outcome.outputs
                            if Path(o).exists()
                        ],
                    )
                    reporter.item(
                        id=item.id,
                        status=outcome.status,
                        input=item.source,
                        outputs=outcome.outputs,
                        bytes_in=bytes_in,
                        bytes_out=outcome.bytes_out,
                        reason=outcome.reason,
                        warnings=outcome.warnings,
                    )
                except Exception as error:
                    # The engine's own Outcome was malformed (a status/reason/warning
                    # code outside the closed registry `Reporter` enforces, or an output
                    # path this item's bookkeeping choked on) — the engine's fault just
                    # as much as a raised exception above, and must not escape run_items
                    # entirely: unlike an exception from `process()` itself, this one
                    # happens after `state`/`reporter` already started describing the
                    # item, so left uncaught it unwinds past `with state_cm as state:`
                    # (marking the whole batch "failed" on the way out) and past
                    # `reporter.result(...)` below, leaving `start`/`stage` printed with
                    # no matching `result` — exactly the guarantee this module exists to
                    # keep. Overwrite whatever the state.update() above may have already
                    # written with an honest failure instead, and still emit a
                    # registry-safe `item` event so no item silently vanishes from the
                    # stream.
                    exit_code = EXIT_FAILED
                    item_failed = True
                    state.update(
                        item.id,
                        status="failed",
                        reason="engine_error",
                        bytes_in=bytes_in,
                        warnings=[],
                        data={"error_type": type(error).__name__, "error_message": str(error)},
                        outputs=[],
                    )
                    reporter.item(
                        id=item.id,
                        status="failed",
                        input=item.source,
                        outputs=[],
                        bytes_in=bytes_in,
                        reason="engine_error",
                        warnings=[],
                    )
                if item_failed and stop_on_error:
                    break
        except KeyboardInterrupt:
            _clear_partials(batch_dir)
            state.finish("interrupted")
            reporter.error(code="interrupted", message="interrupted by user")
            reporter.result(**build_result(state, batch_dir, EXIT_INTERRUPTED))
            return EXIT_INTERRUPTED

        # Any stage declared beyond the generic scan/processing pair above (only
        # `split`'s "verify" today) — emitted once the per-item loop is done, in the
        # order `start` declared it, so `split` reports it without run_items needing to
        # know that stage's name.
        for extra_index, stage_name in enumerate(stages[2:], start=3):
            reporter.stage(stage=stage_name, index=extra_index, count=len(stages))

        state.finish("done" if exit_code == EXIT_OK else "failed")

    result = build_result(state, batch_dir, exit_code)
    reporter.result(**result)
    write_summary_json(summary_json, result)
    return exit_code


def _report_dry_run(planned: list[tuple[Item, str | None]], reporter: Reporter) -> int:
    """--dry-run: plan and report only. Nothing is written anywhere, not even run.json."""
    exit_code = EXIT_OK
    failed = []
    for item, problem in planned:
        status = "skipped" if problem is None else "failed"
        reporter.item(
            id=item.id,
            status=status,
            input=item.source,
            outputs=item.outputs,
            bytes_in=None,
            reason=problem,
        )
        if problem is not None:
            exit_code = EXIT_FAILED
            failed.append({"id": item.id, "input": str(item.source), "reason": problem})
    # Same five keys a real run's `counts` always has (an agent reading `counts.done`
    # must not get a KeyError just because this was a dry run), plus `planned` as an
    # extra: nothing actually ran, so `done`/`pending` are 0 and every plannable item is
    # counted under `skipped` — matching the per-item status this loop already reports.
    reporter.result(
        ok=exit_code == EXIT_OK,
        exit_code=exit_code,
        counts={
            "total": len(planned),
            "done": 0,
            "skipped": len(planned) - len(failed),
            "failed": len(failed),
            "pending": 0,
            "planned": len(planned),
        },
        failed=failed,
        pending=[],
        outputs=[],
        run_file=None,
    )
    return exit_code
