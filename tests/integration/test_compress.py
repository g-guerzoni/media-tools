import json
import subprocess
import sys

from media_tools.core.ffmpeg import probe


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "media_tools", *args], capture_output=True, text=True
    )


def test_single_file_is_compressed(make_video, tmp_path):
    video = make_video(seconds=2, name="lecture.mp4")
    out = tmp_path / "media"
    result = _cli("compress", str(video), "-o", str(out), "-b", "one", "-p", "tiny")
    assert result.returncode == 0
    produced = out / "one" / "lecture.mp4"
    assert produced.is_file()
    assert probe(produced).duration_s > 1.5


def test_batch_folder_and_rerun_skips(make_video, tmp_path):
    folder = make_video(seconds=1, name="in/a.mp4").parent
    make_video(seconds=1, name="in/b.mp4")
    out = tmp_path / "media"

    first = _cli("compress", str(folder), "-o", str(out), "-b", "many", "--json")
    assert first.returncode == 0
    events = [json.loads(line) for line in first.stdout.splitlines() if line.strip()]
    assert sum(1 for e in events if e["type"] == "item" and e["status"] == "done") == 2

    second = _cli("compress", str(folder), "-o", str(out), "-b", "many", "--json")
    events = [json.loads(line) for line in second.stdout.splitlines() if line.strip()]
    assert all(e["reason"] == "exists" for e in events if e["type"] == "item")


def test_preset_change_creates_a_new_batch(make_video, tmp_path):
    video = make_video(seconds=1)
    out = tmp_path / "media"
    _cli("compress", str(video), "-o", str(out), "-p", "medium")
    _cli("compress", str(video), "-o", str(out), "-p", "tiny")
    batches = sorted(p.name for p in out.iterdir() if p.is_dir())
    assert len(batches) == 2


def test_crf_out_of_range_exits_2(make_video, tmp_path):
    video = make_video(seconds=1)
    result = _cli("compress", str(video), "-o", str(tmp_path / "m"), "--crf", "99")
    assert result.returncode == 2


def test_failed_encode_leaves_no_partial_or_output_file(tmp_path):
    # Not real media — same extension, garbage bytes. ffmpeg fails to open it, so the
    # engine must fail cleanly: no `.partial` temp file and no output file behind.
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a real video, just garbage bytes" * 4)
    out = tmp_path / "media"

    result = _cli("compress", str(broken), "-o", str(out), "-b", "broke", "--json")
    assert result.returncode == 1
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    item_events = [e for e in events if e["type"] == "item"]
    assert len(item_events) == 1
    assert item_events[0]["status"] == "failed"
    assert item_events[0]["reason"] == "engine_error"

    batch_dir = out / "broke"
    assert not (batch_dir / "broken.mp4").exists()
    assert not any(batch_dir.rglob(".*.partial"))
