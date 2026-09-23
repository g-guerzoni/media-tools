import io
import json

import pytest

from media_tools.core.events import Reporter


def _lines(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def test_json_mode_writes_only_to_stdout():
    out, err = io.StringIO(), io.StringIO()
    reporter = Reporter(json_mode=True, quiet=False, stdout=out, stderr=err)
    reporter.start(
        tool="compress", batch="b", output_dir="/out", stages=["scan"], items=1, options={"crf": 28}
    )
    reporter.item(
        id=1, status="done", input="/in/a.mp4", outputs=["/out/a.mp4"], bytes_in=10, bytes_out=5
    )
    reporter.result(
        ok=True,
        exit_code=0,
        counts={"total": 1, "done": 1},
        failed=[],
        pending=[],
        outputs=["/out/a.mp4"],
        run_file="/out/run.json",
        elapsed_s=1.0,
    )

    events = _lines(out)
    assert err.getvalue() == ""
    assert [e["type"] for e in events] == ["start", "item", "result"]
    assert all(e["v"] == 1 for e in events)


def test_human_mode_writes_only_to_stderr():
    out, err = io.StringIO(), io.StringIO()
    reporter = Reporter(json_mode=False, quiet=False, stdout=out, stderr=err)
    reporter.start(
        tool="compress", batch="b", output_dir="/out", stages=["scan"], items=1, options={}
    )
    reporter.item(
        id=1, status="done", input="/in/a.mp4", outputs=["/out/a.mp4"], bytes_in=10, bytes_out=5
    )
    assert out.getvalue() == ""
    assert "compress" in err.getvalue()
    assert "a.mp4" in err.getvalue()


def test_unknown_codes_are_rejected():
    reporter = Reporter(json_mode=True, quiet=False, stdout=io.StringIO(), stderr=io.StringIO())
    with pytest.raises(KeyError):
        reporter.error(code="made_up", message="x")
    with pytest.raises(KeyError):
        reporter.item(
            id=1,
            status="failed",
            input="/a",
            outputs=[],
            bytes_in=0,
            bytes_out=None,
            reason="invented_reason",
        )


def test_urls_are_redacted_everywhere():
    out = io.StringIO()
    reporter = Reporter(json_mode=True, quiet=False, stdout=out, stderr=io.StringIO())
    reporter.item(
        id=1,
        status="failed",
        input="https://host/v/x.m3u8?sjwt=SECRET&uid=9",
        outputs=[],
        bytes_in=0,
        bytes_out=None,
        reason="engine_error",
    )
    line = out.getvalue()
    assert "SECRET" not in line
    assert "https://host/v/x.m3u8" in line


def test_urls_in_progress_and_options_are_redacted_on_both_streams():
    out, err = io.StringIO(), io.StringIO()
    reporter = Reporter(json_mode=False, quiet=False, stdout=out, stderr=err)
    reporter.start(
        tool="download",
        batch="b",
        output_dir="/out",
        stages=["fetch"],
        items=1,
        options={"url": "https://host/video?token=SECRET123"},
    )
    reporter.progress(
        stage="fetch",
        index=1,
        count=1,
        path="https://host/file?auth=HIDDEN",
        percent=50.0,
    )

    human_output = err.getvalue()
    assert "SECRET123" not in human_output
    assert "HIDDEN" not in human_output
    assert "https://host/video" in human_output
    assert "https://host/file" in human_output


def test_urls_in_json_stream_also_redacted():
    out, err = io.StringIO(), io.StringIO()
    reporter = Reporter(json_mode=True, quiet=False, stdout=out, stderr=err)
    reporter.start(
        tool="download",
        batch="b",
        output_dir="/out",
        stages=["fetch"],
        items=1,
        options={"url": "https://host/video?token=SECRET123"},
    )

    events = _lines(out)
    json_str = json.dumps(events)
    assert "SECRET123" not in json_str
    assert "https://host/video" in json_str


def test_error_messages_are_redacted():
    out, err = io.StringIO(), io.StringIO()
    reporter = Reporter(json_mode=False, quiet=False, stdout=out, stderr=err)
    reporter.error(
        code="usage",
        message="download failed: https://host/x.m3u8?sjwt=SECRET&uid=9",
        hint="check https://host/help?token=ABC",
    )

    error_output = err.getvalue()
    # Tokens must be stripped
    assert "SECRET" not in error_output
    assert "ABC" not in error_output
    # URLs' paths must still appear
    assert "https://host/x.m3u8" in error_output
    assert "https://host/help" in error_output


def test_error_written_even_when_quiet_or_json():
    # Error should be visible even under --quiet
    err = io.StringIO()
    reporter = Reporter(json_mode=False, quiet=True, stderr=err)
    reporter.error(code="usage", message="fatal error")
    assert "fatal error" in err.getvalue()

    # Error should be visible even under --json
    out, err = io.StringIO(), io.StringIO()
    reporter = Reporter(json_mode=True, quiet=False, stdout=out, stderr=err)
    reporter.error(code="usage", message="fatal error")
    assert "fatal error" in err.getvalue()
    # JSON event also present
    assert json.loads(out.getvalue())["type"] == "error"


def test_result_with_nothing_to_summarise_prints_no_human_line():
    """A `result` whose counts are all zero and that never owned a batch (`run_file`
    is `None`) never did any work — most commonly a `UsageError` raised before a
    task's own `start`. The human already saw the `error` line; a bare, content-free
    "✗  · 0.0s" underneath it is noise, not information."""
    err = io.StringIO()
    reporter = Reporter(json_mode=False, quiet=False, stdout=io.StringIO(), stderr=err)
    reporter.error(code="usage", message="no input given")
    reporter.result(
        ok=False,
        exit_code=2,
        counts={"total": 0, "done": 0, "skipped": 0, "failed": 0, "pending": 0},
        failed=[],
        pending=[],
        outputs=[],
        run_file=None,
        elapsed_s=0.0,
    )
    assert "no input given" in err.getvalue()
    assert "✗" not in err.getvalue()


def test_result_with_real_counts_still_prints_the_human_line():
    """The suppression above must not swallow a genuine summary — only the
    nothing-happened case."""
    err = io.StringIO()
    reporter = Reporter(json_mode=False, quiet=False, stdout=io.StringIO(), stderr=err)
    reporter.result(
        ok=True,
        exit_code=0,
        counts={"total": 1, "done": 1, "skipped": 0, "failed": 0, "pending": 0},
        failed=[],
        pending=[],
        outputs=["/out/a.mp4"],
        run_file="/out/run.json",
        elapsed_s=1.0,
    )
    assert "✓" in err.getvalue()
    assert "done 1" in err.getvalue()


def test_result_with_a_run_file_but_all_zero_counts_still_prints(tmp_path):
    """`run_file is not None` alone is enough to summarise, even if every count
    happens to be zero (a batch that was opened but never processed an item) — the
    suppression is specifically for a run that never owned a batch at all."""
    err = io.StringIO()
    reporter = Reporter(json_mode=False, quiet=False, stdout=io.StringIO(), stderr=err)
    reporter.result(
        ok=True,
        exit_code=0,
        counts={"total": 0, "done": 0, "skipped": 0, "failed": 0, "pending": 0},
        failed=[],
        pending=[],
        outputs=[],
        run_file=str(tmp_path / "run.json"),
        elapsed_s=0.5,
    )
    assert "✓" in err.getvalue()
