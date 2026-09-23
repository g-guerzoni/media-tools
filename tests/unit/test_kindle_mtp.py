"""The MTP backend, driven entirely through an injected fake runner, plus the helper
script's own operation layer, driven against a stub device.

No test here spawns Calibre, imports `calibre`, or touches a device. Three seams make
that possible: `MtpBackend`'s `runner` stands in for the whole `calibre-debug`
invocation; the exit-code tests feed realistic stdout/stderr/returncode triples into
the REAL parser so the mapping itself is exercised; and the helper's ops take a
duck-typed device, so a stub exercises every one of them. "Unverified against
hardware" is meant to describe the driver calls, not the logic around them.
"""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from media_tools.integrations.calibre import CalibreError
from media_tools.tasks.ebook.kindle import massstorage, mtp
from media_tools.tasks.ebook.kindle.backend import DeviceFile, DeviceWriteProtected
from media_tools.tasks.ebook.kindle.detect import Device, DeviceBusy, DeviceNotFound

SERIAL = "G000TESTSERIAL"


@pytest.fixture
def device():
    return Device(serial=SERIAL, product_id=0x9981, mode="mtp", mount=None)


class FakeRunner:
    """Stands in for one `calibre-debug` invocation. Records every ops list it was
    handed, so a test can assert both what was sent and how many times."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def __call__(self, ops):
        self.calls.append([dict(op) for op in ops])
        if not self.responses:
            raise AssertionError("the runner was invoked more often than the test allowed")
        response = self.responses.pop(0)
        return response(ops) if callable(response) else response


def results(*entries) -> dict:
    return {"v": 1, "device": {"serial": SERIAL, "checked": True}, "results": list(entries)}


def listing(*files, **extra) -> dict:
    entry = {
        "op": "list",
        "ok": True,
        "files": [{"path": p, "size": s, "mtime": m} for p, s, m in files],
    }
    entry.update(extra)
    return entry


def backend(device, runner, **kwargs):
    kwargs.setdefault("gui_check", lambda: False)
    return mtp.MtpBackend(device, runner=runner, cache_dir=Path("/unused"), **kwargs)


def helper_stdout(payload: dict | None, *, started: bool = True, chatter: bool = True) -> str:
    """What `calibre-debug` actually hands back: its own chatter around the helper's
    two marker lines. The chatter deliberately mentions both markers and prints AFTER
    the result too — the parser must take the last line that STARTS WITH the marker."""
    lines = []
    if chatter:
        lines += [
            "Using calibre-debug from /Applications/calibre.app/Contents/MacOS/calibre-debug",
            "calibre 9.15.0  embedded-python: True",
            f"the helper prints {mtp.RESULT_MARKER} followed by one JSON object",
        ]
    if started:
        lines.append(mtp.START_MARKER)
    if chatter:
        lines += ["MTP device detected, opening session", ""]
    if payload is not None:
        lines.append(mtp.RESULT_MARKER + json.dumps(payload))
    if chatter:
        lines += ["Device session closed", "calibre-debug exiting"]
    return "\n".join(lines) + "\n"


def parsed(payload, stderr="", code=0, **kwargs):
    """A fake runner that pushes a realistic stdout through the real parser."""
    return lambda ops: mtp.parse_helper_output(helper_stdout(payload, **kwargs), stderr, code)


# --- parsing helper output ------------------------------------------------------


def test_list_files_parses_helper_output_into_device_files(device):
    runner = FakeRunner(
        results(
            listing(
                ("documents/en/A Book - An Author.azw3", 1024, 1700000000.0),
                ("documents/pt/Um Livro - Um Autor.azw3", 2048, 1700000001.0),
                ("My Clippings.txt", 12, 1700000002.0),
            )
        )
    )
    files = backend(device, runner).list_files()

    assert files == [
        DeviceFile(path="documents/en/A Book - An Author.azw3", size=1024, mtime=1700000000.0),
        DeviceFile(path="documents/pt/Um Livro - Um Autor.azw3", size=2048, mtime=1700000001.0),
        DeviceFile(path="My Clippings.txt", size=12, mtime=1700000002.0),
    ]
    assert all("\\" not in f.path and not f.path.startswith("/") for f in files)
    assert runner.calls == [[{"op": "list", "path": ""}]]


def test_list_files_filters_the_cached_listing_by_prefix(device):
    runner = FakeRunner(
        results(
            listing(
                ("documents/en/A Book.azw3", 1, 1.0),
                ("documents/pt/Um Livro.azw3", 2, 2.0),
                ("system/thumbnails/cover.jpg", 3, 3.0),
            )
        )
    )
    device_backend = backend(device, runner)
    assert [f.path for f in device_backend.list_files("documents")] == [
        "documents/en/A Book.azw3",
        "documents/pt/Um Livro.azw3",
    ]
    assert [f.path for f in device_backend.list_files("system/thumbnails")] == [
        "system/thumbnails/cover.jpg"
    ]


def test_the_marker_survives_calibre_debug_chatter(device):
    payload = results(listing(("documents/en/A Book.azw3", 7, 1.0)))
    files = backend(device, FakeRunner(parsed(payload))).list_files()
    assert [f.path for f in files] == ["documents/en/A Book.azw3"]


def test_output_without_the_marker_is_a_calibre_error(device):
    runner = FakeRunner(parsed(None))
    with pytest.raises(CalibreError) as error:
        backend(device, runner).list_files()
    assert "marker" in str(error.value).lower()


def test_unparseable_json_after_the_marker_is_a_calibre_error(device):
    stdout = f"{mtp.START_MARKER}\n{mtp.RESULT_MARKER}not json at all\n"
    runner = FakeRunner(lambda ops: mtp.parse_helper_output(stdout, "traceback tail", 0))
    with pytest.raises(CalibreError) as error:
        backend(device, runner).list_files()
    assert "traceback tail" in str(error.value)


def test_a_bare_marker_line_after_the_result_does_not_fail_the_invocation(device):
    """A BARE marker line printed after the payload is the nastiest case: it starts
    with the marker, so a last-line-wins parser would try to read its empty remainder
    as the result and fail an invocation that actually succeeded. The parser keeps
    scanning backwards instead."""
    payload = results(listing(("documents/en/A Book.azw3", 1, 1.0)))
    stdout = (
        f"{mtp.START_MARKER}\n"
        f"{mtp.RESULT_MARKER}{json.dumps(payload)}\n"
        f"a plugin mentions {mtp.RESULT_MARKER} in passing\n"
        f"{mtp.RESULT_MARKER}\n"
    )
    runner = FakeRunner(lambda ops: mtp.parse_helper_output(stdout, "", 0))
    assert len(backend(device, runner).list_files()) == 1


# --- exit-code mapping ----------------------------------------------------------


def test_exit_code_2_raises_device_not_found(device):
    runner = FakeRunner(parsed(None, "no MTP device found\n", 2))
    with pytest.raises(DeviceNotFound):
        backend(device, runner).list_files()


def test_a_non_zero_exit_without_a_start_marker_is_an_invocation_failure(device):
    """A broken `calibre-debug -e ... --` convention must not look like an absent
    Kindle, or the user goes hunting for a cable that is already plugged in."""
    runner = FakeRunner(
        lambda ops: mtp.parse_helper_output("calibre-debug: unknown option\n", "usage: ...", 2)
    )
    with pytest.raises(CalibreError) as error:
        backend(device, runner).list_files()
    message = str(error.value)
    assert "never reached the MTP helper" in message
    assert "calibre-debug" in message


def test_exit_code_3_raises_device_busy_with_the_macos_hint(device, monkeypatch):
    monkeypatch.setattr(mtp.platform, "system", lambda: "Darwin")
    runner = FakeRunner(parsed(None, "device is busy\n", 3))
    with pytest.raises(DeviceBusy) as error:
        backend(device, runner).list_files()
    message = str(error.value)
    assert "ptpcamerad" in message
    assert "OpenMTP" in message
    assert "Android File Transfer" in message
    assert "Send to Kindle" in message


def test_exit_code_3_raises_device_busy_with_the_linux_hint(device, monkeypatch):
    monkeypatch.setattr(mtp.platform, "system", lambda: "Linux")
    runner = FakeRunner(parsed(None, "device is busy\n", 3))
    with pytest.raises(DeviceBusy) as error:
        backend(device, runner).list_files()
    assert "gio mount -u mtp://" in str(error.value)


def test_the_busy_hint_never_offers_to_fix_it_for_the_user(device, monkeypatch):
    monkeypatch.setattr(mtp.platform, "system", lambda: "Darwin")
    runner = FakeRunner(parsed(None, "device is busy\n", 3))
    with pytest.raises(DeviceBusy) as error:
        backend(device, runner).list_files()
    assert "never" in str(error.value).lower()


def test_exit_code_4_raises_device_write_protected(device, tmp_path):
    source = tmp_path / "A Book.azw3"
    source.write_bytes(b"book")
    runner = FakeRunner(parsed(None, "device is read-only\n", 4))
    with pytest.raises(DeviceWriteProtected):
        backend(device, runner).write(source, "documents/en/A Book.azw3")


def test_exit_code_4_attaches_the_writes_that_already_landed(device, tmp_path):
    """The helper emits what it completed before aborting; without attaching it the
    caller cannot tell which books made it onto the device."""
    source = tmp_path / "A Book.azw3"
    source.write_bytes(b"book")
    payload = results(
        {"op": "put", "ok": True, "size": 4, "local_size": 4},
        {"op": "put", "ok": False, "code": "write_protected", "error": "read-only"},
    )
    runner = FakeRunner(parsed(payload, "device is read-only\n", 4))
    with pytest.raises(DeviceWriteProtected) as error:
        backend(device, runner).write(source, "documents/en/A Book.azw3")
    assert [entry["ok"] for entry in error.value.results] == [True, False]


def test_any_other_non_zero_exit_carries_the_stderr_tail(device):
    stderr = "\n".join(f"line {n}" for n in range(30))
    runner = FakeRunner(parsed(None, stderr, 1))
    with pytest.raises(CalibreError) as error:
        backend(device, runner).list_files()
    assert "line 29" in str(error.value)


# --- a failed listing must never become a cached empty device -------------------


def test_a_failed_root_listing_raises_and_is_never_cached(device):
    """The whole point. A transient failure reported as an empty listing would be
    cached for the backend's lifetime: a backup would write nothing and call itself a
    success, and a sync would see an empty device and re-upload the library."""
    runner = FakeRunner(
        results({"op": "list", "ok": False, "code": "list_failed", "error": "usb hiccup"}),
        results(listing(("documents/en/A Book.azw3", 1, 1.0))),
    )
    device_backend = backend(device, runner)
    with pytest.raises(CalibreError):
        device_backend.list_files()
    # Nothing was cached, so the next call really asks the device again.
    assert [f.path for f in device_backend.list_files()] == ["documents/en/A Book.azw3"]
    assert len(runner.calls) == 2


def test_a_partial_listing_raises_rather_than_reporting_a_short_device(device):
    runner = FakeRunner(
        results(
            {
                "op": "list",
                "ok": False,
                "code": "list_partial",
                "partial": True,
                "files": [{"path": "documents/en/A Book.azw3", "size": 1, "mtime": 1.0}],
                "error": "the listing failed part-way through",
            }
        )
    )
    with pytest.raises(CalibreError) as error:
        backend(device, runner).list_files()
    assert "part-way" in str(error.value)


def test_a_listing_flagged_incomplete_is_returned_but_not_cached(device):
    """Belt and braces: even an `ok` listing carrying `partial`/`missing`/`note` must
    not become this backend's idea of the device."""
    runner = FakeRunner(
        results(listing(("documents/en/A Book.azw3", 1, 1.0), note="one folder was unreadable")),
        results(listing(("documents/en/A Book.azw3", 1, 1.0), ("documents/pt/Livro.azw3", 2, 2.0))),
    )
    device_backend = backend(device, runner)
    assert len(device_backend.list_files()) == 1
    assert len(device_backend.list_files()) == 2
    assert len(runner.calls) == 2


