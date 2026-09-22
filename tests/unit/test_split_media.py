import io
import os
from pathlib import Path
from types import SimpleNamespace

from media_tools.core.events import Reporter
from media_tools.core.ffmpeg import Probe
from media_tools.core.runner import Context, Item
from media_tools.tasks.split.media import MARGIN_RATIO, MAX_ATTEMPTS, MediaSplitEngine


def _args(**overrides):
    base = dict(max_size="1MB")
    base.update(overrides)
    return SimpleNamespace(**base)


def _reporter():
    return Reporter(json_mode=False, quiet=True, stdout=io.StringIO(), stderr=io.StringIO())


def _context(tmp_path, *, max_bytes, total_items=1, ffmpeg="ffmpeg"):
    return Context(
        batch_dir=tmp_path,
        reporter=_reporter(),
        deps={"ffmpeg": ffmpeg, "max_bytes": max_bytes},
        total_items=total_items,
    )


def _item(tmp_path, name="clip.mp4", source_size=1000):
    source = tmp_path / name
    source.write_bytes(b"x" * source_size)
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{Path(name).stem}.part01{Path(name).suffix}"
    return Item(id=1, source=source, root=None, outputs=[target])


# -- inputs / naming -------------------------------------------------------


def test_inputs_accept_the_union_of_video_and_audio_extensions():
    engine = MediaSplitEngine()
    assert ".mp4" in engine.inputs
    assert ".mp3" in engine.inputs
    assert ".m4a" in engine.inputs


def test_output_names_predicts_unchanged_name_when_already_under_the_limit(tmp_path):
    source = tmp_path / "small.mp4"
    source.write_bytes(b"x" * 100)
    names = MediaSplitEngine().output_names(source, _args(max_size="1MB"))
    assert names == ["small.mp4"]


def test_output_names_predicts_part01_when_over_the_limit(tmp_path):
    source = tmp_path / "big.mp4"
    source.write_bytes(b"x" * 2_000_000)
    names = MediaSplitEngine().output_names(source, _args(max_size="1MB"))
    assert names == ["big.part01.mp4"]


# -- already under the limit: placement, not re-encoding -------------------


def test_under_limit_file_is_hard_linked_into_the_batch(tmp_path, monkeypatch):
    engine = MediaSplitEngine()
    item = _item(tmp_path, source_size=100)
    ctx = _context(tmp_path, max_bytes=1_000_000)

    monkeypatch.setattr(
        "media_tools.tasks.split.media.probe", lambda p: Probe(duration_s=1.0, bitrate_bps=None)
    )

    outcome = engine.process(item, ctx)

    assert outcome.status == "done"
    assert outcome.reason == "under_limit"
    assert outcome.data == {"parts": 1, "placed": "link"}
    placed = outcome.outputs[0]
    assert placed.name == item.source.name
    assert os.stat(placed).st_ino == os.stat(item.source).st_ino


def test_under_limit_file_falls_back_to_copy_when_link_fails(tmp_path, monkeypatch):
    # os.link raises EXDEV whenever -o points at a different filesystem (an external
    # drive, a network share) — the realistic path, not a rare fallback.
    engine = MediaSplitEngine()
    item = _item(tmp_path, source_size=100)
    ctx = _context(tmp_path, max_bytes=1_000_000)

    monkeypatch.setattr(
        "media_tools.tasks.split.media.probe", lambda p: Probe(duration_s=1.0, bitrate_bps=None)
    )

    def boom(_src, _dst):
        raise OSError("cross-device link")

    monkeypatch.setattr("media_tools.tasks.split.media.os.link", boom)

    outcome = engine.process(item, ctx)

    assert outcome.status == "done"
    assert outcome.reason == "under_limit"
    assert outcome.data == {"parts": 1, "placed": "copy"}
    assert outcome.outputs[0].read_bytes() == item.source.read_bytes()
    assert not list((tmp_path / "out").glob(".*.partial"))


