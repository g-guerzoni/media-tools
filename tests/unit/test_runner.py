import io
import json
from pathlib import Path

from media_tools.core.events import EXIT_FAILED, EXIT_INTERRUPTED, EXIT_OK, EXIT_USAGE, Reporter
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