def test_a_missing_prefix_still_returns_an_empty_list_cleanly(device):
    runner = FakeRunner(results(listing(missing=True, note="no such folder")))
    assert backend(device, runner).list_files("documents/de") == []


# --- the audible/ and system/ exclusions ----------------------------------------


def test_the_listing_excludes_audible_and_system_internals(device):
    runner = FakeRunner(
        results(
            listing(
                ("My Clippings.txt", 1, 1.0),
                ("audible/Some Audiobook.aax", 2, 2.0),
                ("documents/en/A Book.azw3", 3, 3.0),
                ("documents/audible/nested.aax", 4, 4.0),
                ("system/thumbnails/cover.jpg", 5, 5.0),
                ("system/wifi/wifi.cfg", 6, 6.0),
                ("system/com.amazon.ebook.booklet.reader/reader.log", 7, 7.0),
                (".Trashes/deleted.azw3", 8, 8.0),
                ("documents/._A Book.azw3", 9, 9.0),
            )
        )
    )
    assert [f.path for f in backend(device, runner).list_files()] == [
        "My Clippings.txt",
        "documents/en/A Book.azw3",
        "system/thumbnails/cover.jpg",
    ]


@pytest.mark.parametrize("prefix", ["audible", "system/wifi", "system/logs"])
def test_a_prefix_inside_an_excluded_area_returns_empty_without_asking_the_device(device, prefix):
    runner = FakeRunner()
    assert backend(device, runner).list_files(prefix) == []
    assert runner.calls == []


