from pathlib import Path

import pytest

from media_tools.cli import build_parser
from media_tools.core import inputs
from media_tools.tasks.serve import requests
from media_tools.tasks.serve.requests import RequestError


@pytest.fixture
def root(tmp_path):
    base = tmp_path / "in" / "app1"
    base.mkdir(parents=True)
    (base / "talk.mp4").write_bytes(b"x")
    (base / "books").mkdir()
    (base / "list.json").write_text("[]")
    (tmp_path / "in" / "app2").mkdir()
    (tmp_path / "in" / "app2" / "secret.mp4").write_bytes(b"x")
    return base


def _plan(request, root, max_workers=1):
    return requests.plan(request, parser=build_parser(), input_root=root, max_workers=max_workers)


def test_a_simple_request_becomes_a_cli_command_line(root, tmp_path):
    plan = _plan({"task": "convert", "inputs": ["talk.mp4"], "options": {"to": "mp3"}}, root)
    out = tmp_path / "out" / "app1"
    assert plan.command_line(out) == [
        "convert",
        "--to=mp3",
        "--json",
        f"--output-dir={out}",
        "--",
        str(root / "talk.mp4"),
    ]


def test_the_command_line_parses_as_the_real_cli(root, tmp_path):
    plan = _plan({"task": "split", "inputs": ["talk.mp4"], "options": {"max-size": "25MB"}}, root)
    args = build_parser().parse_args(plan.command_line(tmp_path / "out"))
    assert args.max_size == "25MB"
    assert args.json_mode
    assert args.output_dir == tmp_path / "out"


@pytest.mark.parametrize(
    "key", ["output-dir", "json", "quiet", "summary-json", "op-item", "list-formats", "help"]
)
def test_flags_only_the_server_controls_are_refused(root, key):
    with pytest.raises(RequestError, match="not accepted"):
        _plan({"task": "compress", "inputs": ["talk.mp4"], "options": {key: "x"}}, root)


def test_an_unknown_option_is_refused(root):
    with pytest.raises(RequestError, match="not accepted"):
        _plan({"task": "compress", "inputs": ["talk.mp4"], "options": {"bogus": 1}}, root)


def test_a_value_starting_with_a_dash_stays_a_value(root, tmp_path):
    plan = _plan(
        {"task": "compress", "inputs": ["talk.mp4"], "options": {"batch": "--output-dir=/etc"}},
        root,
    )
    args = build_parser().parse_args(plan.command_line(tmp_path / "out"))
    assert args.batch == "--output-dir=/etc"
    assert args.output_dir == tmp_path / "out"


@pytest.mark.parametrize("path", ["../app2/secret.mp4", "/etc/passwd", "books/../../app2"])
def test_inputs_outside_the_callers_directory_are_refused(root, path):
    with pytest.raises(RequestError, match="outside your input directory"):
        _plan({"task": "compress", "inputs": [path]}, root)


def test_a_symlink_out_of_the_callers_directory_is_refused(root, tmp_path):
    (root / "evil.mp4").symlink_to(tmp_path / "in" / "app2" / "secret.mp4")
    with pytest.raises(RequestError, match="outside your input directory"):
        _plan({"task": "compress", "inputs": ["evil.mp4"]}, root)


def test_a_path_typed_option_is_confined_too(root):
    with pytest.raises(RequestError, match="outside your input directory"):
        _plan({"task": "ebook", "command": "build", "options": {"list": "/etc/passwd"}}, root)
    plan = _plan({"task": "ebook", "command": "build", "options": {"list": "list.json"}}, root)
    assert f"--list={root / 'list.json'}" in plan.argv


def test_ebook_kindle_is_never_reachable(root):
    with pytest.raises(RequestError, match="ebook command must be one of"):
        _plan({"task": "ebook", "command": "kindle"}, root)


@pytest.mark.parametrize("task", ["serve", "doctor", "status", "formats", "nope"])
def test_only_work_tasks_are_accepted(root, task):
    with pytest.raises(RequestError, match="task must be one of"):
        _plan({"task": task}, root)


def test_download_inputs_must_be_http_urls(root):
    with pytest.raises(RequestError, match="http"):
        _plan({"task": "download", "inputs": ["file:///etc/passwd"]}, root)
    plan = _plan({"task": "download", "inputs": ["https://example.com/v"]}, root)
    assert plan.positional == ["https://example.com/v"]


def test_workers_are_capped_and_defaulted(root):
    request = {"task": "ebook", "command": "build", "inputs": ["books"]}
    assert "--workers=2" in _plan(request, root, max_workers=2).argv
    request["options"] = {"workers": 16}
    assert "--workers=2" in _plan(request, root, max_workers=2).argv


def test_unknown_request_fields_are_refused(root):
    with pytest.raises(RequestError, match="unknown request fields"):
        _plan({"task": "compress", "inputs": ["talk.mp4"], "argv": ["-o", "/"]}, root)


def test_a_boolean_flag_takes_only_a_boolean(root):
    with pytest.raises(RequestError, match="true or false"):
        _plan({"task": "compress", "inputs": ["talk.mp4"], "options": {"force": "yes"}}, root)


# -- the point-of-use check in core.inputs -----------------------------------------


def test_expand_inputs_refuses_a_source_outside_the_input_root(tmp_path, monkeypatch):
    allowed = tmp_path / "in"
    allowed.mkdir()
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"x")
    (allowed / "link.mp4").symlink_to(outside)
    monkeypatch.setenv(inputs.INPUT_ROOT_ENV, str(allowed))
    with pytest.raises(inputs.InputError, match="outside the permitted input directory"):
        inputs.expand_inputs(
            [allowed / "link.mp4"],
            recursive=False,
            extensions=None,
            accepted={".mp4"},
            output_root=tmp_path / "out",
        )


def test_expand_inputs_is_unchanged_without_the_variable(tmp_path, monkeypatch):
    monkeypatch.delenv(inputs.INPUT_ROOT_ENV, raising=False)
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    sources = inputs.expand_inputs(
        [clip], recursive=False, extensions=None, accepted={".mp4"}, output_root=Path("/nowhere")
    )
    assert [s.path for s in sources] == [clip]
