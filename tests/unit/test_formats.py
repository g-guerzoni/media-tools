"""formats: what each task and engine can read and write, and the table `--markdown`
renders for the README. Task 14."""

from __future__ import annotations

import json
import subprocess
import sys

from media_tools.tasks import formats as formats_task


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "media_tools", *args], capture_output=True, text=True
    )


def test_formats_json_lists_every_engine():
    result = _cli("formats", "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    rows = payload["formats"]
    tasks = {row["task"] for row in rows}
    assert {"compress", "convert", "split", "download"} <= tasks
    audio = next(r for r in rows if r["task"] == "convert" and r["engine"] == "audio")
    assert "mp3" in audio["outputs"]
    assert ".m4a" in audio["inputs"]


def test_formats_json_envelope_has_v_and_type():
    # RULING R27: every query command emits one JSON object with {"v": 1, "type": ...}.
    payload = json.loads(_cli("formats", "--json").stdout)
    assert payload["v"] == 1
    assert payload["type"] == "formats"


def test_formats_markdown_is_a_table():
    result = _cli("formats", "--markdown")
    assert result.stdout.startswith("| Task |")
    assert "compress" in result.stdout


def test_formats_json_output_is_valid_json_on_stdout_only():
    result = _cli("formats", "--json")
    assert result.returncode == 0
    json.loads(result.stdout)  # a single JSON value, not JSON Lines


def test_formats_plain_table_lists_every_task_on_stdout():
    result = _cli("formats")
    assert result.returncode == 0
    assert "compress" in result.stdout
    assert "download" in result.stdout
    # Results go to stdout, progress and logs go to stderr - and formats has no
    # progress to log, so stderr must be empty.
    assert result.stderr == ""


def test_collect_never_crashes_on_a_task_with_no_engines():
    # R5: `download` (and `ebook`) legitimately have no ENGINES; collect() must use
    # getattr, not a plain attribute access which would raise AttributeError.
    rows = formats_task.collect()
    download_rows = [r for r in rows if r["task"] == "download"]
    assert download_rows  # still shows up in the table, just without a fixed format
    assert download_rows[0]["engine"] is None
    assert download_rows[0]["inputs"] == []
    assert download_rows[0]["outputs"] == []


def test_as_markdown_header_and_separator():
    rows = formats_task.collect()
    table = formats_task.as_markdown(rows)
    lines = table.splitlines()
    assert lines[0] == "| Task | Engine | Input formats | Output formats | Requires |"
    assert lines[1].startswith("| --- |")


def test_video_engine_requires_ffmpeg():
    rows = formats_task.collect()
    video = next(r for r in rows if r["task"] == "compress" and r["engine"] == "video")
    assert "ffmpeg" in video["requires"]


def test_split_engine_output_is_parts():
    rows = formats_task.collect()
    media = next(r for r in rows if r["task"] == "split" and r["engine"] == "media")
    assert media["outputs"] == ["parts"]