def test_both_backends_exclude_the_same_things(fake_kindle, device):
    """The exclusion constants live in `massstorage` and `mtp` imports them, so this
    pins that they really do produce the same listing for the same device tree."""
    disk = massstorage.MassStorageBackend(fake_kindle.mount)
    expected = [f.path for f in disk.list_files()]

    # What the MTP helper would hand back for that same tree: everything, unfiltered.
    runner = FakeRunner(
        results(
            listing(
                ("My Clippings.txt", 9, 1.0),
                ("documents/en/A Book - An Author.azw3", 12, 2.0),
                ("documents/pt/Um Livro - Um Autor.azw3", 5, 3.0),
                ("system/thumbnails/cover.jpg", 5, 4.0),
                ("system/wifi/wifi.cfg", 11, 5.0),
            )
        )
    )
    assert [f.path for f in backend(device, runner).list_files()] == expected


def test_the_mtp_filter_reuses_massstorages_own_constants():
    assert mtp.PROTECTED_DIRS is massstorage.PROTECTED_DIRS
    assert mtp.RESTRICTED_PARENT == massstorage.RESTRICTED_PARENT
    assert mtp.RESTRICTED_EXCEPTION == massstorage.RESTRICTED_EXCEPTION


# --- the eight backend methods over run_ops -------------------------------------


def test_write_sends_one_put_op_with_an_explicit_device_path(device, tmp_path):
    source = tmp_path / "A Book - An Author.azw3"
    source.write_bytes(b"book bytes")
    runner = FakeRunner(results({"op": "put", "ok": True, "size": 10, "local_size": 10}))

    backend(device, runner).write(source, "documents/en/A Book - An Author.azw3")

    assert runner.calls == [
        [
            {
                "op": "put",
                "path": "documents/en/A Book - An Author.azw3",
                "local": str(source.resolve()),
            }
        ]
    ]


def test_remove_sends_an_rm_op(device):
    runner = FakeRunner(results({"op": "rm", "ok": True}))
    backend(device, runner).remove("documents/en/A Book.azw3")
    assert runner.calls == [[{"op": "rm", "path": "documents/en/A Book.azw3"}]]


def test_read_sends_a_get_op_with_the_local_destination(device, tmp_path):
    dest = tmp_path / "pulled.azw3"
    runner = FakeRunner(results({"op": "get", "ok": True, "size": 4}))
    backend(device, runner).read("documents/en/A Book.azw3", dest)
    assert runner.calls == [
        [{"op": "get", "path": "documents/en/A Book.azw3", "local": str(dest.resolve())}]
    ]


def test_free_space_and_eject_each_send_their_own_op(device):
    runner = FakeRunner(
        results({"op": "free", "ok": True, "free": 4096}),
        results({"op": "eject", "ok": True}),
    )
    device_backend = backend(device, runner)
    assert device_backend.free_space() == 4096
    device_backend.eject()
    assert [call[0]["op"] for call in runner.calls] == ["free", "eject"]


def test_exists_uses_the_cached_listing_when_there_is_one(device):
    runner = FakeRunner(results(listing(("documents/en/A Book.azw3", 1, 1.0))))
    device_backend = backend(device, runner)
    device_backend.list_files()
    assert device_backend.exists("documents/en/A Book.azw3")
    assert not device_backend.exists("documents/en/Missing.azw3")
    assert len(runner.calls) == 1


def test_exists_lists_only_the_parent_folder_when_the_cache_is_cold(device):
    runner = FakeRunner(results(listing(("documents/en/A Book.azw3", 1, 1.0))))
    assert backend(device, runner).exists("documents/en/A Book.azw3")
    assert runner.calls == [[{"op": "list", "path": "documents/en"}]]


def test_a_failed_op_raises_carrying_the_helpers_message(device):
    runner = FakeRunner(results({"op": "free", "ok": False, "error": "the device went away"}))
    with pytest.raises(CalibreError) as error:
        backend(device, runner).free_space()
    assert "the device went away" in str(error.value)


def test_a_short_results_array_is_a_calibre_error(device):
    runner = FakeRunner(results())
    with pytest.raises(CalibreError):
        backend(device, runner).remove("documents/en/A Book.azw3")


def test_a_malformed_result_entry_is_a_calibre_error(device):
    runner = FakeRunner({"v": 1, "results": ["not an object"]})
    with pytest.raises(CalibreError) as error:
        backend(device, runner).remove("documents/en/A Book.azw3")
    assert "malformed" in str(error.value)


# --- the contract both backends share -------------------------------------------


def test_remove_of_an_absent_path_raises_file_not_found_on_both_backends(fake_kindle, device):
    disk = massstorage.MassStorageBackend(fake_kindle.mount)
    with pytest.raises(FileNotFoundError):
        disk.remove("documents/en/Gone.azw3")

    runner = FakeRunner(
        results({"op": "rm", "ok": False, "code": "not_in_cached_tree", "error": "not there"})
    )
    with pytest.raises(FileNotFoundError):
        backend(device, runner).remove("documents/en/Gone.azw3")


def test_the_rm_gap_is_machine_readable_not_prose(device):
    """Task 8 must be able to tell "this path class cannot be deleted over MTP" from
    "the file is already gone" without matching English text."""
    runner = FakeRunner(
        results(
            {
                "op": "rm",
                "ok": False,
                "code": "not_in_cached_tree",
                "error": "... system/ ... *.sdr ...",
            }
        )
    )
    with pytest.raises(mtp.MtpPathNotInCachedTree):
        backend(device, runner).remove("documents/en/A Book.sdr/page.apnx")
    assert issubclass(mtp.MtpPathNotInCachedTree, FileNotFoundError)


def test_read_of_an_absent_path_raises_file_not_found_on_both_backends(
    fake_kindle, device, tmp_path
):
    disk = massstorage.MassStorageBackend(fake_kindle.mount)
    with pytest.raises(FileNotFoundError):
        disk.read("documents/en/Gone.azw3", tmp_path / "out.azw3")

    runner = FakeRunner(
        results({"op": "get", "ok": False, "code": "not_found", "error": "not on the device"})
    )
    with pytest.raises(FileNotFoundError):
        backend(device, runner).read("documents/en/Gone.azw3", tmp_path / "out.azw3")


def test_exists_answers_for_files_only_on_both_backends(fake_kindle, device):
    disk = massstorage.MassStorageBackend(fake_kindle.mount)
    assert disk.exists("documents/en/A Book - An Author.azw3")
    assert not disk.exists("documents/en"), "a directory is not a file on either backend"

    runner = FakeRunner(results(listing(("documents/en/A Book - An Author.azw3", 1, 1.0))))
    device_backend = backend(device, runner)
    device_backend.list_files()
    assert device_backend.exists("documents/en/A Book - An Author.azw3")
    assert not device_backend.exists("documents/en")


def test_the_mtp_backend_matches_the_mass_storage_backends_shape(device):
    """The Protocol is not runtime-checkable, so pin the nine method names and their
    signatures here."""
    import inspect

    from media_tools.tasks.ebook.kindle.backend import DeviceBackend

    methods = [name for name in vars(DeviceBackend) if not name.startswith("_")]
    assert sorted(methods) == [
        "close",
        "eject",
        "exists",
        "free_space",
        "list_files",
        "read",
        "read_many",
        "remove",
        "write",
    ]
    for name in methods:
        expected = inspect.signature(getattr(massstorage.MassStorageBackend, name))
        assert inspect.signature(getattr(mtp.MtpBackend, name)) == expected, name


