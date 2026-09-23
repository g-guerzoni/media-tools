import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from media_tools.tasks.ebook.kindle import backend, massstorage


def test_lists_only_files_under_the_prefix(fake_kindle):
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    documents = device.list_files("documents")
    assert documents
    assert all(f.path.startswith("documents/") for f in documents)
    assert any(f.path.endswith(".azw3") for f in documents)


def test_listing_a_prefix_that_is_not_there_is_empty_but_a_missing_mount_raises(fake_kindle):
    """`[]` must mean "nothing is there", never "I could not look". A prefix that is
    not on the device is the first; a mount that is gone is the second, and answering
    `[]` for it is how a backup writes an empty snapshot and calls itself a success."""
    import shutil as _shutil

    device = massstorage.MassStorageBackend(fake_kindle.mount)
    assert device.list_files("documents/de") == []

    _shutil.rmtree(fake_kindle.mount)
    with pytest.raises(FileNotFoundError):
        device.list_files()
    with pytest.raises(FileNotFoundError):
        device.list_files("documents")


def test_an_unmounted_volume_is_not_a_kindle_with_nothing_on_it(fake_kindle, monkeypatch):
    """Unmounting leaves the mountpoint DIRECTORY behind, empty and on the parent
    filesystem, so `is_dir()` still says yes and a walk returns nothing — the last way
    an empty listing could have meant "I could not look"."""
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    assert device._device_id == fake_kindle.mount.stat().st_dev, "recorded from a real stat"
    assert device.list_files("documents"), "the device answers while it is still mounted"

    # The directory is still there and still non-empty; only the filesystem under it
    # changed, which is exactly what an unmount looks like.
    monkeypatch.setattr(massstorage, "_device_id", lambda mount: device._device_id + 1)
    with pytest.raises(FileNotFoundError):
        device.list_files()
    with pytest.raises(FileNotFoundError):
        device.list_files("documents")


#: A directory chmod'd to 0 is still readable by root, so the two permission tests
#: below would silently assert nothing in a container that runs as root.
skip_as_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="a chmod 0 directory is still readable by root",
)


@skip_as_root
def test_a_directory_that_cannot_be_read_raises_rather_than_listing_short(fake_kindle):
    """The last hole in "an empty listing means nothing is there, never I could not
    look": `_walk` used to swallow the `OSError` `os.scandir` raises for a directory
    it cannot open, and — being a generator — its bare `return` ENDED the listing
    there, silently. Nothing downstream could tell that walk from a complete one.

    Note that `entry.stat()`, one line further in, was never inside that handler: the
    same failure one line later already raised. This pins the whole function on the
    raising side of that line.
    """
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    assert device.list_files("documents"), "the device answers while everything is readable"

    unreadable = fake_kindle.mount / "documents" / "pt"
    unreadable.chmod(0o000)
    try:
        with pytest.raises(OSError):
            device.list_files()
        with pytest.raises(OSError):
            device.list_files("documents")
    finally:
        unreadable.chmod(0o755)


@skip_as_root
def test_an_unreadable_mount_root_raises_rather_than_answering_empty(fake_kindle):
    """The worst shape of the same defect: the mount itself is there and is a
    directory (so neither of `list_files`' own guards fires) but cannot be opened, and
    the walk answered `[]` — which `backup._listing` reads as a factory-reset Kindle."""
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    fake_kindle.mount.chmod(0o000)
    try:
        with pytest.raises(OSError):
            device.list_files()
    finally:
        fake_kindle.mount.chmod(0o755)


@skip_as_root
def test_a_write_the_device_refuses_as_read_only_is_write_protected_not_not_found(
    fake_kindle, tmp_path
):
    """`DeviceWriteProtected` used to be raised in exactly one place, `mtp.py` — this
    module never even imported it. So a Kindle mounted read-only, or one locked by its
    own firmware, raised a plain `OSError` here, which `cli._error_code_for` maps to
    `device_not_found`: the user was told their Kindle had gone away, for a device
    sitting on the desk, after a mandatory backup had already run."""
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    source = tmp_path / "A Book.azw3"
    source.write_bytes(b"book")

    read_only = fake_kindle.mount / "documents" / "en"
    read_only.chmod(0o555)
    try:
        with pytest.raises(backend.DeviceWriteProtected):
            device.write(source, "documents/en/A Book.azw3")
        # And nothing is left sitting at a `.partial` name on the device either.
        assert [p.name for p in read_only.iterdir() if p.name.endswith(".partial")] == []
    finally:
        read_only.chmod(0o755)


