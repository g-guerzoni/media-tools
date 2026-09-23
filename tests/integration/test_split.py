import json
import subprocess
import sys

from media_tools.core.ffmpeg import probe


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "media_tools", *args], capture_output=True, text=True
    )


def test_parts_never_exceed_the_limit_and_cover_the_source(make_video, tmp_path):
    # Long enough that a third of its size still clears --max-size's 1MB floor. An
    # explicit gop=15 (1 keyframe/s at 15fps) gives every ffmpeg build the same cut
    # points — this test must not depend on a build's default keyframe interval, which
    # is exactly what made it fail in CI while passing locally (a build that placed
    # keyframes much further apart put the requested limit out of reach for the old,
    # non-converging retry ladder).
    video = make_video(seconds=130, name="long.mp4", size="640x480", gop=15)
    limit = video.stat().st_size // 3
    out = tmp_path / "media"

    # A bare number means MB (parse_size's project-wide convention), so an exact byte
    # threshold computed from a real file's size must carry an explicit "B" suffix.
    result = _cli("split", str(video), "--max-size", f"{limit}B", "-o", str(out), "-b", "s")
    assert result.returncode == 0

    parts = sorted((out / "s").glob("long.part*.mp4"))
    assert len(parts) >= 3
    assert all(p.stat().st_size <= limit for p in parts)

    source_duration = probe(video).duration_s
    total = sum(probe(p).duration_s for p in parts)
    assert total >= source_duration - 0.5  # overlap allowed, loss is not


def test_file_under_the_limit_is_placed_unchanged(make_video, tmp_path):
    video = make_video(seconds=1, name="small.mp4")
    out = tmp_path / "media"
    result = _cli("split", str(video), "--max-size", "500MB", "-o", str(out), "-b", "u", "--json")
    assert result.returncode == 0
    placed = out / "u" / "small.mp4"
    assert placed.stat().st_size == video.stat().st_size

    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    item = next(e for e in events if e["type"] == "item")
    assert item["reason"] == "under_limit"


def test_batch_of_two_files(make_video, tmp_path):
    # Long enough that half its size still clears --max-size's 1MB floor. Explicit gop=15
    # for the same reason as above: cut points must not depend on a build's default.
    folder = make_video(seconds=70, name="in/one.mp4", size="640x480", gop=15).parent
    make_video(seconds=70, name="in/two.mp4", size="640x480", gop=15)
    out = tmp_path / "media"
    limit = (folder / "one.mp4").stat().st_size // 2
    result = _cli("split", str(folder), "--max-size", f"{limit}B", "-o", str(out), "-b", "b")
    assert result.returncode == 0
    assert len(list((out / "b").glob("one.part*.mp4"))) >= 2
    assert len(list((out / "b").glob("two.part*.mp4"))) >= 2


def test_corrupt_file_fails_only_that_item(make_video, tmp_path):
    folder = make_video(seconds=5, name="in/good.mp4").parent
    (folder / "bad.mp4").write_bytes(b"not a video")
    out = tmp_path / "media"
    result = _cli("split", str(folder), "--max-size", "1MB", "-o", str(out), "-b", "m", "--json")
    assert result.returncode == 1
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    statuses = {e["input"].split("/")[-1]: e["status"] for e in events if e["type"] == "item"}
    assert statuses["bad.mp4"] == "failed"
    assert statuses["good.mp4"] in {"done"}


def test_max_size_below_minimum_exits_2(make_video, tmp_path):
    video = make_video(seconds=1)
    assert (
        _cli("split", str(video), "--max-size", "10KB", "-o", str(tmp_path / "m")).returncode == 2
    )
