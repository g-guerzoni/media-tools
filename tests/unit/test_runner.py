import io
import json
import os
from pathlib import Path

import pytest

from media_tools.core.events import (
    EXIT_DEPENDENCY,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    Reporter,
)
from media_tools.core.inputs import Source
from media_tools.core.paths import temp_path
from media_tools.core.runner import Context, Item, Outcome, run_items
from media_tools.core.state import RunState


class FakeEngine:
    """Writes '<stem>.out' containing the source bytes; fails on files named 'bad'."""

    name = "fake"
    inputs = frozenset({".src"})
    outputs = frozenset({"out"})
    dependencies = ()

    def add_arguments(self, group):  # pragma: no cover - nothing to add
        pass

    def hash_options(self, args):
        return {}

    def output_names(self, src: Path, args) -> list[str]:
        return [f"{src.stem}.out"]

    def process(self, item: Item, ctx: Context) -> Outcome:
        if item.source.stem == "bad":
            return Outcome(status="failed", outputs=[], bytes_out=None, reason="engine_error")
        target = item.outputs[0]
        target.write_bytes(item.source.read_bytes())
        return Outcome(status="done", outputs=[target], bytes_out=target.stat().st_size)


class MessyEngine(FakeEngine):
    """Writes real leftovers at both the temp name and the final path, then fails anyway.

    Unlike FakeEngine's 'bad' branch (which fails without writing anything), this proves
    the runner's failure cleanup actually removes what was on disk, not just that the
    assertion happens to hold when nothing was ever written."""

    def process(self, item: Item, ctx: Context) -> Outcome:
        temp = temp_path(item.outputs[0])
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(b"temp leftovers")
        item.outputs[0].write_bytes(b"partial final output")
        return Outcome(status="failed", outputs=[], bytes_out=None, reason="engine_error")


def _sources(*paths: Path) -> list[Source]:
    return [Source(path=p, root=None) for p in paths]


def _run(sources, batch, engines=None, **kwargs):
    reporter = Reporter(json_mode=False, quiet=True, stdout=io.StringIO(), stderr=io.StringIO())
    return run_items(
        sources,
        task="fake",
        engines=engines or [FakeEngine()],
        args=object(),
        reporter=reporter,
        batch_dir=batch,
        stages=["process"],
        options={},
        **kwargs,
    )


def test_item_without_a_matching_engine_fails_as_unsupported(tmp_path):
    other = tmp_path / "a.other"
    other.write_bytes(b"x")
    batch = tmp_path / "b"
    assert _run(_sources(other), batch) == EXIT_FAILED
    data = json.loads((batch / "run.json").read_text())
    assert data["items"][0]["reason"] == "unsupported_input"


def test_summary_json_receives_the_result(tmp_path):
    src = tmp_path / "a.src"
    src.write_bytes(b"x")
    summary = tmp_path / "summary.json"
    _run(_sources(src), tmp_path / "b", summary_json=summary)
    payload = json.loads(summary.read_text())
    assert payload["type"] == "result"
    assert payload["ok"] is True


def test_runner_processes_and_skips_existing(tmp_path):
    src = tmp_path / "a.src"
    src.write_bytes(b"hello")
    batch = tmp_path / "media" / "b"

    assert _run(_sources(src), batch) == EXIT_OK
    assert (batch / "a.out").read_bytes() == b"hello"

    # second run: nothing to do
    assert _run(_sources(src), batch) == EXIT_OK
    data = json.loads((batch / "run.json").read_text())
    assert data["counts"]["skipped"] == 1


def test_runner_reports_failures_with_exit_1(tmp_path):
    bad = tmp_path / "bad.src"
    bad.write_bytes(b"x")
    assert _run(_sources(bad), tmp_path / "b") == EXIT_FAILED


def test_runner_detects_output_collisions(tmp_path):
    first = tmp_path / "one" / "a.src"
    second = tmp_path / "two" / "a.src"
    for path in (first, second):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x")

    assert _run(_sources(first, second), tmp_path / "b") == EXIT_FAILED
    data = json.loads((tmp_path / "b" / "run.json").read_text())
    reasons = [item["reason"] for item in data["items"]]
    assert "output_collision" in reasons


def test_runner_refuses_output_equal_to_input(tmp_path):
    src = tmp_path / "b" / "a.src"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")

    class SameNameEngine(FakeEngine):
        def output_names(self, src, args):
            return [src.name]

    reporter = Reporter(json_mode=False, quiet=True, stdout=io.StringIO(), stderr=io.StringIO())
    code = run_items(
        _sources(src),
        task="fake",
        engines=[SameNameEngine()],
        args=object(),
        reporter=reporter,
        batch_dir=tmp_path / "b",
        stages=["process"],
        options={},
    )
    assert code == EXIT_FAILED
    assert src.read_bytes() == b"x"