@skip_as_root
def test_a_HOST_side_permission_failure_never_claims_the_device_is_write_protected(
    fake_kindle, tmp_path
):
    """`write` copies FROM a host file — a library book for `add`/`sync`, a file out of
    a snapshot for `restore`. Classifying by errno alone therefore turned a host-side
    `EACCES` into "the Kindle refused the write as read-only": a confident false
    statement about the user's hardware, which is the exact defect the write-protected
    code was added to stop, reintroduced one layer up.

    On `restore` it is worse than a misleading per-book detail — the write is unguarded
    there, so it becomes a run-level `device_write_protected`/exit 3 asserting the
    device is locked when nothing is wrong with the device at all.
    """
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    unreadable_source = tmp_path / "library" / "A Book.azw3"
    unreadable_source.parent.mkdir(parents=True)
    unreadable_source.write_bytes(b"book")
    unreadable_source.chmod(0o000)
    try:
        with pytest.raises(OSError) as caught:
            device.write(unreadable_source, "documents/en/A Book.azw3")
    finally:
        unreadable_source.chmod(0o644)

    assert not isinstance(caught.value, backend.DeviceWriteProtected)
    # The original error survives, naming the host path and the errno — which is what
    # a user needs in order to look in the right place.
    assert caught.value.filename == str(unreadable_source)
    assert [
        p.name
        for p in (fake_kindle.mount / "documents" / "en").iterdir()
        if p.name.endswith(".partial")
    ] == []


@skip_as_root
def test_a_delete_the_device_refuses_as_read_only_is_write_protected_too(fake_kindle):
    """The other half of "anywhere it writes": a removal on a read-only volume is the
    same refusal, and must not read as "the book was already gone" (`FileNotFoundError`,
    which `_remove_one` acts on) or as an unplugged device."""
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    read_only = fake_kindle.mount / "documents" / "en"
    read_only.chmod(0o555)
    try:
        with pytest.raises(backend.DeviceWriteProtected):
            device.remove("documents/en/A Book - An Author.azw3")
    finally:
        read_only.chmod(0o755)
    assert (read_only / "A Book - An Author.azw3").is_file()


def test_read_leaves_nothing_behind_when_the_copy_dies_part_way(fake_kindle, tmp_path, monkeypatch):
    """The same guarantee `write` makes for the device, in the other direction — and
    the one MTP's helper already made by removing its half-fetched local file."""
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    dest = tmp_path / "book.azw3"

    def die_part_way(source, target):
        Path(target).write_bytes(b"half")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(massstorage.shutil, "copyfile", die_part_way)
    with pytest.raises(OSError):
        device.read("documents/en/A Book - An Author.azw3", dest)

    assert not dest.exists()
    assert list(tmp_path.glob("*.partial")) == []


def test_a_read_interrupted_by_ctrl_c_leaves_no_partial_on_the_host(
    fake_kindle, tmp_path, monkeypatch
):
    """The same `BaseException` rule `write` follows. A Ctrl+C during a library-sized
    backup is not exotic — it is the most likely way a long transfer ends — and it must
    not strand a `.partial` beside every file it had reached."""
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    dest = tmp_path / "book.azw3"

    def interrupt(source, target):
        Path(target).write_bytes(b"half")
        raise KeyboardInterrupt

    monkeypatch.setattr(massstorage.shutil, "copyfile", interrupt)
    with pytest.raises(KeyboardInterrupt):
        device.read("documents/en/A Book - An Author.azw3", dest)
    monkeypatch.undo()

    assert not dest.exists()
    assert list(tmp_path.glob("*.partial")) == []


def test_a_write_interrupted_by_ctrl_c_leaves_no_partial_on_the_device(
    fake_kindle, tmp_path, monkeypatch
):
    """`except Exception` let a KeyboardInterrupt strand a `.partial` ON THE DEVICE —
    the one failure this module's own docstring promises cannot happen."""
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    source = tmp_path / "New.azw3"
    source.write_bytes(b"a new book")

    def interrupt(local, temp):
        Path(temp).write_bytes(b"a new")
        raise KeyboardInterrupt

    monkeypatch.setattr(massstorage.shutil, "copyfile", interrupt)
    with pytest.raises(KeyboardInterrupt):
        device.write(source, "documents/en/New.azw3")
    monkeypatch.undo()

    assert not device.exists("documents/en/New.azw3")
    assert list((fake_kindle.mount / "documents" / "en").glob("*.partial")) == []


