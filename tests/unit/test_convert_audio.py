import io
import json
from pathlib import Path
from types import SimpleNamespace

from media_tools.core.events import Reporter
from media_tools.core.ffmpeg import Probe
from media_tools.core.runner import Context, Item
from media_tools.tasks.convert.audio import QUALITY, AudioEngine


def _args(**overrides):
    base = dict(to="mp3", quality="high")
    base.update(overrides)
    return SimpleNamespace(**base)


def _reporter(*, json_mode=False, stdout=None):
    return Reporter(
        json_mode=json_mode,
        quiet=not json_mode,
        stdout=stdout or io.StringIO(),
        stderr=io.StringIO(),
    )


def _item(tmp_path, name="clip.mp4", source_size=1000):
    source = tmp_path / name
    source.write_bytes(b"x" * source_size)
    target = tmp_path / "out" / f"{Path(name).stem}.mp3"
    target.parent.mkdir(parents=True, exist_ok=True)
    return Item(id=1, source=source, root=None, outputs=[target])


def _context(tmp_path, options, *, total_items=1, reporter=None, ffmpeg="ffmpeg"):
    return Context(
        batch_dir=tmp_path,
        reporter=reporter or _reporter(),
        deps={"ffmpeg": ffmpeg, "options": options},
        total_items=total_items,
    )


def test_inputs_accept_both_video_and_audio_extensions():
    engine = AudioEngine()
    assert ".mp4" in engine.inputs
    assert ".m4a" in engine.inputs
    assert engine.outputs == frozenset({"mp3"})


def test_hash_options_maps_quality_to_bitrate():
    assert AudioEngine().hash_options(_args(quality="low")) == {"to": "mp3", "bitrate": "96k"}
    assert AudioEngine().hash_options(_args(quality="medium")) == {"to": "mp3", "bitrate": "192k"}
    assert AudioEngine().hash_options(_args(quality="high")) == {
        "to": "mp3",
        "bitrate": QUALITY["high"],
    }


def test_hash_options_includes_the_target_format_so_a_future_second_target_gets_its_own_batch():
    # The batch name is derived from these options; without "to", converting the same
    # input to two different formats at the same bitrate would collide on one batch.
    assert AudioEngine().hash_options(_args(to="mp3")) == {"to": "mp3", "bitrate": "320k"}


def test_output_names_replaces_extension_with_mp3():
    assert AudioEngine().output_names(Path("talk.mp4"), _args()) == ["talk.mp3"]
    assert AudioEngine().output_names(Path("voice.m4a"), _args()) == ["voice.mp3"]


def test_process_success_writes_output_and_leaves_no_temp(tmp_path, monkeypatch):
    engine = AudioEngine()
    item = _item(tmp_path, name="talk.mp4")
    ctx = _context(tmp_path, engine.hash_options(_args()))
    captured = {}

    def fake_probe(path):
        return Probe(duration_s=4.0, bitrate_bps=None)

    def fake_run_ffmpeg(argv, *, total_s=None, on_progress=None):
        captured["argv"] = argv
        captured["total_s"] = total_s
        if on_progress:
            on_progress(50.0)
        Path(argv[-1]).write_bytes(b"y" * 10)
        return 0, ""

    monkeypatch.setattr("media_tools.tasks.convert.audio.probe", fake_probe)
    monkeypatch.setattr("media_tools.tasks.convert.audio.run_ffmpeg", fake_run_ffmpeg)

    outcome = engine.process(item, ctx)

    assert outcome.status == "done"
    assert outcome.outputs == [item.outputs[0]]
    assert outcome.bytes_out == 10
    assert item.outputs[0].read_bytes() == b"y" * 10
    assert captured["total_s"] == 4.0
    assert "-vn" in captured["argv"]
    assert "libmp3lame" in captured["argv"]
    assert not list(item.outputs[0].parent.glob(".*.partial"))


def test_process_failure_removes_partial_and_output(tmp_path, monkeypatch):
    engine = AudioEngine()
    item = _item(tmp_path)
    ctx = _context(tmp_path, engine.hash_options(_args()))

    def fake_probe(path):
        return Probe(duration_s=None, bitrate_bps=None)

    def fake_run_ffmpeg(argv, *, total_s=None, on_progress=None):
        Path(argv[-1]).write_bytes(b"partial garbage")
        return 1, "Error opening input file"

    monkeypatch.setattr("media_tools.tasks.convert.audio.probe", fake_probe)
    monkeypatch.setattr("media_tools.tasks.convert.audio.run_ffmpeg", fake_run_ffmpeg)

    outcome = engine.process(item, ctx)

    assert outcome.status == "failed"
    assert outcome.reason == "engine_error"
    assert outcome.outputs == []
    assert not item.outputs[0].exists()
    assert not list(item.outputs[0].parent.glob(".*.partial"))


def test_progress_reports_the_real_item_id_and_batch_total(tmp_path, monkeypatch):
    """Regression guard (RULING R4): progress must report ctx.total_items as the count,
    not item.id (which would claim e.g. "2 of 2" for the second of seven files)."""
    engine = AudioEngine()
    item = _item(tmp_path)
    item.id = 2
    stdout = io.StringIO()
    reporter = _reporter(json_mode=True, stdout=stdout)
    ctx = _context(tmp_path, engine.hash_options(_args()), total_items=7, reporter=reporter)

    def fake_probe(path):
        return Probe(duration_s=4.0, bitrate_bps=None)

    def fake_run_ffmpeg(argv, *, total_s=None, on_progress=None):
        on_progress(37.5)
        Path(argv[-1]).write_bytes(b"y")
        return 0, ""

    monkeypatch.setattr("media_tools.tasks.convert.audio.probe", fake_probe)
    monkeypatch.setattr("media_tools.tasks.convert.audio.run_ffmpeg", fake_run_ffmpeg)

    engine.process(item, ctx)

    events = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    progress_events = [e for e in events if e["type"] == "progress"]
    assert len(progress_events) == 1
    assert progress_events[0]["item"]["index"] == 2
    assert progress_events[0]["item"]["count"] == 7
    assert progress_events[0]["percent"] == 37.5
