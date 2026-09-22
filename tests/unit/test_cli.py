import json
import subprocess
import sys

from media_tools.cli import TASKS, main
from media_tools.core.events import EXIT_FAILED


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


def test_convert_with_garbage_input_fails_the_item_not_the_command(tmp_path):
    # Since Task 11, the audio engine handles .mp3 -> mp3, so this is no longer a usage
    # error (no engine) but a per-item failure: ffmpeg cannot decode the garbage bytes.
    named = tmp_path / "clip.mp3"
    named.write_bytes(b"x")
    result = _run(["convert", str(named), "--to", "mp3", "--json", "-o", str(tmp_path / "out")])
    assert result.returncode == 1
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    (item,) = [e for e in events if e["type"] == "item"]
    assert item["status"] == "failed"
    assert item["reason"] == "engine_error"


def test_ebook_stub_task_reports_not_implemented():
    # ebook is the only task still a Task 8 stub; formats/doctor/status were replaced
    # by Task 14/15 and are covered by their own test modules. Its exit code is 3
    # (missing dependency/configuration), but the error code is "config_missing", not
    # "dependency_missing" — nothing is missing from the machine to go install.
    result = _run(["ebook", "--json"])
    assert result.returncode == 3
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(e["type"] == "error" and e["code"] == "config_missing" for e in events)


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


def test_download_help_does_not_advertise_unused_scan_flags():
    """download never reads args.recursive/args.extensions (its inputs are URLs, not a
    folder scan), so -r/--recursive and -e/--extensions must not appear in its --help —
    they used to be registered (inherited from add_common_flags) but silently do
    nothing."""
    result = _run(["download", "--help"])
    assert "--recursive" not in result.stdout
    assert "--extensions" not in result.stdout


def test_keyboard_interrupt_in_a_task_exits_130(monkeypatch):
    def boom(args):
        raise KeyboardInterrupt

    compress_task = next(t for t in TASKS if t.NAME == "compress")
    monkeypatch.setattr(compress_task, "run", boom)
    assert main(["compress", "x"]) == 130


# -- C3: an unanticipated exception must not escape as a bare traceback --------------


def test_unexpected_exception_in_a_task_is_reported_as_internal_error(monkeypatch):
    def boom(args):
        raise RuntimeError("kaboom")

    compress_task = next(t for t in TASKS if t.NAME == "compress")
    monkeypatch.setattr(compress_task, "run", boom)
    assert main(["compress", "x"]) == EXIT_FAILED


def test_unexpected_exception_emits_an_internal_error_json_event(monkeypatch, capsys):
    def boom(args):
        raise RuntimeError("kaboom")

    compress_task = next(t for t in TASKS if t.NAME == "compress")
    monkeypatch.setattr(compress_task, "run", boom)
    code = main(["compress", "x", "--json"])
    assert code == EXIT_FAILED
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    (error,) = [e for e in events if e["type"] == "error"]
    assert error["code"] == "internal_error"
    assert "RuntimeError" in error["message"]
    assert "kaboom" in error["message"]
    assert error["hint"]


# -- C2: a corrupt or non-object run.json must fail cleanly, not crash ---------------


def test_corrupt_run_json_is_a_clean_batch_task_mismatch_not_a_crash(tmp_path):
    named = tmp_path / "clip.mp4"
    named.write_bytes(b"x")
    out_root = tmp_path / "out"
    batch_dir = out_root / "mybatch"
    batch_dir.mkdir(parents=True)
    (batch_dir / "run.json").write_text("{ this is not json", encoding="utf-8")

    result = _run(["compress", str(named), "-b", "mybatch", "--json", "-o", str(out_root)])
    assert result.returncode == 2
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(e["type"] == "error" and e["code"] == "batch_task_mismatch" for e in events)
    assert events[-1]["type"] == "result"


def test_non_object_run_json_is_a_clean_batch_task_mismatch_not_a_crash(tmp_path):
    named = tmp_path / "clip.mp4"
    named.write_bytes(b"x")
    out_root = tmp_path / "out"
    batch_dir = out_root / "mybatch"
    batch_dir.mkdir(parents=True)
    (batch_dir / "run.json").write_text("[]", encoding="utf-8")

    result = _run(["compress", str(named), "-b", "mybatch", "--json", "-o", str(out_root)])
    assert result.returncode == 2
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(e["type"] == "error" and e["code"] == "batch_task_mismatch" for e in events)


def test_default_batch_name_collision_appends_suffix_instead_of_mismatch(tmp_path):
    """I4/R29, spec 7.1's other half: a HASH-derived (no --batch) name whose run.json
    belongs to a different task/options is not this run's batch to refuse or silently
    reuse — -2, -3, ... is appended instead, unlike an EXPLICIT --batch name (see
    test_corrupt_run_json_is_a_clean_batch_task_mismatch_not_a_crash and friends,
    above, and test_open_refuses_different_options_unless_forced in test_state.py)."""
    from media_tools.core.paths import batch_hash

    named = tmp_path / "clip.mp4"
    named.write_bytes(b"x")
    out_root = tmp_path / "out"

    options = {"codec": "h264", "crf": 28, "audio_bitrate": "96k", "mono": False}
    selection = {"recursive": False, "extensions": []}
    expected_name = batch_hash(
        task="compress", options=options, selection=selection, inputs=[named]
    )

    # Plant a batch under that exact name belonging to a different task, so this
    # command's default batch name collides the moment it is resolved.
    colliding = out_root / expected_name
    colliding.mkdir(parents=True)
    (colliding / "run.json").write_text(
        json.dumps({"task": "split", "engine_options": {}}), encoding="utf-8"
    )

    result = _run(["compress", str(named), "--json", "-o", str(out_root)])
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    (start,) = [e for e in events if e["type"] == "start"]
    assert start["batch"] == f"{expected_name}-2"
    assert not any(e["type"] == "error" and e["code"] == "batch_task_mismatch" for e in events)


