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


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "media_tools", *args], capture_output=True, text=True
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
