import io
import json
from pathlib import Path
from types import SimpleNamespace

from media_tools.core.events import Reporter
from media_tools.core.ffmpeg import Probe
from media_tools.core.runner import Context, Item
from media_tools.tasks.compress.video import VideoEngine


def _args(**overrides):
    base = dict(preset="medium", codec=None, crf=None, audio_bitrate="96k", mono=False)
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
    target = tmp_path / "out" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    return Item(id=1, source=source, root=None, outputs=[target])


def _context(tmp_path, options, *, total_items=1, reporter=None, ffmpeg="ffmpeg"):
    return Context(
        batch_dir=tmp_path,
        reporter=reporter or _reporter(),
        deps={"ffmpeg": ffmpeg, "options": options},
        total_items=total_items,
    )


def test_hash_options_uses_preset_defaults():
    options = VideoEngine().hash_options(_args())
    assert options == {"codec": "h264", "crf": 28, "audio_bitrate": "96k", "mono": False}


def test_hash_options_lets_flags_override_the_preset():
    options = VideoEngine().hash_options(_args(preset="tiny", codec="h264", crf=40, mono=True))
    assert options == {"codec": "h264", "crf": 40, "audio_bitrate": "96k", "mono": True}


def test_output_names_replaces_extension_with_mp4():
    assert VideoEngine().output_names(Path("clip.mov"), _args()) == ["clip.mp4"]


def test_process_success_shrinks_file_and_leaves_no_temp(tmp_path, monkeypatch):
    engine = VideoEngine()
    item = _item(tmp_path, source_size=1000)
    ctx = _context(tmp_path, engine.hash_options(_args(preset="tiny")))
    captured = {}

    def fake_probe(path):
        return Probe(duration_s=9.0, bitrate_bps=None)

    def fake_run_ffmpeg(argv, *, total_s=None, on_progress=None):
        captured["total_s"] = total_s
        if on_progress:
            on_progress(50.0)
        Path(argv[-1]).write_bytes(b"y" * 10)
        return 0, ""

    monkeypatch.setattr("media_tools.tasks.compress.video.probe", fake_probe)
    monkeypatch.setattr("media_tools.tasks.compress.video.run_ffmpeg", fake_run_ffmpeg)

    outcome = engine.process(item, ctx)

    assert outcome.status == "done"
    assert outcome.warnings == []
    assert outcome.outputs == [item.outputs[0]]
    assert outcome.bytes_out == 10
    assert item.outputs[0].read_bytes() == b"y" * 10
    assert captured["total_s"] == 9.0
    assert not list(item.outputs[0].parent.glob(".*.partial"))


def test_process_flags_no_gain_but_keeps_the_output(tmp_path, monkeypatch):
    engine = VideoEngine()
    item = _item(tmp_path, source_size=1000)
    ctx = _context(tmp_path, engine.hash_options(_args()))

    def fake_probe(path):
        return Probe(duration_s=2.0, bitrate_bps=None)

    def fake_run_ffmpeg(argv, *, total_s=None, on_progress=None):
        Path(argv[-1]).write_bytes(b"z" * 2000)
        return 0, ""

    monkeypatch.setattr("media_tools.tasks.compress.video.probe", fake_probe)
    monkeypatch.setattr("media_tools.tasks.compress.video.run_ffmpeg", fake_run_ffmpeg)

    outcome = engine.process(item, ctx)

    assert outcome.status == "done"
    assert outcome.warnings == ["no_gain"]
    assert item.outputs[0].exists()


def test_process_failure_removes_partial_and_output(tmp_path, monkeypatch):
    engine = VideoEngine()
    item = _item(tmp_path)
    ctx = _context(tmp_path, engine.hash_options(_args()))

    def fake_probe(path):
        return Probe(duration_s=None, bitrate_bps=None)

    def fake_run_ffmpeg(argv, *, total_s=None, on_progress=None):
        # ffmpeg writes to the temp path before it fails partway through.
        Path(argv[-1]).write_bytes(b"partial garbage")
        return 1, "Error opening input file"

    monkeypatch.setattr("media_tools.tasks.compress.video.probe", fake_probe)
    monkeypatch.setattr("media_tools.tasks.compress.video.run_ffmpeg", fake_run_ffmpeg)

    outcome = engine.process(item, ctx)

    assert outcome.status == "failed"
    assert outcome.reason == "engine_error"
    assert outcome.outputs == []
    assert not item.outputs[0].exists()
    assert not list(item.outputs[0].parent.glob(".*.partial"))


def test_process_builds_argv_from_options_and_ffmpeg_dependency(tmp_path, monkeypatch):
    engine = VideoEngine()
    item = _item(tmp_path)
    options = engine.hash_options(_args(preset="small", mono=True))
    ctx = _context(tmp_path, options, ffmpeg="/opt/ffmpeg-bin")
    captured = {}

    def fake_probe(path):
        return Probe(duration_s=1.0, bitrate_bps=None)

    def fake_run_ffmpeg(argv, *, total_s=None, on_progress=None):
        captured["argv"] = argv
        Path(argv[-1]).write_bytes(b"y")
        return 0, ""

    monkeypatch.setattr("media_tools.tasks.compress.video.probe", fake_probe)
    monkeypatch.setattr("media_tools.tasks.compress.video.run_ffmpeg", fake_run_ffmpeg)

    engine.process(item, ctx)

    argv = captured["argv"]
    assert argv[0] == "/opt/ffmpeg-bin"
    assert "libx265" in argv  # "small" preset -> h265
    assert str(options["crf"]) in argv
    assert argv[argv.index("-ac") + 1] == "1"


def test_progress_reports_the_real_item_id_and_batch_total(tmp_path, monkeypatch):
    """Regression guard: progress must report ctx.total_items as the count, not item.id
    (which would claim e.g. "2 of 2" for the second of seven files)."""
    engine = VideoEngine()
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

    monkeypatch.setattr("media_tools.tasks.compress.video.probe", fake_probe)
    monkeypatch.setattr("media_tools.tasks.compress.video.run_ffmpeg", fake_run_ffmpeg)

    engine.process(item, ctx)

    events = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    progress_events = [e for e in events if e["type"] == "progress"]
    assert len(progress_events) == 1
    assert progress_events[0]["item"]["index"] == 2
    assert progress_events[0]["item"]["count"] == 7
    assert progress_events[0]["percent"] == 37.5
