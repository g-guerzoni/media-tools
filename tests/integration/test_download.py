import http.server
import json
import subprocess
import sys
import threading

import pytest


@pytest.fixture
def local_server(make_video, tmp_path):
    """Serve a real mp4 over HTTP so yt-dlp's generic extractor can fetch it."""
    make_video(seconds=1, name="serve/video.mp4")
    root = tmp_path / "serve"

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/video.mp4"
    server.shutdown()


def _cli(*args, cwd=None):
    return subprocess.run(
        [sys.executable, "-m", "media_tools", *args], capture_output=True, text=True, cwd=cwd
    )


def test_single_url_downloads(local_server, tmp_path):
    out = tmp_path / "media"
    result = _cli("download", local_server, "-o", str(out), "-b", "d", "--name", "clip")
    assert result.returncode == 0
    assert (out / "d" / "clip.mp4").is_file()


def test_list_file_names_the_batch(local_server, tmp_path):
    listing = tmp_path / "lesson-pack.json"
    listing.write_text(json.dumps([{"url": local_server, "name": "one"}]), encoding="utf-8")
    out = tmp_path / "media"
    assert _cli("download", "--list", str(listing), "-o", str(out)).returncode == 0
    assert (out / "lesson-pack" / "one.mp4").is_file()


def test_failure_exits_1_and_hides_tokens(tmp_path):
    out = tmp_path / "media"
    result = _cli(
        "download",
        "http://127.0.0.1:9/none.mp4?token=SECRET",
        "-o",
        str(out),
        "-b",
        "f",
        "--json",
    )
    assert result.returncode == 1
    assert "SECRET" not in result.stdout + result.stderr

    run_file = out / "f" / "run.json"
    assert run_file.is_file()
    assert "SECRET" not in run_file.read_text(encoding="utf-8")


def test_urls_and_list_together_exit_2(tmp_path):
    listing = tmp_path / "l.json"
    listing.write_text('["http://x/y.mp4"]', encoding="utf-8")
    assert (
        _cli("download", "http://x/y.mp4", "--list", str(listing), "-o", str(tmp_path)).returncode
        == 2
    )


def test_list_formats_prints_to_stdout_and_downloads_nothing(local_server, tmp_path):
    out = tmp_path / "media"
    result = _cli("download", local_server, "--list-formats", "-o", str(out), "-b", "lf")
    assert result.returncode == 0
    assert result.stdout.strip()
    assert not (out / "lf").exists()


def test_audio_type_without_audio_only_format_warns(local_server, tmp_path):
    out = tmp_path / "media"
    result = _cli(
        "download",
        local_server,
        "--type",
        "audio",
        "-o",
        str(out),
        "-b",
        "a",
        "--name",
        "only",
        "--json",
    )
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert result.returncode == 0
    assert any(
        e.get("code") == "no_audio_only_format" for e in events if e["type"] in {"warning", "item"}
    ) or any(
        "no_audio_only_format" in (e.get("warnings") or []) for e in events if e["type"] == "item"
    )


def test_malformed_list_file_exits_2_naming_accepted_shapes(tmp_path):
    listing = tmp_path / "bad.json"
    listing.write_text(json.dumps({"videos": ["http://x/y.mp4"]}), encoding="utf-8")
    result = _cli("download", "--list", str(listing), "-o", str(tmp_path / "media"))
    assert result.returncode == 2
    assert "urls" in (result.stdout + result.stderr)


def test_empty_list_file_exits_2(tmp_path):
    listing = tmp_path / "empty.json"
    listing.write_text("[]", encoding="utf-8")
    result = _cli("download", "--list", str(listing), "-o", str(tmp_path / "media"))
    assert result.returncode == 2


