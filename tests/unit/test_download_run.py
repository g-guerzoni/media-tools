"""In-process tests of `_download_all`'s control flow (batch/state coordination),
mirroring tests/unit/test_runner.py's style: a `Reporter` writing to an `io.StringIO`
and a monkeypatched per-item function, rather than a real subprocess + network."""

import io
import json

from media_tools.core import runner as core_runner
from media_tools.core.events import EXIT_INTERRUPTED, Reporter
from media_tools.core.runner import Outcome
from media_tools.tasks import download as download_task
from media_tools.tasks.download.ytdlp import Entry


def _json_lines(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def test_download_dry_run_counts_have_the_same_keys_as_a_real_run(tmp_path):
    entries = [Entry("http://x/1", "item1"), Entry("http://x/2", "item2")]
    out = io.StringIO()
    reporter = Reporter(json_mode=True, quiet=True, stdout=out, stderr=io.StringIO())

    code = download_task._dry_run(entries, batch_dir=tmp_path / "b", reporter=reporter, options={})
    assert code == 0
    (result,) = [e for e in _json_lines(out) if e["type"] == "result"]
    assert result["counts"].keys() >= {"total", "done", "skipped", "failed", "pending"}
    assert result["counts"]["total"] == 2
    assert result["counts"]["skipped"] == 2
    assert result["counts"]["done"] == 0
    assert result["counts"]["failed"] == 0
    assert result["counts"]["pending"] == 0


def test_download_summary_json_receives_the_result(tmp_path, monkeypatch):
    def fake_download_one(entry, **kwargs):
        output_dir = kwargs["output_dir"]
        target = output_dir / f"{entry.name}.mp4"
        target.write_bytes(b"x")
        return Outcome(status="done", outputs=[target], bytes_out=target.stat().st_size)

    monkeypatch.setattr(download_task, "download_one", fake_download_one)

    entries = [Entry("http://x/1", "item1")]
    summary = tmp_path / "summary.json"
    reporter = Reporter(json_mode=False, quiet=True, stdout=io.StringIO(), stderr=io.StringIO())

    code = download_task._download_all(
        entries,
        batch_dir=tmp_path / "b",
        reporter=reporter,
        ffmpeg="ffmpeg",
        type_="video",
        quality="worst",
        format_id=None,
        options={},
        force=False,
        stop_on_error=False,
        summary_json=summary,
    )

    assert code == 0
    payload = json.loads(summary.read_text())
    assert payload["type"] == "result"
    assert payload["ok"] is True


def test_download_shares_the_result_envelope_helpers_with_run_items():
    """I6: `download` used to hand-sync its own copies of `_empty_result`/`_build_result`
    and the --summary-json writer; it must use `core.runner`'s shared ones instead of a
    second copy that can drift."""
    assert download_task.build_result is core_runner.build_result
    assert download_task.empty_result is core_runner.empty_result
    assert download_task.write_summary_json is core_runner.write_summary_json


def test_keyboard_interrupt_mid_batch_reports_real_progress(tmp_path, monkeypatch):
    """FIX 2: the interrupted `result` event must reflect the batch's real, partial
    progress (and point at the real run.json) — not `_empty_result`'s all-zero, null
    `run_file` payload, which is only correct for a run that never owned a batch."""
    calls = {"n": 0}

    def fake_download_one(entry, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        output_dir = kwargs["output_dir"]
        target = output_dir / f"{entry.name}.mp4"
        target.write_bytes(b"x")
        return Outcome(status="done", outputs=[target], bytes_out=target.stat().st_size)

    monkeypatch.setattr(download_task, "download_one", fake_download_one)

    entries = [Entry(f"http://x/{i}", f"item{i}") for i in range(1, 4)]
    batch_dir = tmp_path / "b"
    out = io.StringIO()
    reporter = Reporter(json_mode=True, quiet=True, stdout=out, stderr=io.StringIO())

    code = download_task._download_all(
        entries,
        batch_dir=batch_dir,
        reporter=reporter,
        ffmpeg="ffmpeg",
        type_="video",
        quality="worst",
        format_id=None,
        options={},
        force=False,
        stop_on_error=False,
        summary_json=None,
    )

    assert code == EXIT_INTERRUPTED
    lines = _json_lines(out)
    assert lines[-1]["type"] == "result"
    assert lines[-1]["exit_code"] == EXIT_INTERRUPTED
    assert lines[-1]["ok"] is False
    assert lines[-1]["counts"]["done"] == 1
    assert lines[-1]["counts"]["pending"] == 2
    assert lines[-1]["run_file"] is not None
    assert (batch_dir / "run.json").is_file()
    # the first item's work is not lost
    assert (batch_dir / "item1.mp4").read_bytes() == b"x"


def test_download_declares_and_emits_resolve_then_download_stages(tmp_path, monkeypatch):
    """I2/R28: spec 9.4 names both `resolve` and `download` as download's stages; the
    code used to declare only `["download"]` and never emit a `stage` event at all."""

    def fake_download_one(entry, **kwargs):
        output_dir = kwargs["output_dir"]
        target = output_dir / f"{entry.name}.mp4"
        target.write_bytes(b"x")
        return Outcome(status="done", outputs=[target], bytes_out=target.stat().st_size)

    monkeypatch.setattr(download_task, "download_one", fake_download_one)

    entries = [Entry("http://x/1", "item1")]
    out = io.StringIO()
    reporter = Reporter(json_mode=True, quiet=True, stdout=out, stderr=io.StringIO())

    code = download_task._download_all(
        entries,
        batch_dir=tmp_path / "b",
        reporter=reporter,
        ffmpeg="ffmpeg",
        type_="video",
        quality="worst",
        format_id=None,
        options={},
        force=False,
        stop_on_error=False,
        summary_json=None,
    )

    assert code == 0
    lines = _json_lines(out)
    (start,) = [e for e in lines if e["type"] == "start"]
    assert start["stages"] == ["resolve", "download"]
    stages = [e for e in lines if e["type"] == "stage"]
    assert [(s["stage"], s["index"], s["count"]) for s in stages] == [
        ("resolve", 1, 2),
        ("download", 2, 2),
    ]