# --- batching and cache invalidation --------------------------------------------


def test_several_ops_produce_exactly_one_runner_invocation(device, tmp_path):
    source = tmp_path / "A Book.azw3"
    source.write_bytes(b"x")
    runner = FakeRunner(
        results(
            {"op": "mkdir", "ok": True},
            {"op": "put", "ok": True, "size": 1, "local_size": 1},
            {"op": "rm", "ok": True},
            listing(("documents/en/A Book.azw3", 1, 1.0)),
        )
    )
    outcome = backend(device, runner).run_ops(
        [
            {"op": "mkdir", "path": "documents/en"},
            {"op": "put", "path": "documents/en/A Book.azw3", "local": str(source)},
            {"op": "rm", "path": "documents/en/Old.azw3"},
            {"op": "list", "path": "documents/en"},
        ]
    )

    assert len(runner.calls) == 1
    assert [entry["op"] for entry in runner.calls[0]] == ["mkdir", "put", "rm", "list"]
    assert len(outcome) == 4


def test_run_ops_hands_back_raw_results_for_the_caller_to_check(device):
    """`run_ops` does not raise on a per-op failure — its docstring tells bulk callers
    to check `ok` themselves, and this pins that behaviour so Tasks 4 and 7 can rely
    on it."""
    runner = FakeRunner(results({"op": "rm", "ok": True}, {"op": "rm", "ok": False, "error": "x"}))
    outcome = backend(device, runner).run_ops(
        [{"op": "rm", "path": "a"}, {"op": "rm", "path": "b"}]
    )
    assert [entry["ok"] for entry in outcome] == [True, False]
    assert "check" in mtp.MtpBackend.run_ops.__doc__.lower()


def test_a_second_list_files_hits_the_cache(device):
    runner = FakeRunner(results(listing(("documents/en/A Book.azw3", 1, 1.0))))
    device_backend = backend(device, runner)
    first = device_backend.list_files()
    second = device_backend.list_files()
    assert first == second
    assert len(runner.calls) == 1


def test_a_write_invalidates_the_listing_cache(device, tmp_path):
    source = tmp_path / "New.azw3"
    source.write_bytes(b"new")
    runner = FakeRunner(
        results(listing(("documents/en/A Book.azw3", 1, 1.0))),
        results({"op": "put", "ok": True, "size": 3, "local_size": 3}),
        results(listing(("documents/en/A Book.azw3", 1, 1.0), ("documents/en/New.azw3", 3, 2.0))),
    )
    device_backend = backend(device, runner)
    assert len(device_backend.list_files()) == 1
    device_backend.write(source, "documents/en/New.azw3")
    assert len(device_backend.list_files()) == 2
    assert len(runner.calls) == 3


def test_a_remove_invalidates_the_listing_cache(device):
    runner = FakeRunner(
        results(listing(("documents/en/A Book.azw3", 1, 1.0))),
        results({"op": "rm", "ok": True}),
        results(listing()),
    )
    device_backend = backend(device, runner)
    assert len(device_backend.list_files()) == 1
    device_backend.remove("documents/en/A Book.azw3")
    assert device_backend.list_files() == []
    assert len(runner.calls) == 3


def test_close_clears_the_listing_cache(device):
    runner = FakeRunner(
        results(listing(("documents/en/A Book.azw3", 1, 1.0))),
        results(listing()),
    )
    device_backend = backend(device, runner)
    assert len(device_backend.list_files()) == 1
    device_backend.close()
    assert device_backend.list_files() == []
    assert len(runner.calls) == 2


def test_run_ops_with_no_ops_never_invokes_the_runner(device):
    runner = FakeRunner()
    assert backend(device, runner).run_ops([]) == []
    assert runner.calls == []


# --- the Calibre-GUI preflight --------------------------------------------------


def test_the_calibre_gui_preflight_raises_device_busy_without_invoking_the_runner(device):
    runner = FakeRunner()
    device_backend = backend(device, runner, gui_check=lambda: True)
    with pytest.raises(DeviceBusy) as error:
        device_backend.list_files()
    assert "calibre" in str(error.value).lower()
    assert "close" in str(error.value).lower()
    assert runner.calls == []


def test_the_preflight_lets_ops_through_when_the_gui_is_not_running(device):
    runner = FakeRunner(results(listing()))
    assert backend(device, runner, gui_check=lambda: False).list_files() == []
    assert len(runner.calls) == 1


PS_OUTPUT = """\
/sbin/launchd
/System/Library/CoreServices/ptpcamerad
/Applications/calibre.app/Contents/MacOS/calibre-debug -e /x/kindle_mtp.py -- /x/ops.json
/Applications/calibre.app/Contents/MacOS/calibre-parallel --pipe-worker
/Applications/calibre.app/Contents/MacOS/ebook-convert in.epub out.azw3
"""


def test_the_gui_check_ignores_calibres_other_executables():
    """`calibre-debug` is this backend's own helper and `calibre-parallel` is what
    `ebook-convert` spawns routinely — neither holds the device, and reporting either
    as the GUI would block the user with a DeviceBusy they cannot act on."""
    assert not mtp.gui_is_running_in(PS_OUTPUT)


def test_the_gui_check_spots_the_real_gui():
    running = PS_OUTPUT + "/Applications/calibre.app/Contents/MacOS/calibre --with-library /x\n"
    assert mtp.gui_is_running_in(running)
    assert mtp.gui_is_running_in("/usr/bin/calibre-gui\n")
    assert not mtp.gui_is_running_in("")


# --- CalibreDebugRunner: the real subprocess plumbing, without Calibre ----------


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def stub_calibre_debug(monkeypatch, located):
    """`Dependency` is a frozen dataclass, so the whole object is replaced rather than
    its `locate` field patched."""
    from media_tools.core.engine import Dependency

    monkeypatch.setattr(
        mtp,
        "CALIBRE_DEBUG",
        Dependency(name="calibre-debug", locate=lambda: located, install_hint="install Calibre"),
    )


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    """Patch out only the `subprocess.run` call, so everything else the runner does —
    the envelope on disk, the argv, the env, the temp-file cleanup — is the real code."""
    record = SimpleNamespace(argv=None, env=None, timeout=None, envelope=None, ops_file=None)

    stub_calibre_debug(monkeypatch, "/fake/calibre-debug")

    def fake_run(argv, **kwargs):
        record.argv = argv
        record.env = kwargs.get("env")
        record.timeout = kwargs.get("timeout")
        record.ops_file = Path(argv[-1])
        record.envelope = json.loads(record.ops_file.read_text(encoding="utf-8"))
        return FakeProc(helper_stdout(results(listing(("documents/en/A Book.azw3", 1, 1.0)))))

    monkeypatch.setattr(mtp.subprocess, "run", fake_run)
    return record