def test_named_file_outside_extensions_flag_warns_but_still_runs(tmp_path):
    """I3/R30: wired end to end through tasks.common.prepare()."""
    named = tmp_path / "clip.mkv"
    named.write_bytes(b"x")
    result = _run(["compress", str(named), "-e", "mp4", "--json", "-o", str(tmp_path / "out")])
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    warnings = [e for e in events if e["type"] == "warning"]
    assert any(w["code"] == "extension_filter_bypassed" for w in warnings)
    # it was still processed as an item (not rejected as a usage error)
    assert any(e["type"] == "item" for e in events)


def test_download_corrupt_run_json_is_a_clean_batch_task_mismatch_not_a_crash(tmp_path):
    out_root = tmp_path / "out"
    batch_dir = out_root / "mybatch"
    batch_dir.mkdir(parents=True)
    (batch_dir / "run.json").write_text("{ this is not json", encoding="utf-8")

    result = _run(
        ["download", "http://example.invalid/v", "-b", "mybatch", "--json", "-o", str(out_root)]
    )
    assert result.returncode == 2
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(e["type"] == "error" and e["code"] == "batch_task_mismatch" for e in events)


def test_named_file_with_unsupported_extension_exits_2_with_usage(tmp_path):
    # R21's actual path: a real file, an extension no engine accepts, and to=None
    # (unlike --to, which short-circuits earlier; and unlike a missing path, which hits
    # the not-found branch instead of expand_inputs's suffix rejection).
    named = tmp_path / "notes.txt"
    named.write_text("hi")
    result = _run(["compress", str(named), "--json", "-o", str(tmp_path / "out")])
    assert result.returncode == 2
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(e["type"] == "error" and e["code"] == "usage" for e in events)


# -- R22: argparse's own errors (bypassing prepare/UsageError entirely) must still reach
# stdout as a JSON `error` event when --json is present, so an agent never sees an empty
# stdout on a usage error argparse rejects on its own (unknown subcommand, a missing
# required flag, a bad flag type, ...). --------------------------------------------------


def test_convert_missing_to_reports_usage_as_json_on_stdout(tmp_path):
    named = tmp_path / "clip.mp3"
    named.write_bytes(b"x")
    result = _run(["convert", str(named), "--json", "-o", str(tmp_path / "out")])
    assert result.returncode == 2
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "error"
    assert event["code"] == "usage"
    assert "--to" in result.stderr


def test_unknown_subcommand_reports_usage_as_json_on_stdout():
    result = _run(["nope", "--json"])
    assert result.returncode == 2
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "error"
    assert event["code"] == "usage"
    assert "invalid choice" in result.stderr


def test_bad_limit_type_reports_usage_as_json_on_stdout(tmp_path):
    named = tmp_path / "clip.mp3"
    named.write_bytes(b"x")
    result = _run(["compress", str(named), "--limit", "abc", "--json", "-o", str(tmp_path / "out")])
    assert result.returncode == 2
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "error"
    assert event["code"] == "usage"
    assert "--limit" in result.stderr


def test_negative_limit_is_a_usage_error(tmp_path):
    """A negative --limit used to silently drop the last N items (Python slicing:
    sources[:-1]) instead of limiting anything."""
    named = tmp_path / "clip.mp3"
    named.write_bytes(b"x")
    result = _run(["compress", str(named), "--limit", "-1", "--json", "-o", str(tmp_path / "out")])
    assert result.returncode == 2
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(e["type"] == "error" and e["code"] == "usage" for e in events)


def test_zero_limit_is_a_usage_error(tmp_path):
    named = tmp_path / "clip.mp3"
    named.write_bytes(b"x")
    result = _run(["compress", str(named), "--limit", "0", "--json", "-o", str(tmp_path / "out")])
    assert result.returncode == 2
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(e["type"] == "error" and e["code"] == "usage" for e in events)


def test_download_negative_limit_is_a_usage_error(tmp_path):
    result = _run(
        ["download", "http://example.invalid/v", "--limit", "-1", "--json", "-o", str(tmp_path)]
    )
    assert result.returncode == 2
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(e["type"] == "error" and e["code"] == "usage" for e in events)


def test_argparse_usage_errors_leave_stdout_empty_without_json_flag(tmp_path):
    named = tmp_path / "clip.mp3"
    named.write_bytes(b"x")
    commands = [
        ["convert", str(named), "-o", str(tmp_path / "out")],
        ["nope"],
        ["compress", str(named), "--limit", "abc", "-o", str(tmp_path / "out")],
    ]
    for argv in commands:
        result = _run(argv)
        assert result.returncode == 2
        assert result.stdout == ""
        assert result.stderr  # argparse's usual text is still there
