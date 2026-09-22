import json
import subprocess
import sys

import pytest

from media_tools.cli import TASKS, main


def _run(args, **kwargs):
    return subprocess.run(
        [sys.executable, "-m", "media_tools", *args], capture_output=True, text=True, **kwargs
    )


def test_no_arguments_shows_usage_and_exits_2():
    assert main([]) == 2


def test_no_arguments_prints_usage_to_stderr_only():
    result = _run([])
    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage" in result.stderr.lower()


def test_unknown_task_exits_2():
    result = _run(["nope"])
    assert result.returncode == 2


def test_missing_input_exits_2_with_json_error(tmp_path):
    result = _run(
        ["compress", str(tmp_path / "missing.mp4"), "--json", "-o", str(tmp_path / "out")]
    )
    assert result.returncode == 2
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(e["type"] == "error" and e["code"] == "usage" for e in events)


def test_empty_folder_exits_2_with_no_input_matched(tmp_path):
    (tmp_path / "in").mkdir()
    result = _run(["compress", str(tmp_path / "in"), "--json", "-o", str(tmp_path / "out")])
    assert result.returncode == 2
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(e.get("code") == "no_input_matched" for e in events)


def test_help_lists_every_task():
    result = _run(["--help"])
    for task in (
        "compress",
        "convert",
        "split",
        "download",
        "ebook",
        "formats",
        "doctor",
        "status",
    ):
        assert task in result.stdout


def test_file_task_with_no_inputs_exits_2(tmp_path):
    result = _run(["compress", "-o", str(tmp_path / "out")])
    assert result.returncode == 2


def test_no_input_matched_error_carries_a_hint(tmp_path):
    (tmp_path / "in").mkdir()
    result = _run(["compress", str(tmp_path / "in"), "--json", "-o", str(tmp_path / "out")])
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    (error,) = [e for e in events if e["type"] == "error"]
    assert error["hint"]


def test_convert_requires_to_and_exits_2(tmp_path):
    named = tmp_path / "clip.mp3"
    named.write_bytes(b"x")
    result = _run(["convert", str(named), "-o", str(tmp_path / "out")])
    assert result.returncode == 2


def test_convert_with_to_but_no_engine_reports_usage(tmp_path):
    named = tmp_path / "clip.mp3"
    named.write_bytes(b"x")
    result = _run(["convert", str(named), "--to", "mp3", "--json", "-o", str(tmp_path / "out")])
    assert result.returncode == 2
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(e["type"] == "error" and e["code"] == "usage" for e in events)


@pytest.mark.parametrize("task", ["ebook", "formats", "doctor", "status"])
def test_minimal_stub_tasks_report_not_implemented(task):
    result = _run([task, "--json"])
    assert result.returncode == 3
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(e["type"] == "error" and e["code"] == "dependency_missing" for e in events)


def test_json_mode_emits_only_jsonlines_on_stdout(tmp_path):
    result = _run(["compress", str(tmp_path / "missing.mp4"), "--json", "-o", str(tmp_path)])
    for line in result.stdout.splitlines():
        if line.strip():
            json.loads(line)  # raises if any stdout line is not valid JSON


def test_download_help_shows_url_not_input():
    result = _run(["download", "--help"])
    assert "URL" in result.stdout
    assert "INPUT" not in result.stdout
    assert "URLs to process" in result.stdout


def test_keyboard_interrupt_in_a_task_exits_130(monkeypatch):
    def boom(args):
        raise KeyboardInterrupt

    compress_task = next(t for t in TASKS if t.NAME == "compress")
    monkeypatch.setattr(compress_task, "run", boom)
    assert main(["compress", "x"]) == 130