def test_the_runner_writes_the_envelope_and_builds_the_argv(spawned, tmp_path):
    runner = mtp.CalibreDebugRunner(tmp_path / "cache", serial=SERIAL)
    payload = runner([{"op": "list", "path": ""}])

    assert payload["results"][0]["files"][0]["path"] == "documents/en/A Book.azw3"
    assert spawned.envelope == {
        "v": mtp.PROTOCOL_VERSION,
        "serial": SERIAL,
        "ops": [{"op": "list", "path": ""}],
    }
    assert spawned.argv[0] == "/fake/calibre-debug"
    assert spawned.argv[1:3] == ["-e", str(mtp._HELPER)]
    assert spawned.argv[3] == "--"
    assert spawned.timeout == mtp.DEFAULT_TIMEOUT


def test_the_runner_isolates_calibres_config_directory(spawned, tmp_path):
    cache = tmp_path / "cache"
    mtp.CalibreDebugRunner(cache)([{"op": "list", "path": ""}])
    assert spawned.env["CALIBRE_CONFIG_DIRECTORY"] == str(cache / "calibre-config")


def test_the_runner_deletes_the_ops_file_afterwards(spawned, tmp_path):
    cache = tmp_path / "cache"
    mtp.CalibreDebugRunner(cache)([{"op": "list", "path": ""}])
    assert not spawned.ops_file.exists()
    assert list(cache.glob("mtp-ops-*.json")) == []