def test_copy_failure_midway_leaves_neither_the_output_nor_a_partial(tmp_path, monkeypatch):
    # A copy (unlike the hard link) is not atomic: an interruption partway through must
    # not leave a truncated file at the real output name — that would be indistinguishable
    # from a finished split on the next run and silently pass corruption through.
    engine = MediaSplitEngine()
    item = _item(tmp_path, source_size=100)
    ctx = _context(tmp_path, max_bytes=1_000_000)

    monkeypatch.setattr(
        "media_tools.tasks.split.media.probe", lambda p: Probe(duration_s=1.0, bitrate_bps=None)
    )
    monkeypatch.setattr(
        "media_tools.tasks.split.media.os.link",
        lambda _src, _dst: (_ for _ in ()).throw(OSError("cross-device link")),
    )

    def half_written_then_boom(_src, dst):
        Path(dst).write_bytes(b"partial bytes")
        raise OSError("disk full")

    monkeypatch.setattr("media_tools.tasks.split.media.shutil.copyfile", half_written_then_boom)

    try:
        engine.process(item, ctx)
        raised = False
    except OSError:
        raised = True

    assert raised
    out_dir = tmp_path / "out"
    assert not (out_dir / item.source.name).exists()
    assert not list(out_dir.glob(".*.partial"))


def test_unreadable_file_fails_even_when_its_bytes_are_under_the_limit(tmp_path, monkeypatch):
    # The old script's regression: a small, corrupt file must not be silently "placed"
    # unchanged just because its byte size happens to be under the limit.
    engine = MediaSplitEngine()
    item = _item(tmp_path, source_size=10)
    ctx = _context(tmp_path, max_bytes=1_000_000)

    monkeypatch.setattr(
        "media_tools.tasks.split.media.probe", lambda p: Probe(duration_s=None, bitrate_bps=None)
    )

    outcome = engine.process(item, ctx)

    assert outcome.status == "failed"
    assert outcome.reason == "engine_error"
    assert outcome.outputs == []
    assert not (tmp_path / "out" / item.source.name).exists()


# -- splitting: size retry ---------------------------------------------------


def test_oversized_part_is_retried_with_a_smaller_budget_then_succeeds(tmp_path, monkeypatch):
    engine = MediaSplitEngine()
    item = _item(tmp_path, source_size=5_000_000)
    ctx = _context(tmp_path, max_bytes=1_000_000)

    # A 2.0s total that a single (margin-shrunk) part fully advances past, so the loop
    # stops after exactly one part — keeping the retry count in this test unambiguous.
    monkeypatch.setattr(
        "media_tools.tasks.split.media.probe", lambda p: Probe(duration_s=2.0, bitrate_bps=None)
    )

    budgets_seen = []
    attempt = {"n": 0}

    def fake_run_ffmpeg(argv, *, total_s=None, on_progress=None):
        budget = int(argv[argv.index("-fs") + 1])
        budgets_seen.append(budget)
        attempt["n"] += 1
        temp = Path(argv[-1])
        # First attempt overshoots the limit; the second attempt (5% smaller budget) fits.
        size = 1_100_000 if attempt["n"] == 1 else 900_000
        temp.write_bytes(b"y" * size)
        return 0, ""

    monkeypatch.setattr("media_tools.tasks.split.media.run_ffmpeg", fake_run_ffmpeg)

    outcome = engine.process(item, ctx)

    assert outcome.status == "done"
    assert len(budgets_seen) == 2
    assert budgets_seen[1] < budgets_seen[0]
    assert all(p.stat().st_size <= 1_000_000 for p in outcome.outputs)


def test_part_still_oversized_after_max_attempts_fails_and_keeps_no_part(tmp_path, monkeypatch):
    engine = MediaSplitEngine()
    item = _item(tmp_path, source_size=5_000_000)
    ctx = _context(tmp_path, max_bytes=1_000_000)

    monkeypatch.setattr(
        "media_tools.tasks.split.media.probe",
        lambda p: Probe(duration_s=10.0, bitrate_bps=None),
    )

    attempts = {"n": 0}

    def fake_run_ffmpeg(argv, *, total_s=None, on_progress=None):
        attempts["n"] += 1
        Path(argv[-1]).write_bytes(b"y" * 1_100_000)  # always oversized
        return 0, ""

    monkeypatch.setattr("media_tools.tasks.split.media.run_ffmpeg", fake_run_ffmpeg)

    outcome = engine.process(item, ctx)

    assert outcome.status == "failed"
    assert outcome.reason == "size_limit_unreachable"
    assert attempts["n"] == MAX_ATTEMPTS
    assert outcome.outputs == []
    out_dir = tmp_path / "out"
    assert not list(out_dir.glob("*.mp4"))
    assert not list(out_dir.glob(".*.partial"))


