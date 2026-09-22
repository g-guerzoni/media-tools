import json

import pytest

from media_tools.core.state import BatchInUse, BatchTaskMismatch, RunState, resolve_batch_name


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


def test_input_urls_are_redacted_in_run_json(tmp_path):
    with RunState.open(tmp_path / "b", task="compress", options={}, inputs=[]) as state:
        state.add_item("https://host/v/x.m3u8?sjwt=SECRET&uid=9")
        state.finish("done")
    data = json.loads((tmp_path / "b" / "run.json").read_text())
    assert "SECRET" not in json.dumps(data)
    assert "https://host/v/x.m3u8" in data["items"][0]["input"]


def test_finish_releases_lock_for_immediate_reopen(tmp_path):
    state = RunState.open(tmp_path / "b", task="compress", options={}, inputs=[])
    state.add_item("/in/a.mp4")
    state.finish("done")
    # Should not raise BatchInUse; lock was released by finish()
    state2 = RunState.open(tmp_path / "b", task="compress", options={}, inputs=[])
    state2.finish("done")


def test_update_with_invalid_item_id_raises(tmp_path):
    state = RunState.open(tmp_path / "b", task="compress", options={}, inputs=[])
    state.add_item("/in/a.mp4")
    with pytest.raises(ValueError, match="item_id must be >= 1"):
        state.update(0, status="done")
    # Verify no item was modified by checking data in memory
    assert state.data["items"][0]["status"] == "pending"
    state.finish("done")


# -- C2: a corrupt or non-object run.json must not crash the caller -------------------


def test_open_raises_batch_task_mismatch_on_corrupt_run_json(tmp_path):
    batch = tmp_path / "b"
    batch.mkdir()
    (batch / "run.json").write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(BatchTaskMismatch, match="unreadable"):
        RunState.open(batch, task="compress", options={}, inputs=[])


@pytest.mark.parametrize("payload", ["[]", '"hello"', "null", "42"])
def test_open_raises_batch_task_mismatch_on_non_object_run_json(tmp_path, payload):
    batch = tmp_path / "b"
    batch.mkdir()
    (batch / "run.json").write_text(payload, encoding="utf-8")
    with pytest.raises(BatchTaskMismatch, match="JSON object"):
        RunState.open(batch, task="compress", options={}, inputs=[])


# -- I4/R29: reusing a batch with different options -------------------------------


def test_open_refuses_different_options_unless_forced(tmp_path):
    batch = tmp_path / "b"
    with RunState.open(batch, task="compress", options={"crf": 28}, inputs=[]) as state:
        state.finish("done")

    with pytest.raises(BatchTaskMismatch, match="28.*32|32.*28"):
        RunState.open(batch, task="compress", options={"crf": 32}, inputs=[])

    # unforced attempt must not have touched the record on disk
    data = json.loads((batch / "run.json").read_text())
    assert data["engine_options"] == {"crf": 28}

    with RunState.open(batch, task="compress", options={"crf": 32}, inputs=[], force=True) as st:
        st.finish("done")
    data = json.loads((batch / "run.json").read_text())
    assert data["engine_options"] == {"crf": 32}


def test_open_same_options_reopens_without_force(tmp_path):
    batch = tmp_path / "b"
    with RunState.open(batch, task="compress", options={"crf": 28}, inputs=[]) as state:
        state.finish("done")
    with RunState.open(batch, task="compress", options={"crf": 28}, inputs=[]) as state:
        state.finish("done")  # no BatchTaskMismatch


# -- resolve_batch_name: the HASH-derived side of I4/R29, spec 7.1 --------------------


def test_resolve_batch_name_reuses_a_free_name(tmp_path):
    assert resolve_batch_name(tmp_path, "abc", task="compress", options={}) == "abc"


def test_resolve_batch_name_reuses_a_matching_batch(tmp_path):
    with RunState.open(tmp_path / "abc", task="compress", options={"crf": 28}, inputs=[]) as st:
        st.finish("done")
    assert resolve_batch_name(tmp_path, "abc", task="compress", options={"crf": 28}) == "abc"


def test_resolve_batch_name_appends_suffix_on_option_mismatch(tmp_path):
    with RunState.open(tmp_path / "abc", task="compress", options={"crf": 28}, inputs=[]) as st:
        st.finish("done")
    assert resolve_batch_name(tmp_path, "abc", task="compress", options={"crf": 32}) == "abc-2"


def test_resolve_batch_name_appends_suffix_on_task_mismatch(tmp_path):
    with RunState.open(tmp_path / "abc", task="compress", options={}, inputs=[]) as st:
        st.finish("done")
    assert resolve_batch_name(tmp_path, "abc", task="split", options={}) == "abc-2"


def test_resolve_batch_name_appends_suffix_on_corrupt_run_json(tmp_path):
    batch = tmp_path / "abc"
    batch.mkdir()
    (batch / "run.json").write_text("{ not json", encoding="utf-8")
    assert resolve_batch_name(tmp_path, "abc", task="compress", options={}) == "abc-2"


def test_resolve_batch_name_skips_multiple_taken_names(tmp_path):
    with RunState.open(tmp_path / "abc", task="compress", options={"crf": 28}, inputs=[]) as st:
        st.finish("done")
    with RunState.open(tmp_path / "abc-2", task="compress", options={"crf": 32}, inputs=[]) as st:
        st.finish("done")
    assert resolve_batch_name(tmp_path, "abc", task="compress", options={"crf": 40}) == "abc-3"