def test_the_runner_deletes_the_ops_file_even_when_the_spawn_fails(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    stub_calibre_debug(monkeypatch, "/fake/calibre-debug")

    def explode(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(mtp.subprocess, "run", explode)
    with pytest.raises(CalibreError) as error:
        mtp.CalibreDebugRunner(cache)([{"op": "list", "path": ""}])
    assert "calibre-debug failed" in str(error.value)
    assert list(cache.glob("mtp-ops-*.json")) == []


def test_the_runner_reports_a_missing_calibre_debug(monkeypatch, tmp_path):
    stub_calibre_debug(monkeypatch, None)
    with pytest.raises(CalibreError) as error:
        mtp.CalibreDebugRunner(tmp_path)([{"op": "free"}])
    assert "calibre-debug not found" in str(error.value)
    assert "install Calibre" in str(error.value)


def test_the_runner_reports_a_missing_helper_script(monkeypatch, tmp_path):
    stub_calibre_debug(monkeypatch, "/fake/calibre-debug")
    monkeypatch.setattr(mtp, "_HELPER", tmp_path / "not-installed" / "kindle_mtp.py")
    with pytest.raises(CalibreError) as error:
        mtp.CalibreDebugRunner(tmp_path)([{"op": "free"}])
    assert "missing from the install" in str(error.value)


def test_the_backend_builds_a_default_runner_carrying_the_devices_serial(device, tmp_path):
    made = mtp.MtpBackend(device, cache_dir=tmp_path)
    assert isinstance(made._runner, mtp.CalibreDebugRunner)
    assert made._runner.serial == SERIAL


# --- the helper script: constants, and its whole op layer against a stub --------


def load_helper():
    """Load `integrations/kindle_mtp.py` by path. It is never imported as part of the
    package (it only runs under `calibre-debug`), so this both pins the constants the
    backend duplicates and proves the module body imports nothing outside the
    standard library — in particular, not `calibre`."""
    path = Path(mtp.__file__).resolve().parents[3] / "integrations" / "kindle_mtp.py"
    spec = importlib.util.spec_from_file_location("_kindle_mtp_helper_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def helper():
    return load_helper()


class StubEntry:
    def __init__(self, name, is_folder, size):
        self.name = name
        self.is_folder = is_folder
        self.size = size
        self.mtime = None


class StubNode:
    """What `find_path` hands back — only the identity matters to `delete_file_or_folder`."""

    def __init__(self, path):
        self.path = path


class StubDevice:
    """A duck-typed stand-in for Calibre's `MTP_DEVICE`, good enough for every op.

    The tree is nested dicts; a `bytes` value is a file. `list_folder_by_name` returns
    entries in REVERSE name order on purpose, so a test can prove the helper sorts.
    `cached_hidden` names paths the CACHED tree does not expose, which is what makes
    `rm`'s real limitation reproducible.

    It raises what the real driver raises, because that is the one distinction
    `_op_list` turns on: a MISSING folder is `FileNotFoundError` and a path that is
    not a folder is `ValueError` (both verified from Calibre 9.15's frozen unix
    driver), while `fail_listing` models a transient libmtp error as a `RuntimeError`
    — which must NOT be mistaken for "the folder is not there".
    """

    _main_id = "main"

    def __init__(
        self,
        tree,
        *,
        free=4096,
        fail_listing=None,
        cached_hidden=(),
        short_write=None,
        fail_fetch=None,
    ):
        self.tree = tree
        self.free = free
        self.fail_listing = fail_listing
        self.cached_hidden = set(cached_hidden)
        self.short_write = short_write
        # A fetch that dies with bytes ALREADY WRITTEN — the only shape in which
        # `_op_get`'s removal of the local file is load-bearing rather than tidy.
        self.fail_fetch = fail_fetch
        self.deleted: list[str] = []
        self.shutdown_called = False
        device = self
        root = StubStorage(device)
        self.filesystem_cache = SimpleNamespace(storage=lambda storage_id: root, entries=[root])

    def node(self, parts):
        node = self.tree
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def list_folder_by_name(self, parent, *names):
        joined = "/".join(names)
        if self.fail_listing is not None and joined == self.fail_listing:
            raise RuntimeError(f"transient MTP failure listing {joined!r}")
        node = self.node(list(names))
        if node is None:
            raise FileNotFoundError(f"Could not find folder named: {joined} in storage")
        if not isinstance(node, dict):
            raise ValueError(f"{joined} is not a folder")
        entries = [
            StubEntry(name, isinstance(value, dict), 0 if isinstance(value, dict) else len(value))
            for name, value in node.items()
        ]
        return sorted(entries, key=lambda entry: entry.name, reverse=True)

    def get_file_by_name(self, outfile, parent, *names):
        joined = "/".join(names)
        node = self.node(list(names))
        if not isinstance(node, bytes):
            raise RuntimeError(f"no such file: {joined!r}")
        if self.fail_fetch == joined:
            outfile.write(node[:3])
            raise RuntimeError(f"the transfer of {joined!r} died part-way")
        outfile.write(node)

    def ensure_parent(self, storage, parts):
        node = self.tree
        walked: list[str] = []
        for part in parts[:-1]:
            walked.append(part)
            node = node.setdefault(part, {})
        return StubFolder(self, node, walked)

    def put_file(self, parent, name, stream, size):
        data = stream.read()
        if self.short_write is not None:
            data = data[: self.short_write]
        parent.node[name] = data

    def delete_file_or_folder(self, target):
        self.deleted.append(target.path)

    def free_space(self):
        return self.free

    def shutdown(self):
        self.shutdown_called = True


class StubFolder:
    def __init__(self, device, node, parts):
        self.device = device
        self.node = node
        self.parts = parts
        self.path = "/".join(parts)


class StubStorage(StubFolder):
    def __init__(self, device):
        super().__init__(device, device.tree, [])

    def find_path(self, parts):
        joined = "/".join(parts)
        if any(joined.startswith(hidden) for hidden in self.device.cached_hidden):
            return None
        return None if self.device.node(list(parts)) is None else StubNode(joined)


@pytest.fixture
def tree():
    return {
        "My Clippings.txt": b"clippings",
        "documents": {
            "en": {
                "A Book.azw3": b"english book",
                "A Book.sdr": {"page.apnx": b"positions"},
            },
            "pt": {"Um Livro.azw3": b"livro"},
        },
        "system": {"thumbnails": {"cover.jpg": b"thumb"}},
    }


def test_the_helper_loads_without_calibre_and_agrees_on_the_protocol(helper):
    assert helper.RESULT_MARKER == mtp.RESULT_MARKER
    assert helper.START_MARKER == mtp.START_MARKER
    assert helper.PROTOCOL_VERSION == mtp.PROTOCOL_VERSION
    assert helper.EXIT_NO_DEVICE == mtp.EXIT_NO_DEVICE
    assert helper.EXIT_BUSY == mtp.EXIT_BUSY
    assert helper.EXIT_WRITE_PROTECTED == mtp.EXIT_WRITE_PROTECTED


def test_the_helper_never_imports_media_tools():
    source = (Path(mtp.__file__).resolve().parents[3] / "integrations" / "kindle_mtp.py").read_text(
        encoding="utf-8"
    )
    assert "import media_tools" not in source
    assert "from media_tools" not in source


def test_the_helper_lists_recursively_in_name_order(helper, tree):
    device = StubDevice(tree)
    result = helper._op_list(device, {"op": "list", "path": ""})
    assert result["ok"] is True
    assert [entry["path"] for entry in result["files"]] == [
        "My Clippings.txt",
        "documents/en/A Book.azw3",
        "documents/en/A Book.sdr/page.apnx",
        "documents/pt/Um Livro.azw3",
        "system/thumbnails/cover.jpg",
    ]
    assert result["files"][0]["size"] == len(b"clippings")


def test_the_helper_lists_from_a_prefix(helper, tree):
    result = helper._op_list(StubDevice(tree), {"op": "list", "path": "documents/pt"})
    assert [entry["path"] for entry in result["files"]] == ["documents/pt/Um Livro.azw3"]


def test_a_missing_prefix_is_an_empty_listing_not_a_failure(helper, tree):
    result = helper._op_list(StubDevice(tree), {"op": "list", "path": "documents/de"})
    assert result["ok"] is True
    assert result["files"] == []
    assert result["missing"] is True


def test_a_failure_at_the_device_root_is_never_an_empty_listing(helper, tree):
    """The device root always exists, so a failure listing it is a failure — reporting
    it as an empty device is what would let a backup write nothing and succeed."""
    device = StubDevice(tree, fail_listing="")
    result = helper._op_list(device, {"op": "list", "path": ""})
    assert result["ok"] is False
    assert result["code"] == "list_failed"
    assert "files" not in result


def test_a_failure_part_way_through_the_walk_is_reported_as_partial(helper, tree):
    device = StubDevice(tree, fail_listing="documents/pt")
    result = helper._op_list(device, {"op": "list", "path": ""})
    assert result["ok"] is False
    assert result["code"] == "list_partial"
    assert result["partial"] is True
    # What it DID collect rides along, but flagged, so nothing treats it as the truth.
    assert any(entry["path"] == "documents/en/A Book.azw3" for entry in result["files"])


def test_the_helper_gets_a_file(helper, tree, tmp_path):
    dest = tmp_path / "out" / "book.azw3"
    result = helper._op_get(
        StubDevice(tree), {"op": "get", "path": "documents/en/A Book.azw3", "local": str(dest)}
    )
    assert result == {"op": "get", "ok": True, "size": len(b"english book")}
    assert dest.read_bytes() == b"english book"


def test_getting_an_absent_file_is_reported_as_not_found(helper, tree, tmp_path):
    dest = tmp_path / "out.azw3"
    result = helper._op_get(
        StubDevice(tree), {"op": "get", "path": "documents/en/Gone.azw3", "local": str(dest)}
    )
    assert result["ok"] is False
    assert result["code"] == "not_found"
    assert not dest.exists(), "the empty local file must not be left behind"


def test_a_fetch_that_dies_part_way_removes_the_half_written_local_file(helper, tree, tmp_path):
    """The file IS on the device, so this is not the `not_found` path: the fetch itself
    died with bytes already on disk. The helper removes them and re-raises, which is
    what lets the mass-storage backend promise the same thing by staging its copies."""
    dest = tmp_path / "out" / "book.azw3"
    device = StubDevice(tree, fail_fetch="documents/en/A Book.azw3")

    with pytest.raises(RuntimeError):
        helper._op_get(
            device, {"op": "get", "path": "documents/en/A Book.azw3", "local": str(dest)}
        )
    assert not dest.exists(), "a truncated local file must never survive a failed fetch"


def test_the_helper_puts_a_file_and_reports_the_size_the_device_gives_back(helper, tree, tmp_path):
    source = tmp_path / "New.azw3"
    source.write_bytes(b"brand new book")
    device = StubDevice(tree)
    result = helper._op_put(
        device, {"op": "put", "path": "documents/en/New.azw3", "local": str(source)}
    )
    assert result["ok"] is True
    assert result["size"] == len(b"brand new book")
    assert result["local_size"] == len(b"brand new book")
    assert device.tree["documents"]["en"]["New.azw3"] == b"brand new book"


def test_a_short_write_reports_the_devices_size_not_the_local_one(helper, tree, tmp_path):
    """Reporting `os.path.getsize(local)` would dress a guess up as a measurement: a
    truncated book would look complete and be indistinguishable from a good one."""
    source = tmp_path / "New.azw3"
    source.write_bytes(b"brand new book")
    device = StubDevice(tree, short_write=4)
    result = helper._op_put(
        device, {"op": "put", "path": "documents/en/New.azw3", "local": str(source)}
    )
    assert result["size"] == 4
    assert result["local_size"] == 14


def test_put_creates_missing_parent_folders(helper, tree, tmp_path):
    source = tmp_path / "New.azw3"
    source.write_bytes(b"x")
    device = StubDevice(tree)
    helper._op_put(device, {"op": "put", "path": "documents/de/Neu.azw3", "local": str(source)})
    assert device.tree["documents"]["de"]["Neu.azw3"] == b"x"


def test_the_helper_removes_a_file(helper, tree):
    device = StubDevice(tree)
    assert helper._op_rm(device, {"op": "rm", "path": "documents/en/A Book.azw3"}) == {
        "op": "rm",
        "ok": True,
    }
    assert device.deleted == ["documents/en/A Book.azw3"]


def test_removing_what_the_cached_tree_hides_is_flagged_machine_readably(helper, tree):
    """Calibre's cached tree omits `*.sdr` and `system/`, and there is no
    delete-by-name primitive — so this case is real, and a caller must be able to
    recognise it without reading prose."""
    device = StubDevice(tree, cached_hidden={"documents/en/A Book.sdr", "system"})
    result = helper._op_rm(device, {"op": "rm", "path": "documents/en/A Book.sdr/page.apnx"})
    assert result["ok"] is False
    assert result["code"] == "not_in_cached_tree"
    assert device.deleted == []


def test_the_helper_makes_a_directory(helper, tree):
    device = StubDevice(tree)
    assert helper._op_mkdir(device, {"op": "mkdir", "path": "documents/de"})["ok"] is True
    assert device.tree["documents"]["de"] == {}
    assert "_" not in device.tree["documents"]["de"], "the sentinel must not be created"


def test_free_space_accepts_a_list_or_a_bare_integer(helper, tree):
    assert helper._op_free(StubDevice(tree, free=4096), {})["free"] == 4096
    assert helper._op_free(StubDevice(tree, free=[8192, 0, 0]), {})["free"] == 8192
    assert helper._op_free(StubDevice(tree, free=[]), {})["free"] == 0


def test_eject_is_a_no_op_because_mtp_has_nothing_to_eject(helper, tree):
    assert helper._op_eject(StubDevice(tree), {}) == {"op": "eject", "ok": True}


def test_the_storage_root_falls_back_when_main_id_is_missing(helper, tree):
    device = StubDevice(tree)
    assert helper._storage(device) is device.filesystem_cache.storage("main")
    del type(device)._main_id
    try:
        assert helper._storage(device) is device.filesystem_cache.entries[0]
    finally:
        type(device)._main_id = "main"


def test_run_ops_keeps_going_past_one_failed_op(helper, tree):
    device = StubDevice(tree, cached_hidden={"system"})
    outcome = helper._run_ops(
        device,
        [
            {"op": "rm", "path": "system/thumbnails/cover.jpg"},
            {"op": "free"},
            {"op": "nonsense"},
        ],
    )
    assert [entry["ok"] for entry in outcome] == [False, True, False]
    assert outcome[0]["code"] == "not_in_cached_tree"
    assert outcome[2]["code"] == "unknown_op"


def test_a_read_only_device_aborts_the_batch_and_keeps_what_landed(helper, tree, tmp_path):
    source = tmp_path / "New.azw3"
    source.write_bytes(b"x")
    device = StubDevice(tree)

    def refuse(parent, name, stream, size):
        raise RuntimeError("the device is mounted read-only")

    device.put_file = refuse
    with pytest.raises(helper._HelperError) as error:
        helper._run_ops(
            device,
            [
                {"op": "free"},
                {"op": "put", "path": "documents/en/New.azw3", "local": str(source)},
                {"op": "put", "path": "documents/en/Other.azw3", "local": str(source)},
            ],
        )
    assert error.value.code == helper.EXIT_WRITE_PROTECTED
    # The ops that already ran are carried on the exception, and the third never ran.
    assert [entry["ok"] for entry in error.value.results] == [True, False]
    assert error.value.results[1]["code"] == "write_protected"


def test_classify_will_not_call_a_read_failure_a_write_protected_device(helper):
    """A book title or a local path containing "read only" must not tell the user
    their Kindle is write-protected."""
    error = RuntimeError("could not fetch 'Read Only Memories.azw3'")
    assert helper._classify(error, is_write=False) == helper.EXIT_FAILED
    assert helper._classify(error, is_write=True) == helper.EXIT_WRITE_PROTECTED
    assert helper._classify(RuntimeError("Device is busy")) == mtp.EXIT_BUSY
    assert helper._classify(RuntimeError("no device attached")) == mtp.EXIT_NO_DEVICE


def test_the_helper_refuses_a_path_that_escapes_the_device_root(helper):
    assert helper._split("/documents/en/") == ["documents", "en"]
    assert helper._split("") == []
    with pytest.raises(ValueError):
        helper._split("documents/../../etc/passwd")


def test_the_helper_rejects_an_ops_file_of_the_wrong_protocol_version(helper, tmp_path):
    ops_file = tmp_path / "ops.json"
    ops_file.write_text(json.dumps({"v": 99, "ops": []}), encoding="utf-8")
    with pytest.raises(helper._HelperError):
        helper._read_ops(str(ops_file))

    ops_file.write_text(json.dumps({"v": 1, "ops": {}}), encoding="utf-8")
    with pytest.raises(helper._HelperError):
        helper._read_ops(str(ops_file))

    ops_file.write_text(
        json.dumps({"v": 1, "serial": SERIAL, "ops": [{"op": "list", "path": ""}]}),
        encoding="utf-8",
    )
    assert helper._read_ops(str(ops_file))["ops"] == [{"op": "list", "path": ""}]


def test_the_serial_check_refuses_a_different_device(helper, tree):
    device = StubDevice(tree)
    device.current_serial_num = "SOMEOTHERDEVICE"
    with pytest.raises(helper._HelperError) as error:
        helper._check_serial(device, SERIAL)
    assert error.value.code == helper.EXIT_NO_DEVICE
    assert "different MTP device" in str(error.value)


def test_the_serial_check_passes_the_matching_device(helper, tree):
    device = StubDevice(tree)
    device.current_serial_num = SERIAL
    assert helper._check_serial(device, SERIAL) == {
        "serial": SERIAL,
        "expected": SERIAL,
        "checked": True,
    }


def test_the_serial_check_says_so_when_the_driver_reports_no_serial(helper, tree):
    device = StubDevice(tree)
    device.current_serial_num = None
    info = helper._check_serial(device, SERIAL)
    assert info["checked"] is False
    assert "could not be verified" in info["note"]


def test_the_serial_check_uses_get_device_uid_as_a_fallback(helper, tree):
    device = StubDevice(tree)
    device.current_serial_num = None
    device.get_device_uid = lambda: SERIAL
    assert helper._check_serial(device, SERIAL)["checked"] is True


def test_main_refuses_the_wrong_argv(helper, capsys):
    assert helper.main([]) == helper.EXIT_FAILED
    assert helper.START_MARKER in capsys.readouterr().out


def test_main_reports_an_unreadable_ops_file(helper, tmp_path, capsys):
    assert helper.main([str(tmp_path / "nope.json")]) == helper.EXIT_FAILED
    captured = capsys.readouterr()
    assert helper.START_MARKER in captured.out
    assert "ops file" in captured.err


def test_the_helpers_own_emitter_round_trips_through_the_backends_parser(helper, capsys):
    """The two halves of the protocol, joined: the helper's real `_emit` writes the
    markers and the JSON, and the backend's real parser reads them back. This is the
    closest thing to an end-to-end test that exists without a device."""
    helper._emit_start()
    print("calibre-debug chatter between the two markers")
    helper._emit(
        [{"op": "list", "ok": True, "files": [{"path": "a.azw3", "size": 1, "mtime": 2.0}]}],
        {"serial": SERIAL, "checked": True},
    )
    print("and some trailing chatter")

    payload = mtp.parse_helper_output(capsys.readouterr().out, "", 0)
    assert payload["results"][0]["files"] == [{"path": "a.azw3", "size": 1, "mtime": 2.0}]
    assert payload["device"]["serial"] == SERIAL


def test_a_whole_batch_round_trips_from_ops_to_device_files(helper, tree, capsys, device):
    """Helper ops against the stub device, emitted by the real emitter, parsed by the
    real parser, and turned into `DeviceFile`s by the real backend."""
    stub = StubDevice(tree)
    helper._emit_start()
    helper._emit(helper._run_ops(stub, [{"op": "list", "path": "documents/pt"}]), {})
    stdout = capsys.readouterr().out

    runner = FakeRunner(lambda ops: mtp.parse_helper_output(stdout, "", 0))
    assert [f.path for f in backend(device, runner).list_files("documents/pt")] == [
        "documents/pt/Um Livro.azw3"
    ]


# --- fix round 2: the exclusions cannot leak through run_ops --------------------


EXCLUSION_LISTING = listing(
    ("My Clippings.txt", 1, 1.0),
    ("audible/Some Audiobook.aax", 2, 2.0),
    ("documents/en/A Book.azw3", 3, 3.0),
    ("system/thumbnails/cover.jpg", 4, 4.0),
    ("system/wifi/wifi.cfg", 5, 5.0),
)
EXPECTED_AFTER_EXCLUSION = [
    "My Clippings.txt",
    "documents/en/A Book.azw3",
    "system/thumbnails/cover.jpg",
]


def test_run_ops_filters_list_results_exactly_as_list_files_does(device):
    """`run_ops` is the entry point bulk callers are pointed at, so the exclusions
    must hold there too — otherwise a batched `list` hands back the Wi-Fi credentials
    and the audiobooks that the filter exists to keep off the host."""
    through_run_ops = backend(device, FakeRunner(results(EXCLUSION_LISTING))).run_ops(
        [{"op": "list", "path": ""}]
    )
    through_list_files = backend(device, FakeRunner(results(EXCLUSION_LISTING))).list_files()

    assert [entry["path"] for entry in through_run_ops[0]["files"]] == EXPECTED_AFTER_EXCLUSION
    assert [f.path for f in through_list_files] == EXPECTED_AFTER_EXCLUSION


def test_run_ops_leaves_non_list_results_alone(device):
    runner = FakeRunner(
        results({"op": "put", "ok": True, "size": 3, "local_size": 3}, {"op": "rm", "ok": True})
    )
    outcome = backend(device, runner).run_ops(
        [{"op": "put", "path": "a", "local": "/b"}, {"op": "rm", "path": "c"}]
    )
    assert outcome == [
        {"op": "put", "ok": True, "size": 3, "local_size": 3},
        {"op": "rm", "ok": True},
    ]


def test_run_ops_keeps_a_list_results_own_flags_while_filtering(device):
    """Filtering must not drop `partial`/`missing`, which is what tells a bulk caller
    the listing is not authoritative."""
    runner = FakeRunner(results(listing(("audible/x.aax", 1, 1.0), partial=True)))
    outcome = backend(device, runner).run_ops([{"op": "list", "path": ""}])
    assert outcome[0]["partial"] is True
    assert outcome[0]["files"] == []


# --- fix round 2: only FileNotFoundError means "that folder is not there" -------


def test_a_transient_failure_at_a_non_root_prefix_is_a_failure_not_an_empty_listing(helper, tree):
    """The lie killed at the root must not survive one level down: a libmtp hiccup
    while listing `documents/pt` is not "that folder is empty"."""
    device = StubDevice(tree, fail_listing="documents/pt")
    result = helper._op_list(device, {"op": "list", "path": "documents/pt"})
    assert result["ok"] is False
    assert result["code"] == "list_failed"
    assert "missing" not in result


def test_a_path_that_is_not_a_folder_is_a_failure_not_a_missing_prefix(helper, tree):
    """The real driver raises ValueError for this, not FileNotFoundError."""
    result = helper._op_list(StubDevice(tree), {"op": "list", "path": "documents/en/A Book.azw3"})
    assert result["ok"] is False
    assert result["code"] == "list_failed"


def test_only_file_not_found_produces_the_missing_verdict(helper, tree):
    missing = helper._op_list(StubDevice(tree), {"op": "list", "path": "documents/de"})
    assert missing["ok"] is True and missing["missing"] is True and missing["files"] == []


# --- fix round 2: exit 2 says which of the three situations it was --------------


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("no_device", "connect one over USB"),
        ("different_device", "serial does not match"),
        ("no_storage", "still locked"),
    ],
)
def test_the_exit_2_message_names_which_situation_it_was(device, reason, expected):
    payload = {"v": 1, "device": {"reason": reason}, "results": []}
    runner = FakeRunner(parsed(payload, "helper said something\n", 2))
    with pytest.raises(DeviceNotFound) as error:
        backend(device, runner).list_files()
    assert expected in str(error.value)


def test_an_exit_2_with_no_reason_names_all_three_possibilities(device):
    runner = FakeRunner(parsed(None, "", 2))
    with pytest.raises(DeviceNotFound) as error:
        backend(device, runner).list_files()
    message = str(error.value)
    assert "none is connected" in message
    assert "not the one detected" in message
    assert "no storage" in message


def test_the_helper_reports_the_reason_for_each_exit_2_situation(helper, tree, capsys):
    """The three reasons really are emitted by the helper, so the backend's mapping is
    fed by something real rather than a shape invented in the test."""
    stub = StubDevice(tree)
    stub.current_serial_num = "SOMEOTHERDEVICE"
    with pytest.raises(helper._HelperError) as mismatch:
        helper._check_serial(stub, SERIAL)
    assert mismatch.value.reason == "different_device"

    empty = StubDevice(tree)
    empty.filesystem_cache = SimpleNamespace(storage=lambda sid: None, entries=[])
    del type(empty)._main_id
    try:
        with pytest.raises(helper._HelperError) as no_storage:
            helper._storage(empty)
        assert no_storage.value.reason == "no_storage"
    finally:
        type(empty)._main_id = "main"

    # And `main` emits the reason even when there are no results to report.
    helper._emit([], {"reason": "different_device"})
    payload = mtp._payload_from(capsys.readouterr().out)
    assert payload["device"]["reason"] == "different_device"


# --- fix round 2: exists() does not quietly pay for a full scan -----------------


def test_exists_on_a_root_level_path_keeps_the_full_listing_it_paid_for(device):
    """A root-level path has the device root as its parent, so the scan is a full one
    either way — it must at least populate the cache instead of being thrown away."""
    runner = FakeRunner(results(listing(("My Clippings.txt", 1, 1.0))))
    device_backend = backend(device, runner)
    assert device_backend.exists("My Clippings.txt")
    assert not device_backend.exists("Other.txt")
    assert [f.path for f in device_backend.list_files()] == ["My Clippings.txt"]
    assert len(runner.calls) == 1, "the full scan was paid for once and kept"