def test_dry_run_writes_nothing(tmp_path):
    src = tmp_path / "a.src"
    src.write_bytes(b"x")
    batch = tmp_path / "b"
    assert _run(_sources(src), batch, dry_run=True) == EXIT_OK
    assert not batch.exists()


def test_temp_files_are_removed_on_failure(tmp_path):
    bad = tmp_path / "bad.src"
    bad.write_bytes(b"x")
    batch = tmp_path / "b"
    _run(_sources(bad), batch)
    assert list(batch.glob(".*.partial")) == []


def test_failure_cleanup_removes_both_temp_and_partial_final_output(tmp_path):
    """FakeEngine's 'bad' branch never writes anything, so test_temp_files_are_removed_on_failure
    above would pass even if the cleanup block were deleted. MessyEngine writes real leftovers
    at both the temp name and the final path before failing, so this one actually exercises it."""
    src = tmp_path / "a.src"
    src.write_bytes(b"x")
    batch = tmp_path / "b"

    code = _run(_sources(src), batch, engines=[MessyEngine()])

    assert code == EXIT_FAILED
    assert list(batch.rglob(".*.partial")) == []
    assert not (batch / "a.out").exists()
    data = json.loads((batch / "run.json").read_text())
    assert data["items"][0]["status"] == "failed"


def test_dry_run_reports_failure_for_unsupported_input(tmp_path):
    """A problem found during planning must fail the exit code even in --dry-run,
    otherwise a caller that only checks the exit code is told a broken plan is fine."""
    other = tmp_path / "a.other"
    other.write_bytes(b"x")
    batch = tmp_path / "b"
    assert _run(_sources(other), batch, dry_run=True) == EXIT_FAILED
    assert not batch.exists()


def test_context_reports_total_items_for_the_whole_batch(tmp_path):
    """Engines report progress as (item.id, ctx.total_items); this must be the size of
    the whole planned batch, not something derived from a single item's id."""
    a = tmp_path / "a.src"
    b = tmp_path / "b.src"
    a.write_bytes(b"1")
    b.write_bytes(b"2")
    seen: list[int] = []

    class RecordingEngine(FakeEngine):
        def process(self, item, ctx):
            seen.append(ctx.total_items)
            return super().process(item, ctx)

    assert _run(_sources(a, b), tmp_path / "batch", engines=[RecordingEngine()]) == EXIT_OK
    assert seen == [2, 2]


def test_stop_on_error_leaves_remaining_items_pending(tmp_path):
    bad = tmp_path / "bad.src"
    good = tmp_path / "z.src"
    bad.write_bytes(b"x")
    good.write_bytes(b"y")
    batch = tmp_path / "b"

    code = _run(_sources(bad, good), batch, stop_on_error=True)
    assert code == EXIT_FAILED

    data = json.loads((batch / "run.json").read_text())
    statuses = {item["input"]: item["status"] for item in data["items"]}
    assert statuses[str(bad)] == "failed"
    assert statuses[str(good)] == "pending"
    assert not (batch / "z.out").exists()


def test_batch_in_use_exits_with_usage_code(tmp_path):
    src = tmp_path / "a.src"
    src.write_bytes(b"x")
    batch = tmp_path / "b"
    with RunState.open(batch, task="fake", options={}, inputs=[]):
        assert _run(_sources(src), batch) == EXIT_USAGE


def test_batch_task_mismatch_exits_with_usage_code(tmp_path):
    src = tmp_path / "a.src"
    src.write_bytes(b"x")
    batch = tmp_path / "b"
    with RunState.open(batch, task="other-task", options={}, inputs=[]) as state:
        state.finish("done")
    assert _run(_sources(src), batch) == EXIT_USAGE


def test_interrupt_removes_partial_files_and_exits_130(tmp_path):
    class InterruptingEngine(FakeEngine):
        def process(self, item, ctx):
            temp = temp_path(item.outputs[0])
            temp.parent.mkdir(parents=True, exist_ok=True)
            temp.write_bytes(b"partial")
            raise KeyboardInterrupt

    src = tmp_path / "a.src"
    src.write_bytes(b"x")
    batch = tmp_path / "b"
    reporter = Reporter(json_mode=False, quiet=True, stdout=io.StringIO(), stderr=io.StringIO())
    code = run_items(
        _sources(src),
        task="fake",
        engines=[InterruptingEngine()],
        args=object(),
        reporter=reporter,
        batch_dir=batch,
        stages=["process"],
        options={},
    )
    assert code == EXIT_INTERRUPTED
    assert list(batch.glob(".*.partial")) == []


