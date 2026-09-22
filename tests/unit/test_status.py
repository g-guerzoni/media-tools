"""status: a batch's progress from run.json, without anyone parsing it by hand. Task 14."""

from __future__ import annotations

import json
import subprocess
import sys

from media_tools.tasks.status import read_batches


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "media_tools", *args], capture_output=True, text=True
    )


def _fake_batch(root, name, **overrides):
    batch = root / name
    batch.mkdir(parents=True)
    data = {
        "v": 1,
        "task": "compress",
        "batch": name,
        "output_dir": str(batch),
        "status": "failed",
        "updated_at": "2026-09-22T12:00:00Z",
        "owner": None,
        "engine_options": {"crf": 28},
        "counts": {"total": 3, "done": 1, "skipped": 1, "failed": 1, "pending": 0},
        "items": [
            {"id": 1, "input": "/in/a.mp4", "status": "done", "reason": None, "outputs": []},
            {
                "id": 2,
                "input": "/in/b.mp4",
                "status": "skipped",
                "reason": "exists",
                "outputs": [],
            },
            {
                "id": 3,
                "input": "/in/c.mp4",
                "status": "failed",
                "reason": "engine_error",
                "outputs": [],
            },
        ],
    }
    data.update(overrides)
    (batch / "run.json").write_text(json.dumps(data), encoding="utf-8")
    return batch


def test_status_of_one_batch_reports_failures(tmp_path):
    _fake_batch(tmp_path, "aula-01")
    result = _cli("status", "aula-01", "-o", str(tmp_path), "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["counts"]["failed"] == 1
    assert payload["failed"][0]["reason"] == "engine_error"


def test_status_reports_pending_items_not_just_totals(tmp_path):
    _fake_batch(
        tmp_path,
        "aula-02",
        status="running",
        owner={"pid": 999999999, "host": "x", "since": "now"},
        counts={"total": 4, "done": 1, "skipped": 1, "failed": 1, "pending": 1},
        items=[
            {"id": 1, "input": "/in/a.mp4", "status": "done", "reason": None, "outputs": []},
            {
                "id": 2,
                "input": "/in/b.mp4",
                "status": "skipped",
                "reason": "exists",
                "outputs": [],
            },
            {
                "id": 3,
                "input": "/in/c.mp4",
                "status": "failed",
                "reason": "engine_error",
                "outputs": [],
            },
            {"id": 4, "input": "/in/d.mp4", "status": "pending", "reason": None, "outputs": []},
        ],
    )
    result = _cli("status", "aula-02", "-o", str(tmp_path), "--json")
    payload = json.loads(result.stdout)
    assert payload["pending"] == [{"id": 4, "input": "/in/d.mp4"}]
    assert payload["active"] is True


def test_status_lists_all_batches(tmp_path):
    _fake_batch(tmp_path, "one")
    _fake_batch(tmp_path, "two")
    result = _cli("status", "-o", str(tmp_path), "--json")
    names = {row["batch"] for row in json.loads(result.stdout)}
    assert names == {"one", "two"}


def test_status_list_sorted_by_updated_at_descending(tmp_path):
    _fake_batch(tmp_path, "older", updated_at="2026-01-01T00:00:00Z")
    _fake_batch(tmp_path, "newer", updated_at="2026-06-01T00:00:00Z")
    result = _cli("status", "-o", str(tmp_path), "--json")
    rows = json.loads(result.stdout)
    assert [r["batch"] for r in rows] == ["newer", "older"]


def test_status_skips_reserved_entries_by_default(tmp_path):
    (tmp_path / ".cache").mkdir()
    (tmp_path / "_kindle").mkdir()
    _fake_batch(tmp_path, "real-batch")
    result = _cli("status", "-o", str(tmp_path), "--json")
    names = {row["batch"] for row in json.loads(result.stdout)}
    assert names == {"real-batch"}


def test_status_all_flag_surfaces_reserved_entries_as_unreadable(tmp_path):
    (tmp_path / ".cache").mkdir()
    _fake_batch(tmp_path, "real-batch")
    result = _cli("status", "-o", str(tmp_path), "--json", "--all")
    rows = json.loads(result.stdout)
    cache_row = next(r for r in rows if r["batch"] == ".cache")
    assert cache_row["readable"] is False


def test_status_reports_corrupt_run_json_as_unreadable_not_a_crash(tmp_path):
    batch = tmp_path / "broken"
    batch.mkdir()
    (batch / "run.json").write_text("{not json", encoding="utf-8")
    result = _cli("status", "-o", str(tmp_path), "--json")
    assert result.returncode == 0
    rows = json.loads(result.stdout)
    (row,) = rows
    assert row["batch"] == "broken"
    assert row["readable"] is False


def test_status_missing_run_json_is_unreadable_not_a_crash(tmp_path):
    (tmp_path / "empty-batch").mkdir()
    result = _cli("status", "-o", str(tmp_path), "--json")
    rows = json.loads(result.stdout)
    (row,) = rows
    assert row["readable"] is False


def test_unknown_batch_exits_2(tmp_path):
    assert _cli("status", "ghost", "-o", str(tmp_path)).returncode == 2


def test_status_human_output_goes_to_stdout(tmp_path):
    _fake_batch(tmp_path, "aula-01")
    result = _cli("status", "aula-01", "-o", str(tmp_path))
    assert result.returncode == 0
    assert "aula-01" in result.stdout
    assert result.stderr == ""


def test_status_empty_root_lists_nothing(tmp_path):
    result = _cli("status", "-o", str(tmp_path), "--json")
    assert json.loads(result.stdout) == []


def test_read_batches_returns_parsed_run_json(tmp_path):
    _fake_batch(tmp_path, "b1")
    rows = read_batches(tmp_path)
    assert len(rows) == 1
    assert rows[0]["batch"] == "b1"
    assert rows[0]["readable"] is True
