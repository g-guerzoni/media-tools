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