def test_write_then_read_round_trips(fake_kindle, tmp_path):
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    source = tmp_path / "New Book.azw3"
    source.write_bytes(b"book bytes")
    device.write(source, "documents/en/New Book.azw3")
    assert device.exists("documents/en/New Book.azw3")

    back = tmp_path / "back.azw3"
    device.read("documents/en/New Book.azw3", back)
    assert back.read_bytes() == b"book bytes"


@pytest.mark.parametrize(
    "path",
    [
        "../outside.azw3",
        "documents/../../outside.azw3",
        "/etc/passwd",
        "audible/x.azw3",
        "documents/audible/x.azw3",
        "system/wifi.cfg",
        "system/notthumbnails/x.jpg",
        # A SECOND `system/` component, past a legitimate `system/thumbnails/`
        # child — `validate_writable_path` must check every occurrence, not
        # just the first (a `.index()`-based check would stop at the first
        # "system", see it followed by "thumbnails", and never look at this
        # second one at all).
        "system/thumbnails/system/x.jpg",
    ],
)
def test_write_refuses_a_path_outside_what_this_project_may_ever_touch(fake_kindle, tmp_path, path):
    """A device path can be built from UNTRUSTED data (Task 6: a book's own EXTH
    records) — this is the backstop `validate_writable_path` gives BOTH backends,
    independent of whatever a caller building the path did or did not check."""
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    source = tmp_path / "x.azw3"
    source.write_bytes(b"x")
    with pytest.raises(backend.DeviceWritePathRejected):
        device.write(source, path)


def test_write_still_allows_a_thumbnail_under_system(fake_kindle, tmp_path):
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    source = tmp_path / "cover.jpg"
    source.write_bytes(b"jpeg bytes")
    device.write(source, "system/thumbnails/thumbnail_ABC123_EBOK_portrait.jpg")
    assert device.exists("system/thumbnails/thumbnail_ABC123_EBOK_portrait.jpg")


def test_exists_raises_rather_than_answering_false_when_the_device_is_gone(fake_kindle):
    """A bare `Path.is_file()` swallows `OSError` and answers `False` for an
    unmounted volume exactly as it would for a file that genuinely never existed —
    a caller verifying a just-written file (e.g. installing a Kindle thumbnail)
    needs to be able to tell "verified absent" from "the device vanished mid-check",
    which a possibly-wrong `False` cannot express."""
    import shutil as _shutil

    device = massstorage.MassStorageBackend(fake_kindle.mount)
    assert device.exists("documents/en/A Book - An Author.azw3") is True

    _shutil.rmtree(fake_kindle.mount)
    with pytest.raises(FileNotFoundError):
        device.exists("documents/en/A Book - An Author.azw3")


def test_remove_refuses_to_call_a_vanished_device_an_absent_file(fake_kindle, monkeypatch):
    """`FileNotFoundError` out of `remove` means one thing to its callers — THIS PATH
    is not on the device, so the book was already gone — and `ebook kindle remove`
    reports exactly that and moves on. An unmounted volume makes `unlink()` raise the
    same exception for every path, which would turn a Kindle pulled mid-run into a
    run that reports every remaining book as already gone and exits 0. `DeviceNotFound`
    is a `RuntimeError`, which every caller treats as the real fault it is."""
    from media_tools.tasks.ebook.kindle.detect import DeviceNotFound

    device = massstorage.MassStorageBackend(fake_kindle.mount)
    # The two families must stay DISJOINT, which is what lets `ebook kindle remove`
    # tell them apart by type alone: were `DeviceNotFound` ever made a
    # `FileNotFoundError` subclass (as `mtp.MtpPathNotInCachedTree` deliberately is),
    # every caller written to that contract would silently start reading an unplugged
    # Kindle as a book that was already gone.
    assert not issubclass(DeviceNotFound, FileNotFoundError)

    # A path that really is absent still reports itself as absent.
    with pytest.raises(FileNotFoundError):
        device.remove("documents/en/Never Existed.azw3")

    monkeypatch.setattr(massstorage, "_device_id", lambda mount: device._device_id + 1)
    with pytest.raises(DeviceNotFound):
        device.remove("documents/en/A Book - An Author.azw3")
    # ...and the book is still there, because nothing was attempted.
    assert (fake_kindle.mount / "documents/en/A Book - An Author.azw3").is_file()


