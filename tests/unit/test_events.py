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
