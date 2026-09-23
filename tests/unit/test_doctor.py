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

from media_tools.core.events import EXIT_OK as EXIT_OK_CODE
from media_tools.tasks import doctor as doctor_task
from media_tools.tasks.ebook.kindle import detect as kindle_detect


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


def test_venv_hint_matches_the_readme_python_version(monkeypatch):
    # README.md and .claude/settings.json both tell a human/agent to use python3.13;
    # doctor's own hint must not disagree and say python3.11 (the *minimum* supported,
    # not what anyone is told to actually install).
    monkeypatch.setattr(doctor_task.sys, "prefix", "/usr")
    monkeypatch.setattr(doctor_task.sys, "base_prefix", "/usr", raising=False)
    check = doctor_task._venv_check()
    assert check.status == "warn"
    assert "python3.13" in check.hint
    assert "python3.11" not in check.hint


# -- deno's "found but did not run" branch must hint like its ffmpeg twin ----------


def test_deno_found_but_not_running_carries_a_hint(monkeypatch):
    monkeypatch.setattr(doctor_task, "_deno_exe", lambda: "/usr/local/bin/deno")
    monkeypatch.setattr(doctor_task, "_run_version", lambda argv, **kwargs: (False, ""))
    check = doctor_task._deno_check()
    assert check.status == "missing"
    assert check.hint


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


# -- Task 12: Calibre's tools and the OpenRouter key are optional, ebook-flavoured ---


def test_doctor_reports_calibre_and_the_key_as_optional(tmp_path):
    payload = json.loads(_cli("doctor", "--json", "-o", str(tmp_path)).stdout)
    names = {c["name"]: c for c in payload["checks"]}
    assert "calibre" in names and "openrouter-key" in names
    for name in ("calibre", "openrouter-key"):
        assert names[name]["status"] in {"ok", "warn"}
        assert (
            "ebook" in names[name]["detail"].lower()
            or "ebook" in (names[name].get("hint") or "").lower()
        )


def test_doctor_never_prints_the_key(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "media_tools", "doctor", "--json", "-o", str(tmp_path)],
        capture_output=True,
        text=True,
        env={**os.environ, "OPENROUTER_API_KEY": "sk-do-not-print-me"},
    )
    assert "sk-do-not-print-me" not in result.stdout + result.stderr


# -- RULING RB2: doctor must ask integrations.openrouter.key_present(...), not read
# OPENROUTER_API_KEY itself - otherwise an op:// reference or a --op-item resolves for
# the ebook task but doctor reports the key "missing" anyway. -----------------------


def test_openrouter_check_resolves_an_op_style_reference():
    def fake_runner(argv):
        assert argv[:2] == ["op", "read"]
        return "resolved-secret-value"

    env = {"OPENROUTER_API_KEY": "op://vault/item/field"}
    check = doctor_task._openrouter_check(env=env, runner=fake_runner)
    assert check.status == "ok"
    assert "resolved-secret-value" not in check.detail
    assert "resolved-secret-value" not in (check.hint or "")


def test_openrouter_check_honours_op_item():
    def fake_runner(argv):
        return "resolved-secret-value" if argv[:2] == ["op", "item"] else ""

    check = doctor_task._openrouter_check(op_item="my-item", env={}, runner=fake_runner)
    assert check.status == "ok"
    assert "resolved-secret-value" not in check.detail
    assert "resolved-secret-value" not in (check.hint or "")


def test_openrouter_check_warns_when_nothing_resolves():
    check = doctor_task._openrouter_check(env={}, runner=lambda argv: "")
    assert check.status == "warn"
    assert "ebook" in check.detail.lower() or "ebook" in (check.hint or "").lower()