# -- splitting: a keyframe interval that cannot fit ------------------------


def test_a_part_that_cannot_advance_fails_with_keyframe_interval_reason(tmp_path, monkeypatch):
    engine = MediaSplitEngine()
    item = _item(tmp_path, source_size=5_000_000)
    ctx = _context(tmp_path, max_bytes=1_000_000)

    monkeypatch.setattr(
        "media_tools.tasks.split.media.probe",
        lambda p: Probe(duration_s=10.0 if p == item.source else 0.0, bitrate_bps=None),
    )

    def fake_run_ffmpeg(argv, *, total_s=None, on_progress=None):
        Path(argv[-1]).write_bytes(b"y" * 900_000)  # fits the budget on the first try
        return 0, ""

    monkeypatch.setattr("media_tools.tasks.split.media.run_ffmpeg", fake_run_ffmpeg)

    outcome = engine.process(item, ctx)

    assert outcome.status == "failed"
    assert outcome.reason == "keyframe_interval_exceeds_max_size"
    assert outcome.outputs == []


def test_ffmpeg_failure_mid_split_fails_the_item_and_cleans_up_prior_parts(tmp_path, monkeypatch):
    engine = MediaSplitEngine()
    item = _item(tmp_path, source_size=5_000_000)
    ctx = _context(tmp_path, max_bytes=1_000_000)

    monkeypatch.setattr(
        "media_tools.tasks.split.media.probe",
        lambda p: (
            Probe(duration_s=10.0, bitrate_bps=None) if p == item.source else Probe(3.0, None)
        ),
    )

    calls = {"n": 0}

    def fake_run_ffmpeg(argv, *, total_s=None, on_progress=None):
        calls["n"] += 1
        temp = Path(argv[-1])
        if calls["n"] == 1:
            temp.write_bytes(b"y" * 500_000)
            return 0, ""
        return 1, "boom"

    monkeypatch.setattr("media_tools.tasks.split.media.run_ffmpeg", fake_run_ffmpeg)

    outcome = engine.process(item, ctx)

    assert outcome.status == "failed"
    assert outcome.reason == "engine_error"
    out_dir = tmp_path / "out"
    assert not list(out_dir.glob("*.mp4"))
    assert not list(out_dir.glob(".*.partial"))


# -- final naming -------------------------------------------------------


def test_final_parts_are_zero_padded_to_the_part_count(tmp_path, monkeypatch):
    engine = MediaSplitEngine()
    item = _item(tmp_path, source_size=5_000_000)
    ctx = _context(tmp_path, max_bytes=1_000_000)

    monkeypatch.setattr(
        "media_tools.tasks.split.media.probe",
        lambda p: (
            Probe(duration_s=10.0, bitrate_bps=None) if p == item.source else Probe(2.5, None)
        ),
    )

    def fake_run_ffmpeg(argv, *, total_s=None, on_progress=None):
        Path(argv[-1]).write_bytes(b"y" * 500_000)
        return 0, ""

    monkeypatch.setattr("media_tools.tasks.split.media.run_ffmpeg", fake_run_ffmpeg)

    outcome = engine.process(item, ctx)

    assert outcome.status == "done"
    names = sorted(p.name for p in outcome.outputs)
    assert names[0] == "clip.part01.mp4"
    assert all(len(n.split("part")[1].split(".")[0]) == 2 for n in names)


def test_margin_ratio_and_max_attempts_match_the_documented_contract():
    assert MARGIN_RATIO == 0.02
    assert MAX_ATTEMPTS == 3
