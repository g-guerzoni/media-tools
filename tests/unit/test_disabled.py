import json
import os
import subprocess
import sys
from argparse import Namespace

import pytest

from media_tools.cli import main
from media_tools.core import disabled
from media_tools.tasks import doctor

# Named for what it is: if the run got as far as yt-dlp, the test would need the
# network. Being refused first is the point.
URL = "https://example.com/video"


def _run(args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k != disabled.ENV_VAR}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "media_tools", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _events(stdout):
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


@pytest.fixture
def baked(tmp_path, monkeypatch):
    """A baked disabled-tasks file, as the prod image carries."""
    path = tmp_path / "disabled-tasks"
    monkeypatch.setattr(disabled, "BAKED_PATH", path)
    monkeypatch.delenv(disabled.ENV_VAR, raising=False)
    return path


# -- the set itself ----------------------------------------------------------------


def test_nothing_disabled_by_default(tmp_path):
    loaded = disabled.load(env={}, path=tmp_path / "absent")
    assert loaded.ids == frozenset()
    assert loaded.unreadable is None


def test_env_and_file_are_unioned(tmp_path):
    path = tmp_path / "disabled-tasks"
    path.write_text("download\n# a comment\n\nebook-kindle\n")
    loaded = disabled.load(env={disabled.ENV_VAR: "split, compress"}, path=path)
    assert loaded.ids == {"download", "ebook-kindle", "split", "compress"}
    assert loaded.baked == {"download", "ebook-kindle"}


@pytest.mark.parametrize("value", ["", " ", ",", "compress", "download", "-download"])
def test_the_environment_can_never_narrow_the_baked_set(tmp_path, value):
    path = tmp_path / "disabled-tasks"
    path.write_text("download\nebook-kindle\n")
    loaded = disabled.load(env={disabled.ENV_VAR: value}, path=path)
    assert {"download", "ebook-kindle"} <= loaded.ids


def test_unknown_ids_are_reported_and_disable_nothing(tmp_path):
    loaded = disabled.load(env={disabled.ENV_VAR: "downlaod"}, path=tmp_path / "absent")
    assert loaded.ids == frozenset()
    assert loaded.unknown == {"downlaod"}


def test_an_unreadable_baked_file_fails_closed(tmp_path):
    path = tmp_path / "disabled-tasks"
    path.mkdir()  # reading a directory raises an OSError that is not FileNotFoundError
    loaded = disabled.load(env={}, path=path)
    assert loaded.unreadable
    assert disabled.refusal(Namespace(task="compress"), loaded)
    for report in disabled.ALWAYS_ALLOWED:
        assert disabled.refusal(Namespace(task=report), loaded) is None


def test_disabling_ebook_also_disables_kindle(tmp_path):
    loaded = disabled.load(env={disabled.ENV_VAR: "ebook"}, path=tmp_path / "absent")
    args = Namespace(task="ebook", ebook_command="kindle")
    assert disabled.refusal(args, loaded)


def test_disabling_kindle_leaves_the_rest_of_ebook_alone(tmp_path):
    loaded = disabled.load(env={disabled.ENV_VAR: "ebook-kindle"}, path=tmp_path / "absent")
    assert disabled.refusal(Namespace(task="ebook", ebook_command="kindle"), loaded)
    assert disabled.refusal(Namespace(task="ebook", ebook_command="build"), loaded) is None


# -- the CLI refuses before any work -----------------------------------------------


def test_a_disabled_task_exits_2_with_error_then_result_and_writes_nothing(tmp_path):
    out = tmp_path / "out"
    result = _run(
        ["download", URL, "--json", "-o", str(out)], env_extra={disabled.ENV_VAR: "download"}
    )
    assert result.returncode == 2
    events = _events(result.stdout)
    assert [e["type"] for e in events] == ["error", "result"]
    assert events[0]["code"] == "usage"
    assert "disabled" in events[0]["message"]
    assert events[-1]["exit_code"] == 2
    assert not out.exists()


def test_a_baked_disable_survives_an_empty_environment_variable(
    baked, tmp_path, capsys, monkeypatch
):
    baked.write_text("download\n")
    monkeypatch.setenv(disabled.ENV_VAR, "")
    code = main(["download", URL, "--json", "-o", str(tmp_path / "out")])
    assert code == 2
    events = _events(capsys.readouterr().out)
    assert "by this image" in events[0]["message"]


def test_an_unreadable_baked_file_refuses_work_as_config_missing(baked, tmp_path, capsys):
    baked.mkdir()
    code = main(["compress", str(tmp_path / "x.mp4"), "--json", "-o", str(tmp_path / "out")])
    assert code == 3
    events = _events(capsys.readouterr().out)
    assert events[0]["code"] == "config_missing"
    assert events[-1]["type"] == "result"


def test_an_unset_variable_changes_nothing(tmp_path):
    # With nothing disabled, the same bad input reaches the task's own validation.
    result = _run(["compress", str(tmp_path / "missing.mp4"), "--json"])
    events = _events(result.stdout)
    assert "disabled" not in events[0]["message"]


# -- reported where a caller can see it --------------------------------------------


def test_formats_json_lists_disabled_tasks(baked, capsys):
    baked.write_text("download\nebook-kindle\n")
    assert main(["formats", "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["disabled_tasks"] == ["download", "ebook-kindle"]


def test_formats_markdown_is_unchanged_by_a_deployment(baked, capsys):
    main(["formats", "--markdown"])
    plain = capsys.readouterr().out
    baked.write_text("download\n")
    main(["formats", "--markdown"])
    assert capsys.readouterr().out == plain


def test_doctor_reports_the_sources_and_stays_ok(tmp_path):
    path = tmp_path / "disabled-tasks"
    path.write_text("download\n")
    check = doctor._disabled_tasks_check(env={disabled.ENV_VAR: "split"}, path=path)
    assert check.status == "ok"
    assert "by the image: download" in check.detail
    assert f"by {disabled.ENV_VAR}: split" in check.detail


def test_doctor_warns_on_a_typo(tmp_path):
    check = doctor._disabled_tasks_check(
        env={disabled.ENV_VAR: "downlaod"}, path=tmp_path / "absent"
    )
    assert check.status == "warn"
    assert "downlaod" in check.detail