def test_openrouter_check_uses_a_short_timeout_for_the_op_item_lookup(monkeypatch):
    # Minor finding: `doctor` is a quick health check, not a real key resolution —
    # a named --op-item that never resolves must not be able to make it hang for
    # anywhere near resolve_key's own ~3-minute worst case.
    captured = {}

    def fake_key_present(op_item=None, *, env=None, runner=None, timeout=30):
        captured["timeout"] = timeout
        return False

    monkeypatch.setattr(doctor_task.openrouter, "key_present", fake_key_present)
    doctor_task._openrouter_check(op_item="whatever", env={})
    assert captured["timeout"] == doctor_task._OP_ITEM_LOOKUP_TIMEOUT_S
    assert captured["timeout"] < 30


# -- I6: doctor must agree with calibre.find_tool, not just shutil.which ------------


def test_calibre_check_uses_find_tool_not_just_shutil_which(tmp_path, monkeypatch):
    """A .dmg/App-bundle Calibre install (found via `calibre.find_tool`'s extra
    search dirs, e.g. /Applications/calibre.app/Contents/MacOS on macOS) must not
    make doctor warn "not found" while `ebook build`/`convert` — which already go
    through `find_tool` — work just fine. Same defect class RB2 already fixed for
    the OpenRouter key check."""
    from media_tools.integrations import calibre as calibre_mod

    fake_dir = tmp_path / "calibre-app"
    fake_dir.mkdir()
    for name in doctor_task.CALIBRE_TOOLS:
        script = fake_dir / name
        script.write_text("#!/bin/sh\necho fake 1.0\n")
        script.chmod(0o755)

    monkeypatch.setattr(calibre_mod, "_EXTRA_DIRS", (fake_dir,))
    monkeypatch.setenv("PATH", "/nonexistent")  # shutil.which alone must find nothing

    check = doctor_task._calibre_check()
    assert check.status == "ok"


# -- Task 9: the Kindle section. Warnings, never failures --------------------------
#
# A machine with no Kindle attached is not a broken machine, and `compress`/`convert`/
# `split`/`download`/`ebook build` never touch one — so neither of these two checks may
# ever report "missing", which is the only status that moves `doctor`'s exit code.


def test_doctor_reports_a_kindle_section(tmp_path):
    payload = json.loads(_cli("doctor", "--json", "-o", str(tmp_path)).stdout)
    names = {check["name"] for check in payload["checks"]}
    assert {"kindle-mtp-driver", "kindle-device"} <= names


def test_kindle_checks_are_warnings_never_failures(tmp_path):
    payload = json.loads(_cli("doctor", "--json", "-o", str(tmp_path)).stdout)
    for check in payload["checks"]:
        if check["name"].startswith("kindle-"):
            assert check["status"] in {"ok", "warn"}


def test_kindle_checks_never_move_the_exit_code(tmp_path, monkeypatch):
    # The whole point of "warnings, never failures": whatever these two answer, the
    # exit code is decided by the checks that really do block a task.
    def no_kindle():
        raise kindle_detect.DeviceNotFound("nothing attached")

    monkeypatch.setattr(doctor_task.kindle_detect, "find_device", no_kindle)
    monkeypatch.setattr(doctor_task.calibre, "find_tool", lambda name: None)
    checks = doctor_task.check_all(check_updates=False, output_dir=tmp_path)
    kindle_checks = [c for c in checks if c.name.startswith("kindle-")]
    assert len(kindle_checks) == 2
    assert all(c.status == "warn" for c in kindle_checks)
    assert doctor_task._exit_code(checks) == EXIT_OK_CODE


def test_kindle_device_check_warns_when_nothing_is_connected(monkeypatch):
    def no_kindle():
        raise kindle_detect.DeviceNotFound("nothing attached")

    monkeypatch.setattr(doctor_task.kindle_detect, "find_device", no_kindle)
    check = doctor_task._kindle_device_check()
    assert check.status == "warn"
    assert "kindle" in (check.hint or "").lower()