def test_write_is_atomic_leaving_no_partial_file(fake_kindle, tmp_path, monkeypatch):
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    source = tmp_path / "x.azw3"
    source.write_bytes(b"x" * 100)

    def explode(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(massstorage.shutil, "copyfile", explode)
    with pytest.raises(OSError):
        device.write(source, "documents/x.azw3")
    assert not device.exists("documents/x.azw3")
    assert list((fake_kindle.mount / "documents").glob("*.partial")) == []


def test_write_is_atomic_when_the_final_replace_fails(fake_kindle, tmp_path, monkeypatch):
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    source = tmp_path / "z.azw3"
    source.write_bytes(b"z" * 100)

    def explode(temp, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(massstorage, "fsync_replace", explode)
    with pytest.raises(OSError):
        device.write(source, "documents/z.azw3")
    assert not device.exists("documents/z.azw3")
    assert list((fake_kindle.mount / "documents").glob("*.partial")) == []


def test_remove_deletes_only_what_was_asked(fake_kindle):
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    before = {f.path for f in device.list_files("documents")}
    victim = next(p for p in before if p.endswith(".azw3"))
    device.remove(victim)
    after = {f.path for f in device.list_files("documents")}
    assert victim not in after
    assert after == before - {victim}


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Normal Book.azw3", "Normal Book.azw3"),
        ("AC/DC: Story.azw3", "ACDC Story.azw3"),
        ("trailing dot..azw3", "trailing dot.azw3"),
        ("ctrl\x07char.azw3", "ctrlchar.azw3"),
    ],
)
def test_device_names_follow_fat32_rules(name, expected):
    assert backend.sanitize_device_name(name, max_path=250) == expected


def test_a_very_long_name_is_truncated_but_keeps_its_extension():
    name = "x" * 400 + ".azw3"
    out = backend.sanitize_device_name(name, max_path=100)
    assert out.endswith(".azw3") and len(out) <= 100


def test_free_space_is_reported(fake_kindle):
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    assert device.free_space() > 0


def test_list_files_skips_volume_litter_and_never_descends_into_audible(fake_kindle):
    (fake_kindle.mount / "documents" / "en" / "._AppleDouble").write_bytes(b"junk")
    (fake_kindle.mount / "documents" / ".Trashes").mkdir()
    (fake_kindle.mount / "documents" / ".fseventsd").mkdir()
    (fake_kindle.mount / "documents" / ".Spotlight-V100").mkdir()
    (fake_kindle.mount / "audible" / "book.aax").write_bytes(b"audiobook bytes")

    device = massstorage.MassStorageBackend(fake_kindle.mount)
    paths = {f.path for f in device.list_files()}

    assert not any(p.startswith("audible/") for p in paths)
    assert not any("._AppleDouble" in p for p in paths)
    assert not any(".Trashes" in p or ".fseventsd" in p or ".Spotlight-V100" in p for p in paths)
    assert any(p.endswith(".azw3") for p in paths)


def test_list_files_never_descends_into_audible_even_as_the_requested_prefix(fake_kindle):
    (fake_kindle.mount / "audible" / "book.aax").write_bytes(b"audiobook bytes")
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    assert device.list_files("audible") == []


def test_list_files_lists_system_thumbnails_but_skips_the_rest_of_system(fake_kindle):
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    paths = {f.path for f in device.list_files()}
    assert "system/thumbnails/cover.jpg" in paths
    assert "system/wifi/wifi.cfg" not in paths


def test_list_files_on_a_non_thumbnails_system_prefix_returns_nothing(fake_kindle):
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    assert device.list_files("system/wifi") == []


