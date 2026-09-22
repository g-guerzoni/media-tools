import subprocess
import sys

from media_tools.core.ffmpeg import probe


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "media_tools", *args], capture_output=True, text=True
    )


def test_video_to_mp3(make_video, tmp_path):
    video = make_video(seconds=2, name="talk.mp4")
    out = tmp_path / "media"
    assert _cli("convert", str(video), "--to", "mp3", "-o", str(out), "-b", "x").returncode == 0
    produced = out / "x" / "talk.mp3"
    assert produced.is_file()
    assert probe(produced).duration_s > 1.5


def test_audio_input_is_accepted(make_audio, tmp_path):
    audio = make_audio(seconds=2, name="voice.m4a")
    out = tmp_path / "media"
    assert _cli("convert", str(audio), "--to", "mp3", "-o", str(out), "-b", "x").returncode == 0
    assert (out / "x" / "voice.mp3").is_file()


def test_missing_to_flag_exits_2(make_video, tmp_path):
    video = make_video(seconds=1)
    assert _cli("convert", str(video), "-o", str(tmp_path / "m")).returncode == 2


def test_unsupported_target_exits_2(make_video, tmp_path):
    video = make_video(seconds=1)
    result = _cli("convert", str(video), "--to", "flac", "-o", str(tmp_path / "m"))
    assert result.returncode == 2


def test_input_is_never_overwritten(make_audio, tmp_path):
    source = make_audio(seconds=1, name="same.mp3")
    before = source.read_bytes()
    result = _cli(
        "convert",
        str(source),
        "--to",
        "mp3",
        "-o",
        str(source.parent.parent),
        "-b",
        source.parent.name,
        "--force",
    )
    assert result.returncode == 1
    assert source.read_bytes() == before