def test_kindle_device_check_reports_the_mode_when_one_is_connected(monkeypatch, tmp_path):
    device = kindle_detect.Device(
        serial="SERIAL-THAT-MUST-NOT-BE-PRINTED",
        product_id=0x0004,
        mode="mass_storage",
        mount=tmp_path,
    )
    monkeypatch.setattr(doctor_task.kindle_detect, "find_device", lambda: device)
    check = doctor_task._kindle_device_check()
    assert check.status == "ok"
    assert "mass storage" in check.detail.lower()
    # A serial identifies one physical device and nothing here needs it: `ebook kindle
    # status` is where a user asks for that, deliberately and one command at a time.
    assert "SERIAL-THAT-MUST-NOT-BE-PRINTED" not in check.detail + (check.hint or "")


def test_kindle_device_check_reports_mtp_mode(monkeypatch):
    device = kindle_detect.Device(serial=None, product_id=0x9981, mode="mtp", mount=None)
    monkeypatch.setattr(doctor_task.kindle_detect, "find_device", lambda: device)
    check = doctor_task._kindle_device_check()
    assert check.status == "ok"
    assert "mtp" in check.detail.lower()


def test_kindle_device_check_warns_when_the_device_is_held(monkeypatch):
    def busy():
        raise kindle_detect.DeviceBusy("Calibre has it")

    monkeypatch.setattr(doctor_task.kindle_detect, "find_device", busy)
    check = doctor_task._kindle_device_check()
    assert check.status == "warn"


def test_kindle_device_check_never_raises(monkeypatch):
    # Detection shells out to `ioreg`/reads /sys. Whatever goes wrong there, `doctor`
    # answers "could not tell" rather than dying halfway through its own report.
    def boom():
        raise ValueError("ioreg said something unparseable")

    monkeypatch.setattr(doctor_task.kindle_detect, "find_device", boom)
    check = doctor_task._kindle_device_check()
    assert check.status == "warn"


def test_kindle_mtp_driver_check_warns_when_calibre_debug_is_absent(monkeypatch):
    monkeypatch.setattr(doctor_task.calibre, "find_tool", lambda name: None)
    check = doctor_task._kindle_mtp_driver_check()
    assert check.status == "warn"
    assert check.hint


def test_kindle_mtp_driver_check_is_ok_when_the_probe_prints_its_marker(monkeypatch):
    monkeypatch.setattr(doctor_task.calibre, "find_tool", lambda name: "/fake/calibre-debug")

    def fake_run(argv, **kwargs):
        assert argv[0] == "/fake/calibre-debug"
        assert doctor_task._MTP_PROBE_MARKER in argv[-1]
        return subprocess.CompletedProcess(
            argv, 0, stdout=f"chatter\n{doctor_task._MTP_PROBE_MARKER}\n", stderr=""
        )

    monkeypatch.setattr(doctor_task.subprocess, "run", fake_run)
    check = doctor_task._kindle_mtp_driver_check()
    assert check.status == "ok"


def test_kindle_mtp_driver_check_warns_when_the_import_fails(monkeypatch):
    monkeypatch.setattr(doctor_task.calibre, "find_tool", lambda name: "/fake/calibre-debug")

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="ModuleNotFoundError: calibre.devices.mtp.driver"
        )

    monkeypatch.setattr(doctor_task.subprocess, "run", fake_run)
    check = doctor_task._kindle_mtp_driver_check()
    assert check.status == "warn"


def test_kindle_mtp_driver_check_warns_when_the_probe_cannot_run(monkeypatch):
    monkeypatch.setattr(doctor_task.calibre, "find_tool", lambda name: "/fake/calibre-debug")

    def fake_run(argv, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(doctor_task.subprocess, "run", fake_run)
    check = doctor_task._kindle_mtp_driver_check()
    assert check.status == "warn"


def test_kindle_mtp_driver_probe_marker_never_matches_by_accident(monkeypatch):
    # Exit 0 alone is not enough: `calibre-debug` exits 0 for plenty of things that
    # never imported the driver, so the marker the probe prints is the real signal.
    monkeypatch.setattr(doctor_task.calibre, "find_tool", lambda name: "/fake/calibre-debug")
    monkeypatch.setattr(
        doctor_task.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
    )
    assert doctor_task._kindle_mtp_driver_check().status == "warn"
