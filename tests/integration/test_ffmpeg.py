from media_tools.core.ffmpeg import probe, run_ffmpeg


def test_probe_reads_duration(make_video):
    video = make_video(seconds=3)
    result = probe(video)
    assert result.duration_s is not None
    assert 2.5 < result.duration_s < 3.5


def test_probe_of_a_non_media_file_returns_none(tmp_path):
    junk = tmp_path / "notes.txt"
    junk.write_text("not media")
    assert probe(junk).duration_s is None


def test_run_ffmpeg_reports_progress(make_video, tmp_path, ffmpeg_path):
    video = make_video(seconds=3)
    seen: list[float] = []
    code, _ = run_ffmpeg(
        [ffmpeg_path, "-y", "-i", str(video), "-c", "copy", str(tmp_path / "out.mp4")],
        total_s=3.0,
        on_progress=seen.append,
    )
    assert code == 0
    assert seen and max(seen) > 0