def test_batch_continues_after_one_failure(local_server, tmp_path):
    listing = tmp_path / "mixed.json"
    listing.write_text(
        json.dumps(
            [
                {"url": "http://127.0.0.1:9/none.mp4", "name": "broken"},
                {"url": local_server, "name": "good"},
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "media"
    result = _cli("download", "--list", str(listing), "-o", str(out), "--json")
    assert result.returncode == 1
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    items = {e["input"]: e["status"] for e in events if e["type"] == "item"}
    assert len(items) == 2
    assert any(status == "failed" for status in items.values())
    assert any(status == "done" for status in items.values())
    result_event = next(e for e in events if e["type"] == "result")
    assert result_event["ok"] is False
    assert (out / "mixed" / "good.mp4").is_file()


def test_default_batch_name_is_a_stable_hash_of_the_urls(local_server, tmp_path):
    # No -b/--batch and no --list: the batch name must come from `batch_hash`, not
    # crash trying to treat a URL as a filesystem path, and be stable across runs.
    out = tmp_path / "media"
    first = _cli("download", local_server, "-o", str(out))
    assert first.returncode == 0
    batches = [p.name for p in out.iterdir() if p.is_dir()]
    assert len(batches) == 1
    (batch,) = batches
    assert len(batch) == 8

    second = _cli("download", local_server, "-o", str(out))
    assert second.returncode == 0
    assert [p.name for p in out.iterdir() if p.is_dir()] == [batch]


def test_dry_run_writes_nothing(local_server, tmp_path):
    out = tmp_path / "media"
    result = _cli("download", local_server, "-o", str(out), "-b", "dr", "--dry-run")
    assert result.returncode == 0
    assert not (out / "dr").exists()


# -- Fix round 1 -------------------------------------------------------------------


def test_rerun_skips_an_explicit_named_output_without_touching_the_network(local_server, tmp_path):
    # FIX 1: spec 7.1's "re-running the same command reuses the batch and only does
    # what is missing". An explicit name is resolved without extraction, so the second
    # run must skip before ever reaching the network — proven here by the output's
    # mtime never changing (a real re-download would rewrite the file).
    out = tmp_path / "media"
    first = _cli("download", local_server, "-o", str(out), "-b", "rs", "--name", "clip")
    assert first.returncode == 0
    produced = out / "rs" / "clip.mp4"
    assert produced.is_file()
    mtime_before = produced.stat().st_mtime_ns

    second = _cli("download", local_server, "-o", str(out), "-b", "rs", "--name", "clip", "--json")
    assert second.returncode == 0
    events = [json.loads(line) for line in second.stdout.splitlines() if line.strip()]
    item = next(e for e in events if e["type"] == "item")
    assert item["status"] == "skipped"
    assert item["reason"] == "exists"
    assert produced.stat().st_mtime_ns == mtime_before


def test_rerun_skips_a_title_derived_output(local_server, tmp_path):
    # FIX 1's other case: without an explicit name, the check can only happen inside
    # `download_one` after extraction — but the real (phase-2) download must still be
    # skipped, proven the same way: the file's mtime never changes.
    out = tmp_path / "media"
    first = _cli("download", local_server, "-o", str(out), "-b", "rst")
    assert first.returncode == 0
    batch_dir = out / "rst"
    produced = next(batch_dir.glob("*.mp4"))
    mtime_before = produced.stat().st_mtime_ns

    second = _cli("download", local_server, "-o", str(out), "-b", "rst", "--json")
    assert second.returncode == 0
    events = [json.loads(line) for line in second.stdout.splitlines() if line.strip()]
    item = next(e for e in events if e["type"] == "item")
    assert item["status"] == "skipped"
    assert item["reason"] == "exists"
    assert produced.stat().st_mtime_ns == mtime_before


def test_force_redownloads_despite_an_existing_output(local_server, tmp_path):
    # FIX 1: --force must still override skip/resume.
    out = tmp_path / "media"
    first = _cli("download", local_server, "-o", str(out), "-b", "fr", "--name", "clip")
    assert first.returncode == 0

    second = _cli(
        "download", local_server, "-o", str(out), "-b", "fr", "--name", "clip", "--force", "--json"
    )
    assert second.returncode == 0
    events = [json.loads(line) for line in second.stdout.splitlines() if line.strip()]
    item = next(e for e in events if e["type"] == "item")
    assert item["status"] == "done"


def test_list_formats_error_uses_extraction_failed_code(tmp_path):
    # FIX 3: a bad URL must report through the closed ERROR_CODES registry, not the
    # REASONS-only "engine_error" the hand-rolled JSON used to emit.
    result = _cli(
        "download",
        "http://127.0.0.1:9/none.mp4",
        "--list-formats",
        "-o",
        str(tmp_path / "media"),
        "--json",
    )
    assert result.returncode == 1
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    error_events = [e for e in events if e["type"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["code"] == "extraction_failed"
    assert error_events[0]["retryable"] is True


def test_list_formats_error_is_on_stderr_for_humans_not_stdout(tmp_path):
    # FIX 3: going through `Reporter.error()` means the human-mode message is on
    # stderr, like every other error in the codebase — not printed to stdout, where
    # only the (successful) formats table belongs.
    result = _cli(
        "download", "http://127.0.0.1:9/none.mp4", "--list-formats", "-o", str(tmp_path / "media")
    )
    assert result.returncode == 1
    assert "error" not in result.stdout.lower()
    assert "error" in result.stderr.lower()


def test_colliding_names_fail_the_second_entry_not_the_first(local_server, tmp_path):
    # FIX 4: two different names that both sanitise to "Part-12-Intro" — spec 7.1's
    # "the first input wins, every other fails with reason output_collision".
    listing = tmp_path / "parts.json"
    listing.write_text(
        json.dumps(
            [
                {"url": local_server, "name": "Part 1/2: Intro"},
                {"url": local_server, "name": "Part 12: Intro"},
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "media"
    result = _cli("download", "--list", str(listing), "-o", str(out), "--json")
    assert result.returncode == 1
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    items = [e for e in events if e["type"] == "item"]
    assert len(items) == 2
    assert items[0]["status"] == "done"
    assert items[1]["status"] == "failed"
    assert items[1]["reason"] == "output_collision"
    assert (out / "parts" / "Part-12-Intro.mp4").is_file()


def test_default_batch_name_is_independent_of_cwd(local_server, tmp_path):
    # FIX 5: the same URL, run from two different working directories, must land in
    # the same batch — this is also what makes FIX 1's skip/resume reliable.
    out = tmp_path / "media"
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    dir_b = tmp_path / "b"
    dir_b.mkdir()

    first = _cli("download", local_server, "-o", str(out), cwd=str(dir_a))
    assert first.returncode == 0
    batches_after_first = {p.name for p in out.iterdir() if p.is_dir()}
    assert len(batches_after_first) == 1

    second = _cli("download", local_server, "-o", str(out), cwd=str(dir_b))
    assert second.returncode == 0
    assert {p.name for p in out.iterdir() if p.is_dir()} == batches_after_first


def test_format_overrides_best_when_both_given(local_server, tmp_path):
    # FIX 6: --format's help text says it "overrides --best" — it must not be a
    # UsageError to pass both.
    out = tmp_path / "media"
    result = _cli(
        "download",
        local_server,
        "--best",
        "--format",
        "mp4",
        "-o",
        str(out),
        "-b",
        "fb",
        "--name",
        "clip",
    )
    assert result.returncode == 0
    assert (out / "fb" / "clip.mp4").is_file()
