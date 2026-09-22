"""doctor: is this machine ready to run media-tools. Task 15.

CLI-level tests spawn a real subprocess (as the brief's own tests do) so environment
overrides like MEDIA_TOOLS_FFMPEG take effect; the internal cache/update/pipx-uv logic
is covered separately, in-process, further down so it can be monkeypatched without ever
touching PyPI (no test here may hit the network unless marked)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from media_tools.tasks import doctor as doctor_task


def _cli(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "media_tools", *args],
        capture_output=True,
        text=True,
        env=env,
    )


# -- brief's own tests (verbatim behaviour) ---------------------------------------


def test_doctor_json_reports_required_checks(tmp_path):
    result = _cli("doctor", "--json", "-o", str(tmp_path))
    payload = json.loads(result.stdout)
    names = {check["name"] for check in payload["checks"]}
    assert {"python", "ffmpeg", "yt-dlp", "output-root"} <= names
    assert payload["exit_code"] in (0, 3)


def test_doctor_exit_3_when_a_required_tool_is_missing(tmp_path):
    env = {**os.environ, "MEDIA_TOOLS_FFMPEG": str(tmp_path / "no-such-ffmpeg")}
    result = _cli("doctor", "--json", "-o", str(tmp_path), env=env)
    payload = json.loads(result.stdout)
    assert result.returncode == 3
    assert any(c["name"] == "ffmpeg" and c["status"] == "missing" for c in payload["checks"])


def test_calibre_absence_is_a_warning_not_a_failure(tmp_path):
    payload = json.loads(_cli("doctor", "--json", "-o", str(tmp_path)).stdout)
    calibre = next(c for c in payload["checks"] if c["name"] == "calibre")
    assert calibre["status"] in {"ok", "warn"}


# -- results go to stdout, never a secret in either stream -------------------------


def test_doctor_result_goes_to_stdout_in_json_mode(tmp_path):
    result = _cli("doctor", "--json", "-o", str(tmp_path))
    json.loads(result.stdout)  # a single JSON value on stdout


def test_doctor_json_envelope_has_v_and_type(tmp_path):
    # RULING R27: every query command emits one JSON object with {"v": 1, "type": ...}.
    payload = json.loads(_cli("doctor", "--json", "-o", str(tmp_path)).stdout)
    assert payload["v"] == 1
    assert payload["type"] == "doctor"


def test_doctor_human_table_also_goes_to_stdout(tmp_path):
    result = _cli("doctor", "-o", str(tmp_path))
    assert result.stdout.strip() != ""


def test_doctor_never_prints_the_openrouter_key_value(tmp_path):
    secret = "sk-or-v1-super-secret-value-should-never-leak"
    env = {**os.environ, "OPENROUTER_API_KEY": secret}
    for extra in (["--json"], []):
        result = _cli("doctor", "-o", str(tmp_path), *extra, env=env)
        assert secret not in result.stdout
        assert secret not in result.stderr


def test_doctor_openrouter_key_present_is_ok_or_warn_never_missing(tmp_path):
    env = {**os.environ, "OPENROUTER_API_KEY": "sk-or-v1-whatever"}
    payload = json.loads(_cli("doctor", "--json", "-o", str(tmp_path), env=env).stdout)
    key_check = next(c for c in payload["checks"] if c["name"] == "openrouter-key")
    assert key_check["status"] in {"ok", "warn"}


def test_doctor_quiet_still_prints_a_summary(tmp_path):
    # The session-start hook runs `doctor --quiet --check-updates`; it must never be
    # silent, or an agent watching the hook output has no idea it ran at all.
    result = _cli("doctor", "--quiet", "-o", str(tmp_path))
    assert result.stdout.strip() != ""


def test_doctor_status_values_are_the_declared_enum(tmp_path):
    payload = json.loads(_cli("doctor", "--json", "-o", str(tmp_path)).stdout)
    for check in payload["checks"]:
        assert check["status"] in {"ok", "warn", "missing"}


def test_doctor_exit_0_when_nothing_required_is_missing(tmp_path):
    # On this dev machine ffmpeg/deno/output-root are all real, so a plain run must
    # succeed - exit 3 is reserved for an actually broken environment.
    result = _cli("doctor", "--json", "-o", str(tmp_path))
    payload = json.loads(result.stdout)
    missing = [c for c in payload["checks"] if c["status"] == "missing"]
    assert result.returncode == (3 if missing else 0)


def test_unwritable_output_root_is_reported_missing(tmp_path):
    root = tmp_path / "locked"
    root.mkdir()
    os.chmod(root, 0o500)  # read + execute, no write
    try:
        result = _cli("doctor", "--json", "-o", str(root))
        payload = json.loads(result.stdout)
        out_check = next(c for c in payload["checks"] if c["name"] == "output-root")
        assert out_check["status"] == "missing"
        assert result.returncode == 3
    finally:
        os.chmod(root, 0o700)


# -- in-process: check_all() and its dataclass --------------------------------------


def test_check_all_returns_check_dataclasses(tmp_path):
    checks = doctor_task.check_all(check_updates=False, output_dir=tmp_path)
    assert checks
    for check in checks:
        assert isinstance(check, doctor_task.Check)
        assert check.status in {"ok", "warn", "missing"}


def test_check_all_is_offline_by_default(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not hit the network when check_updates=False")

    monkeypatch.setattr(doctor_task, "_fetch_pypi_version", boom)
    doctor_task.check_all(check_updates=False, output_dir=tmp_path)


# -- --check-updates: cached, never touches the network when the cache is fresh ----


def test_check_updates_uses_a_fresh_cache_without_hitting_the_network(tmp_path, monkeypatch):
    import time

    cache_dir = tmp_path / ".cache"
    cache_dir.mkdir()
    (cache_dir / "updates.json").write_text(
        json.dumps({"checked_at": time.time(), "latest": {}}), encoding="utf-8"
    )

    def boom(*a, **k):
        raise AssertionError("must not hit the network: cache is fresh")

    monkeypatch.setattr(doctor_task, "_fetch_pypi_version", boom)
    doctor_task.check_all(check_updates=True, output_dir=tmp_path)


def test_check_updates_never_compares_media_tools_itself_against_pypi(tmp_path, monkeypatch):
    # media-tools is not published under this name; PyPI already has an unrelated
    # project with the same name, so comparing this checkout against it would be a
    # false "update available" from a name collision, not a real signal.
    calls = []

    def fake_fetch(name, **kwargs):
        calls.append(name)
        return None

    monkeypatch.setattr(doctor_task, "_fetch_pypi_version", fake_fetch)
    doctor_task.check_all(check_updates=True, output_dir=tmp_path)
    assert "media-tools" not in calls


def test_check_updates_refreshes_a_stale_cache(tmp_path, monkeypatch):
    import time

    cache_dir = tmp_path / ".cache"
    cache_dir.mkdir()
    (cache_dir / "updates.json").write_text(
        json.dumps({"checked_at": time.time() - 100_000, "latest": {}}), encoding="utf-8"
    )

    calls = []

    def fake_fetch(name, **kwargs):
        calls.append(name)
        return None

    monkeypatch.setattr(doctor_task, "_fetch_pypi_version", fake_fetch)
    doctor_task.check_all(check_updates=True, output_dir=tmp_path)
    assert calls  # the stale cache was refreshed


# -- --update: refuses under pipx/uv, and never shells out when it does ------------


def test_update_refuses_under_pipx(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor_task.sys, "prefix", str(tmp_path / "pipx" / "venvs" / "media-tools"))

    def boom(*a, **k):
        raise AssertionError("must not run pip under pipx")

    monkeypatch.setattr(doctor_task.subprocess, "run", boom)

    class Args:
        json_mode = False
        quiet = False
        output_dir = None

    code = doctor_task._run_update(Args())
    assert code != 0


def test_update_refuses_under_uv(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor_task.sys, "prefix", str(tmp_path / "uv" / "tools" / "media-tools"))

    def boom(*a, **k):
        raise AssertionError("must not run pip under uv")

    monkeypatch.setattr(doctor_task.subprocess, "run", boom)

    class Args:
        json_mode = False
        quiet = False
        output_dir = None

    code = doctor_task._run_update(Args())
    assert code != 0


def test_update_propagates_quiet_but_not_check_updates_to_the_fresh_recheck(monkeypatch):
    # FIX 4: `doctor --update --quiet` must not suddenly print the full table for the
    # fresh re-check; --check-updates is deliberately never propagated (the upgrade
    # itself already answered whether anything was outdated).
    monkeypatch.setattr(doctor_task, "_install_method", lambda: None)

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(doctor_task.subprocess, "run", fake_run)

    class Args:
        json_mode = False
        quiet = True
        output_dir = None

    code = doctor_task._run_update(Args())
    assert code == 0
    fresh_call = calls[-1]  # the pip install is calls[0]; the fresh re-check is last
    assert "--quiet" in fresh_call
    assert "--check-updates" not in fresh_call


@pytest.mark.network
def test_fetch_pypi_version_real_network():
    version = doctor_task._fetch_pypi_version("yt-dlp")
    assert version is None or isinstance(version, str)
