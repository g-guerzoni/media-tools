"""In-process tests of `_download_all`'s control flow (batch/state coordination),
mirroring tests/unit/test_runner.py's style: a `Reporter` writing to an `io.StringIO`
and a monkeypatched per-item function, rather than a real subprocess + network."""

import io
import json

from media_tools.core.events import EXIT_INTERRUPTED, Reporter
from media_tools.core.runner import Outcome
from media_tools.tasks import download as download_task
from media_tools.tasks.download.ytdlp import Entry


def _json_lines(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


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
