import json

import pytest

from media_tools.core.state import BatchInUse, BatchTaskMismatch, RunState


def test_run_state_records_items_and_counts(tmp_path):
    with RunState.open(
        tmp_path / "b", task="compress", options={"crf": 28}, inputs=["/in"]
    ) as state:
        first = state.add_item("/in/a.mp4")
        second = state.add_item("/in/b.mp4")
        state.update(first, status="done", outputs=[{"path": "a.mp4", "bytes": 5}])
        state.update(second, status="failed", reason="engine_error")
        state.finish("done")

    data = json.loads((tmp_path / "b" / "run.json").read_text())
    assert data["v"] == 1
    assert data["task"] == "compress"
    assert data["counts"] == {"total": 2, "done": 1, "skipped": 0, "failed": 1, "pending": 0}
    assert data["items"][0]["outputs"][0]["path"] == "a.mp4"
    assert data["status"] == "done"


def test_second_run_on_a_live_batch_is_refused(tmp_path):
    with RunState.open(tmp_path / "b", task="compress", options={}, inputs=[]):  # noqa: SIM117
        with pytest.raises(BatchInUse):
            RunState.open(tmp_path / "b", task="compress", options={}, inputs=[])


def test_stale_lock_is_reclaimed(tmp_path):
    batch = tmp_path / "b"
    batch.mkdir(parents=True)
    (batch / ".lock").write_text(json.dumps({"pid": 999999, "host": "gone"}))
    with RunState.open(batch, task="compress", options={}, inputs=[]) as state:
        state.finish("done")
    assert not (batch / ".lock").exists()


def test_batch_owned_by_another_task_is_refused(tmp_path):
    with RunState.open(tmp_path / "b", task="compress", options={}, inputs=[]) as state:
        state.finish("done")
    with pytest.raises(BatchTaskMismatch):
        RunState.open(tmp_path / "b", task="split", options={}, inputs=[])


def test_interrupted_runs_keep_pending_items(tmp_path):
    state = RunState.open(tmp_path / "b", task="compress", options={}, inputs=[])
    state.add_item("/in/a.mp4")
    state.finish("interrupted")
    data = json.loads((tmp_path / "b" / "run.json").read_text())
    assert data["status"] == "interrupted"
    assert data["counts"]["pending"] == 1
