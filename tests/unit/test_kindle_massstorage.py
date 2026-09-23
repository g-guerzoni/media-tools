import plistlib
import subprocess

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


def test_write_then_read_round_trips(fake_kindle, tmp_path):
    device = massstorage.MassStorageBackend(fake_kindle.mount)
    source = tmp_path / "New Book.azw3"
    source.write_bytes(b"book bytes")
    device.write(source, "documents/en/New Book.azw3")
    assert device.exists("documents/en/New Book.azw3")

    back = tmp_path / "back.azw3"
    device.read("documents/en/New Book.azw3", back)
    assert back.read_bytes() == b"book bytes"


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

    with pytest.raises(RuntimeError):
        massstorage.MassStorageBackend(fake_kindle.mount).eject()


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
