"""The MTP backend, driven entirely through an injected fake runner.

No test here spawns Calibre, imports `calibre`, or touches a device: `MtpBackend`'s
`runner` seam stands in for the whole `calibre-debug` invocation, and the exit-code
tests feed realistic stdout/stderr/returncode triples into the real parser so the
mapping itself is exercised rather than mocked away.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from media_tools.integrations.calibre import CalibreError
from media_tools.tasks.ebook.kindle import mtp
from media_tools.tasks.ebook.kindle.backend import DeviceFile, DeviceWriteProtected
from media_tools.tasks.ebook.kindle.detect import Device, DeviceBusy, DeviceNotFound


@pytest.fixture
def device():
    return Device(serial="G000TESTSERIAL", product_id=0x9981, mode="mtp", mount=None)


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
    return {"v": 1, "results": list(entries)}


def listing(*files) -> dict:
    return {
        "op": "list",
        "ok": True,
        "files": [{"path": p, "size": s, "mtime": m} for p, s, m in files],
    }


def backend(device, runner, **kwargs):
    kwargs.setdefault("gui_check", lambda: False)
    return mtp.MtpBackend(device, runner=runner, cache_dir=Path("/unused"), **kwargs)


def noisy_stdout(payload: dict) -> str:
    """What `calibre-debug` actually hands back: its own chatter, then the marker,
    then the JSON. The chatter deliberately mentions the marker too — the parser must
    use the LAST occurrence, not the first."""
    return "\n".join(
        [
            "Using calibre-debug from /Applications/calibre.app/Contents/MacOS/calibre-debug",
            "calibre 9.15.0  embedded-python: True",
            "MTP device detected, opening session",
            f"helper will print {mtp.RESULT_MARKER} followed by one JSON object",
            "",
            mtp.RESULT_MARKER,
            json.dumps(payload),
            "Device session closed",
        ]
    )


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
    runner = FakeRunner(lambda ops: mtp.parse_helper_output(noisy_stdout(payload), "", 0))

    files = backend(device, runner).list_files()
    assert [f.path for f in files] == ["documents/en/A Book.azw3"]


def test_output_without_the_marker_is_a_calibre_error(device):
    runner = FakeRunner(
        lambda ops: mtp.parse_helper_output("calibre 9.15.0\nsomething went sideways\n", "", 0)
    )
    with pytest.raises(CalibreError) as error:
        backend(device, runner).list_files()
    assert "marker" in str(error.value).lower()


def test_unparseable_json_after_the_marker_is_a_calibre_error(device):
    stdout = f"chatter\n{mtp.RESULT_MARKER}\nnot json at all\n"
    runner = FakeRunner(lambda ops: mtp.parse_helper_output(stdout, "traceback tail", 0))
    with pytest.raises(CalibreError) as error:
        backend(device, runner).list_files()
    assert "traceback tail" in str(error.value)


# --- exit-code mapping ----------------------------------------------------------


def test_exit_code_2_raises_device_not_found(device):
    runner = FakeRunner(
        lambda ops: mtp.parse_helper_output("chatter\n", "no MTP device found\n", 2)
    )
    with pytest.raises(DeviceNotFound):
        backend(device, runner).list_files()


def test_exit_code_3_raises_device_busy_with_the_macos_hint(device, monkeypatch):
    monkeypatch.setattr(mtp.platform, "system", lambda: "Darwin")
    runner = FakeRunner(lambda ops: mtp.parse_helper_output("", "device is busy\n", 3))
    with pytest.raises(DeviceBusy) as error:
        backend(device, runner).list_files()
    message = str(error.value)
    assert "ptpcamerad" in message
    assert "OpenMTP" in message
    assert "Android File Transfer" in message
    assert "Send to Kindle" in message


def test_exit_code_3_raises_device_busy_with_the_linux_hint(device, monkeypatch):
    monkeypatch.setattr(mtp.platform, "system", lambda: "Linux")
    runner = FakeRunner(lambda ops: mtp.parse_helper_output("", "device is busy\n", 3))
    with pytest.raises(DeviceBusy) as error:
        backend(device, runner).list_files()
    assert "gio mount -u mtp://" in str(error.value)


def test_the_busy_hint_never_offers_to_fix_it_for_the_user(device, monkeypatch):
    monkeypatch.setattr(mtp.platform, "system", lambda: "Darwin")
    runner = FakeRunner(lambda ops: mtp.parse_helper_output("", "device is busy\n", 3))
    with pytest.raises(DeviceBusy) as error:
        backend(device, runner).list_files()
    assert "never" in str(error.value).lower()


def test_exit_code_4_raises_device_write_protected(device, tmp_path):
    source = tmp_path / "A Book.azw3"
    source.write_bytes(b"book")
    runner = FakeRunner(lambda ops: mtp.parse_helper_output("", "device is read-only\n", 4))
    with pytest.raises(DeviceWriteProtected):
        backend(device, runner).write(source, "documents/en/A Book.azw3")


def test_any_other_non_zero_exit_carries_the_stderr_tail(device):
    stderr = "\n".join(f"line {n}" for n in range(30))
    runner = FakeRunner(lambda ops: mtp.parse_helper_output("", stderr, 1))
    with pytest.raises(CalibreError) as error:
        backend(device, runner).list_files()
    assert "line 29" in str(error.value)


# --- the eight backend methods over run_ops -------------------------------------


def test_write_sends_one_put_op_with_an_explicit_device_path(device, tmp_path):
    source = tmp_path / "A Book - An Author.azw3"
    source.write_bytes(b"book bytes")
    runner = FakeRunner(results({"op": "put", "ok": True, "size": 10}))

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
    runner = FakeRunner(results({"op": "get", "ok": True}))
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
    runner = FakeRunner(results({"op": "rm", "ok": False, "error": "no such file on device"}))
    with pytest.raises(CalibreError) as error:
        backend(device, runner).remove("documents/en/Gone.azw3")
    assert "no such file on device" in str(error.value)


def test_a_short_results_array_is_a_calibre_error(device):
    runner = FakeRunner(results())
    with pytest.raises(CalibreError):
        backend(device, runner).remove("documents/en/A Book.azw3")


# --- batching and cache invalidation --------------------------------------------


def test_several_ops_produce_exactly_one_runner_invocation(device, tmp_path):
    source = tmp_path / "A Book.azw3"
    source.write_bytes(b"x")
    runner = FakeRunner(
        results(
            {"op": "mkdir", "ok": True},
            {"op": "put", "ok": True, "size": 1},
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
        results({"op": "put", "ok": True, "size": 3}),
        results(
            listing(
                ("documents/en/A Book.azw3", 1, 1.0),
                ("documents/en/New.azw3", 3, 2.0),
            )
        ),
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


# --- the helper script itself ---------------------------------------------------


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


def test_the_helper_loads_without_calibre_and_agrees_on_the_protocol():
    helper = load_helper()
    assert helper.RESULT_MARKER == mtp.RESULT_MARKER
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


def test_the_helpers_own_emitter_round_trips_through_the_backends_parser(capsys):
    """The two halves of the protocol, joined: the helper's real `_emit` writes the
    marker and the JSON, and the backend's real parser reads them back. This is the
    closest thing to an end-to-end test that exists without a device."""
    helper = load_helper()
    print("calibre-debug chatter before the helper says anything")
    helper._emit(
        [{"op": "list", "ok": True, "files": [{"path": "a.azw3", "size": 1, "mtime": 2.0}]}]
    )
    print("and some trailing chatter")

    payload = mtp.parse_helper_output(capsys.readouterr().out, "", 0)
    assert payload["results"][0]["files"] == [{"path": "a.azw3", "size": 1, "mtime": 2.0}]


def test_the_helper_refuses_a_path_that_escapes_the_device_root():
    helper = load_helper()
    assert helper._split("/documents/en/") == ["documents", "en"]
    assert helper._split("") == []
    with pytest.raises(ValueError):
        helper._split("documents/../../etc/passwd")


def test_the_helper_rejects_an_ops_file_of_the_wrong_protocol_version(tmp_path):
    helper = load_helper()
    ops_file = tmp_path / "ops.json"
    ops_file.write_text(json.dumps({"v": 99, "ops": []}), encoding="utf-8")
    with pytest.raises(helper._HelperError):
        helper._read_ops(str(ops_file))

    ops_file.write_text(json.dumps({"v": 1, "ops": [{"op": "list", "path": ""}]}), encoding="utf-8")
    assert helper._read_ops(str(ops_file)) == [{"op": "list", "path": ""}]


def test_the_helper_classifies_error_text_onto_its_exit_codes():
    helper = load_helper()
    assert helper._classify(RuntimeError("Device is busy")) == mtp.EXIT_BUSY
    assert helper._classify(RuntimeError("filesystem is read-only")) == mtp.EXIT_WRITE_PROTECTED
    assert helper._classify(RuntimeError("no device attached")) == mtp.EXIT_NO_DEVICE
    assert helper._classify(RuntimeError("something else entirely")) == mtp.EXIT_FAILED


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


def test_the_mtp_backend_matches_the_mass_storage_backends_shape(device):
    """Both backends implement `backend.DeviceBackend`, and a caller (backup, add,
    sync) must not have to care which one it is driving. The Protocol is not
    runtime-checkable, so pin the eight method names and their signatures here."""
    import inspect

    from media_tools.tasks.ebook.kindle.backend import DeviceBackend
    from media_tools.tasks.ebook.kindle.massstorage import MassStorageBackend

    methods = [name for name in vars(DeviceBackend) if not name.startswith("_")]
    assert sorted(methods) == [
        "close",
        "eject",
        "exists",
        "free_space",
        "list_files",
        "read",
        "remove",
        "write",
    ]
    for name in methods:
        expected = inspect.signature(getattr(MassStorageBackend, name))
        assert inspect.signature(getattr(mtp.MtpBackend, name)) == expected, name


def test_a_malformed_result_entry_is_a_calibre_error(device):
    runner = FakeRunner({"v": 1, "results": ["not an object"]})
    with pytest.raises(CalibreError) as error:
        backend(device, runner).remove("documents/en/A Book.azw3")
    assert "malformed" in str(error.value)
