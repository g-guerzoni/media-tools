"""doctor: is this machine ready to run media-tools. Task 15.

CLI-level tests spawn a real subprocess (as the brief's own tests do) so environment
overrides like MEDIA_TOOLS_FFMPEG take effect; the internal cache/update/pipx-uv logic
is covered separately, in-process, further down so it can be monkeypatched without ever
touching PyPI (no test here may hit the network unless marked)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

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


def test_venv_hint_names_no_particular_minor_version(monkeypatch):
    """doctor's hint, README.md's clone+venv block and .claude/settings.json's hook
    message are one coordinated instruction, and the thing that keeps them agreeing is
    that none of them names a minor version. A pinned one (this used to be python3.13)
    drifts the moment Homebrew moves, and then three files and this test disagree about
    a number none of them needs — the project supports >= 3.11 and the CI matrix is
    what proves it."""
    monkeypatch.setattr(doctor_task.sys, "prefix", "/usr")
    monkeypatch.setattr(doctor_task.sys, "base_prefix", "/usr", raising=False)
    check = doctor_task._venv_check()
    assert check.status == "warn"
    assert "python3 -m venv" in check.hint
    assert not re.search(r"python3\.\d+", check.hint)


def test_the_venv_instruction_names_no_minor_version_anywhere_it_appears():
    # The other two copies of the same instruction. Kept here rather than in
    # test_docs.py because what binds them is doctor's hint, not the formats table.
    #
    # The found-at-least-one assertion is the point: without it, rewording either copy
    # (or moving it to another file) silently reduces this to a loop over nothing that
    # keeps passing while covering nothing at all.
    for path in (Path("README.md"), Path(".claude/settings.json")):
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if "-m venv .venv" in line
        ]
        assert lines, f"{path} no longer carries the venv instruction this test exists to pin"
        for line in lines:
            assert not re.search(r"python3\.\d+", line), f"{path}: {line.strip()}"


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


# -- Task 9: the Kindle section ----------------------------------------------------
#
# Two rules, and the second is not implied by the first. (1) Neither check may ever
# report "missing" — that is the only status that moves `doctor`'s exit code, and a
# machine with no Kindle attached is not a broken machine. (2) Neither may report
# "warn" for a state with nothing to DO about it: the session-start hook runs `doctor
# --quiet`, which prints every non-"ok" row, and a permanent warn nobody can act on
# trains everyone to skim the level that carries the real blockers.
#
# Nothing in this section may spawn a real `calibre-debug` or scan the real USB
# bus/`/Volumes`: those are the two probes the whole gate below exists to avoid, and a
# test that runs them anyway is exactly the hardware dependency `-m "not device"`
# promises is absent.

NO_KINDLE = "nothing attached"


def _no_kindle(**_kwargs):
    raise kindle_detect.DeviceNotFound(NO_KINDLE)


def _mtp_kindle(**_kwargs):
    return kindle_detect.Device(serial=None, product_id=0x9981, mode="mtp", mount=None)


def _patch_kindle(monkeypatch, *, finder=_no_kindle, calibre_debug=None, gui=False):
    """Every Kindle probe replaced: detection, `calibre-debug`'s location, and the
    process-table read behind the Calibre-GUI check. With `calibre_debug=None` the
    driver check finds no tool, so nothing is ever spawned even if the gate below it
    were to break.

    Every `finder` takes `**kwargs`, because `doctor` calls `find_device(identify=False)`
    — a stub with a bare signature would raise `TypeError` into the broad catch and
    turn every one of these checks into "could not tell", silently."""
    monkeypatch.setattr(doctor_task.kindle_detect, "find_device", finder)
    monkeypatch.setattr(doctor_task.calibre, "find_tool", lambda name: calibre_debug)
    monkeypatch.setattr(doctor_task.kindle_mtp, "calibre_gui_is_running", lambda: gui)


def _kindle_checks(tmp_path) -> dict:
    checks = doctor_task.check_all(check_updates=False, output_dir=tmp_path)
    return {c.name: c for c in checks if c.name.startswith("kindle-")}


def test_doctor_reports_a_kindle_section(tmp_path, monkeypatch):
    _patch_kindle(monkeypatch)
    assert set(_kindle_checks(tmp_path)) == {"kindle-mtp-driver", "kindle-device"}


def test_kindle_checks_are_never_failures(tmp_path, monkeypatch):
    for finder in (_no_kindle, _mtp_kindle):
        _patch_kindle(monkeypatch, finder=finder)
        for check in _kindle_checks(tmp_path).values():
            assert check.status in {"ok", "warn"}


def test_kindle_checks_never_move_the_exit_code(tmp_path, monkeypatch):
    # Whatever these two answer, the exit code is decided by the checks that really do
    # block a task. The unhappiest reachable combination: no device, no Calibre.
    _patch_kindle(monkeypatch)
    checks = doctor_task.check_all(check_updates=False, output_dir=tmp_path)
    assert doctor_task._exit_code(checks) == EXIT_OK_CODE


def test_a_machine_with_no_kindle_and_no_calibre_gets_no_kindle_warning(tmp_path, monkeypatch):
    # The rule that "warnings, never failures" did NOT already give us. `doctor
    # --quiet` prints every non-"ok" row at every session start; neither of these two
    # belongs there when there is nothing to act on.
    _patch_kindle(monkeypatch)
    assert [c.status for c in _kindle_checks(tmp_path).values()] == ["ok", "ok"]


def test_the_kindle_check_does_not_enumerate_usb_when_a_mount_answers(monkeypatch, tmp_path):
    """Ruling R55 gated the `calibre-debug` probe on an MTP Kindle actually being
    found; the USB enumeration behind it was not gated at all, and `doctor` runs at
    every session start. It asks only whether a Kindle is attached and in which mode,
    and it never prints a serial — so when a mount already answers both, the bus is a
    question with nothing left to learn from it."""
    mount = tmp_path / "Kindle"
    (mount / "documents").mkdir(parents=True)
    (mount / "system").mkdir()
    usb_calls = []

    def must_not_be_called():
        usb_calls.append(1)
        return []

    monkeypatch.setattr(doctor_task.kindle_detect, "list_usb_devices", must_not_be_called)
    monkeypatch.setattr(doctor_task.kindle_detect, "list_candidate_mounts", lambda: [mount])
    monkeypatch.setattr(doctor_task.calibre, "find_tool", lambda name: None)
    monkeypatch.setattr(doctor_task.kindle_mtp, "calibre_gui_is_running", lambda: False)

    check, device, _skip = doctor_task._kindle_device_check()
    assert usb_calls == []
    assert check.status == "ok"
    assert device is not None and device.mode == "mass_storage"
    # And the serial it never asked for is not in the report either way.
    assert device.serial is None


def test_kindle_device_check_is_ok_when_nothing_is_connected(monkeypatch):
    _patch_kindle(monkeypatch)
    check, device, skip_reason = doctor_task._kindle_device_check()
    assert check.status == "ok"
    assert "no Kindle connected" in check.detail
    assert device is None
    assert skip_reason == "no Kindle connected"
    # The hint survives the demotion: it is what a user who EXPECTED a device needs.
    assert "kindle" in (check.hint or "").lower()


def test_kindle_device_check_reports_the_mode_when_one_is_connected(monkeypatch, tmp_path):
    device = kindle_detect.Device(
        serial="SERIAL-THAT-MUST-NOT-BE-PRINTED",
        product_id=0x0004,
        mode="mass_storage",
        mount=tmp_path,
    )
    _patch_kindle(monkeypatch, finder=lambda **_kwargs: device)
    check, found, skip_reason = doctor_task._kindle_device_check()
    assert check.status == "ok"
    assert "mass storage" in check.detail.lower()
    assert found is device
    assert "mass storage" in skip_reason
    # A serial identifies one physical device and nothing here needs it: `ebook kindle
    # status` is where a user asks for that, deliberately and one command at a time.
    assert "SERIAL-THAT-MUST-NOT-BE-PRINTED" not in check.detail + (check.hint or "")


def test_kindle_device_check_reports_mtp_mode(monkeypatch):
    _patch_kindle(monkeypatch, finder=_mtp_kindle)
    check, device, _ = doctor_task._kindle_device_check()
    assert check.status == "ok"
    assert "mtp" in check.detail.lower()
    assert device is not None and device.mode == "mtp"


def test_kindle_device_check_warns_when_calibres_gui_holds_an_mtp_device(monkeypatch):
    """The real gate, not a stub exception. `detect.find_device` returns a device or
    raises `DeviceNotFound`, full stop — it NEVER raises `DeviceBusy`, which only the
    two backends raise. So this is asked directly, the same way `ebook kindle status`
    asks it. Without this, doctor reports "ok, connected (MTP)" on a machine where
    every `ebook kindle` command fails device_busy/exit 3 at `MtpBackend._preflight`."""
    _patch_kindle(monkeypatch, finder=_mtp_kindle, gui=True)
    check, device, _ = doctor_task._kindle_device_check()
    assert check.status == "warn"
    assert "calibre" in check.detail.lower()
    assert "device_busy" in (check.hint or "")
    # Still returned: whether the driver IMPORTS is a separate question from who holds
    # the device, and the probe never opens one.
    assert device is not None and device.mode == "mtp"


def test_a_held_mtp_device_still_gets_its_driver_probed(monkeypatch, tmp_path):
    _patch_kindle(monkeypatch, finder=_mtp_kindle, gui=True)
    checks = _kindle_checks(tmp_path)
    assert checks["kindle-device"].status == "warn"
    # calibre-debug is absent under _patch_kindle's default, so the probe ran and found
    # no tool — which is a warn, not the "not probed" skip.
    assert "not probed" not in checks["kindle-mtp-driver"].detail


def test_the_gui_check_is_not_consulted_for_a_mass_storage_kindle(monkeypatch, tmp_path):
    """Mass storage has no single-holder lock, which is why `run_status` reports
    `held_by` only for MTP. Doctor must not invent one."""

    def boom():
        raise AssertionError("the GUI check must not run for a mass-storage Kindle")

    device = kindle_detect.Device(
        serial=None, product_id=0x0004, mode="mass_storage", mount=tmp_path
    )
    monkeypatch.setattr(doctor_task.kindle_detect, "find_device", lambda **_kwargs: device)
    monkeypatch.setattr(doctor_task.kindle_mtp, "calibre_gui_is_running", boom)
    check, _, _ = doctor_task._kindle_device_check()
    assert check.status == "ok"


def test_the_gui_check_never_raises(monkeypatch):
    def boom():
        raise OSError("no process table today")

    _patch_kindle(monkeypatch, finder=_mtp_kindle)
    monkeypatch.setattr(doctor_task.kindle_mtp, "calibre_gui_is_running", boom)
    check, device, _ = doctor_task._kindle_device_check()
    assert check.status == "ok"
    assert device is not None


def test_kindle_device_check_never_raises(monkeypatch):
    # Detection shells out to `ioreg`/reads /sys. Whatever goes wrong there, `doctor`
    # answers "could not tell" rather than dying halfway through its own report.
    def boom():
        raise ValueError("ioreg said something unparseable")

    _patch_kindle(monkeypatch, finder=boom)
    check, device, skip_reason = doctor_task._kindle_device_check()
    assert check.status == "warn"
    assert device is None
    # NOT "no MTP Kindle connected": detection failed, so nothing is known about what
    # is attached, and claiming otherwise on an "ok" row is a false statement.
    assert skip_reason == "no device detected"


# -- the driver probe, and the gate that keeps it off the session-start path -------


def test_kindle_mtp_driver_check_is_skipped_without_an_mtp_device(monkeypatch, tmp_path):
    # The gate: no subprocess at all, and no warn, for the two cases that do not need
    # the driver. `find_tool` is made to explode rather than return None, so a broken
    # gate fails loudly instead of quietly looking like the "not found" branch.
    def boom(name):
        raise AssertionError("the driver probe must not be reached without an MTP device")

    monkeypatch.setattr(doctor_task.calibre, "find_tool", boom)
    mass_storage = kindle_detect.Device(
        serial=None, product_id=0x0004, mode="mass_storage", mount=tmp_path
    )
    for device in (None, mass_storage):
        check = doctor_task._kindle_mtp_driver_check(device, "some reason")
        assert check.status == "ok"
        assert check.detail == "not probed: some reason"


def test_the_skipped_driver_check_says_which_of_three_reasons_applies(monkeypatch, tmp_path):
    """An "ok" row must not assert something unknown. "no MTP Kindle connected" is
    false when detection itself failed — nothing is known then about what is attached."""

    def boom(**_kwargs):
        raise ValueError("ioreg said something unparseable")

    mass_storage = kindle_detect.Device(
        serial=None, product_id=0x0004, mode="mass_storage", mount=tmp_path
    )
    cases = {
        _no_kindle: "not probed: no Kindle connected",
        boom: "not probed: no device detected",
        (lambda **_kwargs: mass_storage): "not probed: the connected Kindle is mass storage, "
        "which needs no MTP driver",
    }
    for finder, expected in cases.items():
        _patch_kindle(monkeypatch, finder=finder)
        assert _kindle_checks(tmp_path)["kindle-mtp-driver"].detail == expected


def test_kindle_mtp_driver_check_warns_when_calibre_debug_is_absent(monkeypatch):
    monkeypatch.setattr(doctor_task.calibre, "find_tool", lambda name: None)
    check = doctor_task._kindle_mtp_driver_check(_mtp_kindle())
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
    check = doctor_task._kindle_mtp_driver_check(_mtp_kindle())
    assert check.status == "ok"


def test_kindle_mtp_driver_check_warns_when_the_import_fails(monkeypatch):
    monkeypatch.setattr(doctor_task.calibre, "find_tool", lambda name: "/fake/calibre-debug")

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="ModuleNotFoundError: calibre.devices.mtp.driver"
        )

    monkeypatch.setattr(doctor_task.subprocess, "run", fake_run)
    check = doctor_task._kindle_mtp_driver_check(_mtp_kindle())
    assert check.status == "warn"


def test_kindle_mtp_driver_check_warns_when_the_probe_cannot_run(monkeypatch):
    monkeypatch.setattr(doctor_task.calibre, "find_tool", lambda name: "/fake/calibre-debug")

    def fake_run(argv, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(doctor_task.subprocess, "run", fake_run)
    check = doctor_task._kindle_mtp_driver_check(_mtp_kindle())
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
    assert doctor_task._kindle_mtp_driver_check(_mtp_kindle()).status == "warn"
