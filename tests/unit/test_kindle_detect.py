import pytest

from media_tools.tasks.ebook.kindle import detect


def _kindle_usb(product_id=0x0004, serial="G000XXXXXXXXXXXX"):
    return [{"vendor_id": detect.KINDLE_VENDOR_ID, "product_id": product_id, "serial": serial}]


def _fake_mount(tmp_path):
    root = tmp_path / "Kindle"
    (root / "documents").mkdir(parents=True)
    (root / "system").mkdir()
    return root


def test_mass_storage_device_is_detected(tmp_path):
    mount = _fake_mount(tmp_path)
    device = detect.find_device(usb_lister=lambda: _kindle_usb(), mount_lister=lambda: [mount])
    assert device.mode == "mass_storage"
    assert device.mount == mount
    assert device.serial == "G000XXXXXXXXXXXX"


def test_a_kindle_with_no_mount_is_mtp(tmp_path):
    device = detect.find_device(
        usb_lister=lambda: _kindle_usb(product_id=0x9981), mount_lister=lambda: []
    )
    assert device.mode == "mtp"
    assert device.mount is None


def test_a_volume_without_the_kindle_layout_is_ignored(tmp_path):
    stranger = tmp_path / "USB"
    (stranger / "photos").mkdir(parents=True)
    with pytest.raises(detect.DeviceNotFound):
        detect.find_device(usb_lister=lambda: [], mount_lister=lambda: [stranger])


def test_no_kindle_raises_with_a_helpful_message():
    with pytest.raises(detect.DeviceNotFound) as excinfo:
        detect.find_device(usb_lister=lambda: [], mount_lister=lambda: [])
    message = str(excinfo.value)
    assert "USB" in message and "connect" in message


def test_a_non_kindle_usb_device_is_ignored():
    others = [{"vendor_id": 0x05AC, "product_id": 0x1234, "serial": "x"}]
    with pytest.raises(detect.DeviceNotFound):
        detect.find_device(usb_lister=lambda: others, mount_lister=lambda: [])


def test_a_truncated_ioreg_plist_is_no_devices_not_an_internal_error(monkeypatch):
    """Same missing guard as `massstorage._parent_disk_macos`, one step earlier and
    one exit code worse: detection failing here reaches the user as
    `internal_error`/exit 1 rather than `device_not_found`/exit 3, on every single
    `ebook kindle` command."""
    import subprocess

    truncated = b'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><array>'
    monkeypatch.setattr(
        detect.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=truncated, stderr=b""),
    )
    assert detect._list_usb_macos() == []