def test_eject_runs_sync_then_diskutil_eject_on_macos(fake_kindle, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["diskutil", "info"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=plistlib.dumps({"ParentWholeDisk": "disk9"}), stderr=b""
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(massstorage.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(massstorage.subprocess, "run", fake_run)

    massstorage.MassStorageBackend(fake_kindle.mount).eject()

    assert calls[0] == ["sync"]
    assert calls[1] == ["diskutil", "info", "-plist", str(fake_kindle.mount)]
    assert calls[2] == ["diskutil", "eject", "disk9"]


def test_eject_retries_once_when_diskutil_reports_the_device_busy(fake_kindle, monkeypatch):
    eject_attempts = []

    def fake_run(argv, **kwargs):
        if argv[:2] == ["diskutil", "info"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=plistlib.dumps({"ParentWholeDisk": "disk9"}), stderr=b""
            )
        if argv[:2] == ["diskutil", "eject"]:
            eject_attempts.append(argv)
            if len(eject_attempts) == 1:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Resource busy")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(massstorage.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(massstorage.subprocess, "run", fake_run)

    massstorage.MassStorageBackend(fake_kindle.mount).eject()

    assert len(eject_attempts) == 2


def test_eject_raises_when_still_busy_after_the_retry(fake_kindle, monkeypatch):
    def fake_run(argv, **kwargs):
        if argv[:2] == ["diskutil", "info"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=plistlib.dumps({"ParentWholeDisk": "disk9"}), stderr=b""
            )
        if argv[:2] == ["diskutil", "eject"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Resource busy")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(massstorage.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(massstorage.subprocess, "run", fake_run)

    # `DeviceBusy`, not a bare RuntimeError: `cli._error_code_for` tests for it before
    # anything broader, so `ebook kindle eject` reports `device_busy` rather than
    # `dependency_missing` — which would tell the user to install something because a
    # window is open on their Kindle. It remains a RuntimeError subclass, so a caller
    # that only cares that this failed is unaffected.
    with pytest.raises(massstorage.DeviceBusy):
        massstorage.MassStorageBackend(fake_kindle.mount).eject()


def test_an_eject_that_fails_for_any_other_reason_is_not_reported_as_busy(fake_kindle, monkeypatch):
    def fake_run(argv, **kwargs):
        if argv[:2] == ["diskutil", "info"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=plistlib.dumps({"ParentWholeDisk": "disk9"}), stderr=b""
            )
        if argv[:2] == ["diskutil", "eject"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Unable to eject")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(massstorage.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(massstorage.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as caught:
        massstorage.MassStorageBackend(fake_kindle.mount).eject()
    assert not isinstance(caught.value, massstorage.DeviceBusy)


def test_eject_uses_udisksctl_unmount_then_power_off_on_linux(fake_kindle, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(massstorage.platform, "system", lambda: "Linux")
    monkeypatch.setattr(massstorage, "_device_for_mount", lambda mount: "/dev/sdb1")
    monkeypatch.setattr(massstorage.subprocess, "run", fake_run)

    massstorage.MassStorageBackend(fake_kindle.mount).eject()

    assert calls[0] == ["sync"]
    assert calls[1] == ["udisksctl", "unmount", "-b", "/dev/sdb1"]
    assert calls[2] == ["udisksctl", "power-off", "-b", "/dev/sdb1"]


def test_device_for_mount_parses_a_realistic_proc_mounts(tmp_path):
    mount = tmp_path / "Kindle"
    mount.mkdir()
    mounts_file = tmp_path / "mounts"
    mounts_file.write_text(
        "proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n"
        "/dev/sda1 / ext4 rw,relatime 0 0\n"
        f"/dev/sdb1 {mount} vfat rw,nosuid,nodev,relatime,uid=1000 0 0\n"
        "tmpfs /run tmpfs rw,nosuid,nodev 0 0\n",
        encoding="utf-8",
    )
    assert massstorage._device_for_mount(mount, mounts_file=mounts_file) == "/dev/sdb1"


def test_device_for_mount_falls_back_to_the_mount_path_when_not_listed(tmp_path):
    mount = tmp_path / "Kindle"
    mount.mkdir()
    mounts_file = tmp_path / "mounts"
    mounts_file.write_text(
        "proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n", encoding="utf-8"
    )
    assert massstorage._device_for_mount(mount, mounts_file=mounts_file) == str(mount)


def test_a_truncated_diskutil_plist_falls_back_instead_of_crashing_the_eject(
    fake_kindle, monkeypatch
):
    """`plistlib.loads` raises `xml.parsers.expat.ExpatError` for XML that stops
    part-way, and that is NOT a `ValueError` — so the `except (ValueError, TypeError)`
    beside it caught the empty/garbage case and missed the truncated one. `eject` then
    failed as `internal_error`/exit 1 instead of using the fallback sitting right
    there."""
    truncated = b'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
    monkeypatch.setattr(
        massstorage,
        "_run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=truncated, stderr=b""),
    )
    assert massstorage._parent_disk_macos(fake_kindle.mount) == str(fake_kindle.mount)


def test_an_eject_that_ran_and_refused_is_not_a_missing_dependency(fake_kindle, monkeypatch):
    """The THIRD failure mode, which had no shape of its own: `_run_with_retry` raised
    a bare `RuntimeError` for any non-busy failure, and `_error_code_for`'s catch-all
    turns that into `dependency_missing` — telling the user to install a binary that
    just ran."""

    def fake_run(argv, **kwargs):
        if argv[:2] == ["diskutil", "info"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=plistlib.dumps({"ParentWholeDisk": "disk9"}), stderr=b""
            )
        if argv[:2] == ["diskutil", "eject"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Unable to eject")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(massstorage.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(massstorage.subprocess, "run", fake_run)

    with pytest.raises(massstorage.EjectFailed) as caught:
        massstorage.MassStorageBackend(fake_kindle.mount).eject()
    assert not isinstance(caught.value, massstorage.DeviceBusy)
    assert "Unable to eject" in str(caught.value)