def _json_lines(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def test_interrupted_run_emits_a_final_result_event(tmp_path):
    """Spec 7.3: `result` is always the last line, including on interruption."""

    class InterruptOnSecondEngine(FakeEngine):
        def __init__(self):
            self.calls = 0

        def process(self, item, ctx):
            self.calls += 1
            if self.calls == 2:
                temp = temp_path(item.outputs[0])
                temp.parent.mkdir(parents=True, exist_ok=True)
                temp.write_bytes(b"partial")
                raise KeyboardInterrupt
            return super().process(item, ctx)

    first = tmp_path / "a.src"
    second = tmp_path / "b.src"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    batch = tmp_path / "b"
    out = io.StringIO()
    reporter = Reporter(json_mode=True, quiet=True, stdout=out, stderr=io.StringIO())

    code = run_items(
        _sources(first, second),
        task="fake",
        engines=[InterruptOnSecondEngine()],
        args=object(),
        reporter=reporter,
        batch_dir=batch,
        stages=["process"],
        options={},
    )

    assert code == EXIT_INTERRUPTED
    lines = _json_lines(out)
    assert lines[-1]["type"] == "result"
    assert lines[-1]["exit_code"] == EXIT_INTERRUPTED
    assert lines[-1]["ok"] is False
    # the first item's work is not lost, and nothing partial is left behind
    assert (batch / "a.out").read_bytes() == b"1"
    assert list(batch.glob(".*.partial")) == []


def test_batch_conflict_emits_a_final_result_event_with_null_run_file(tmp_path):
    """Spec 7.3: `result` is always the last line, including on failure. A run that never
    got to own the batch (another run holds it) has no run.json of its own to point at."""
    src = tmp_path / "a.src"
    src.write_bytes(b"x")
    batch = tmp_path / "b"
    out = io.StringIO()
    reporter = Reporter(json_mode=True, quiet=True, stdout=out, stderr=io.StringIO())

    with RunState.open(batch, task="fake", options={}, inputs=[]):
        code = run_items(
            _sources(src),
            task="fake",
            engines=[FakeEngine()],
            args=object(),
            reporter=reporter,
            batch_dir=batch,
            stages=["process"],
            options={},
        )

    assert code == EXIT_USAGE
    lines = _json_lines(out)
    assert lines[-1]["type"] == "result"
    assert lines[-1]["exit_code"] == EXIT_USAGE
    assert lines[-1]["ok"] is False
    assert lines[-1]["run_file"] is None
    assert lines[-1]["counts"]["total"] == 0


class CrashOnFirstEngine(FakeEngine):
    """Raises on the first item it sees, then behaves normally for the rest."""

    def __init__(self):
        self.calls = 0

    def process(self, item, ctx):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("boom")
        return super().process(item, ctx)


def test_engine_exception_fails_only_that_item_and_the_batch_continues(tmp_path):
    """R20: a raised exception (a vanished source, a library crash, ...) must not kill the
    rest of the batch, must be recorded as engine_error with the exception preserved in the
    item's data (not its reason), and the run must still end with a final result event."""
    first = tmp_path / "a.src"
    second = tmp_path / "b.src"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    batch = tmp_path / "b"
    out = io.StringIO()
    reporter = Reporter(json_mode=True, quiet=True, stdout=out, stderr=io.StringIO())

    code = run_items(
        _sources(first, second),
        task="fake",
        engines=[CrashOnFirstEngine()],
        args=object(),
        reporter=reporter,
        batch_dir=batch,
        stages=["process"],
        options={},
    )

    assert code == EXIT_FAILED
    data = json.loads((batch / "run.json").read_text())
    items = {Path(item["input"]).name: item for item in data["items"]}
    assert items["a.src"]["status"] == "failed"
    assert items["a.src"]["reason"] == "engine_error"
    assert "boom" in items["a.src"]["data"]["error_message"]
    # the second item still ran and produced its output
    assert items["b.src"]["status"] == "done"
    assert (batch / "b.out").read_bytes() == b"2"

    lines = _json_lines(out)
    assert lines[-1]["type"] == "result"
    assert lines[-1]["exit_code"] == EXIT_FAILED


def test_unwritable_output_root_exits_with_dependency_code(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")

    root = tmp_path / "readonly"
    root.mkdir()
    os.chmod(root, 0o500)
    batch = root / "b"
    src = tmp_path / "a.src"
    src.write_bytes(b"x")
    out = io.StringIO()
    reporter = Reporter(json_mode=True, quiet=True, stdout=out, stderr=io.StringIO())

    try:
        code = run_items(
            _sources(src),
            task="fake",
            engines=[FakeEngine()],
            args=object(),
            reporter=reporter,
            batch_dir=batch,
            stages=["process"],
            options={},
        )
    finally:
        os.chmod(root, 0o700)

    assert code == EXIT_DEPENDENCY
    events = _json_lines(out)
    assert any(e["type"] == "error" and e["code"] == "output_not_writable" for e in events)
    assert events[-1]["type"] == "result"
    assert events[-1]["exit_code"] == EXIT_DEPENDENCY
    assert events[-1]["run_file"] is None
