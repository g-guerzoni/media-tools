import pytest

from media_tools.tasks.ebook.kindle import detect


def _kindle_usb(product_id=0x0004, serial="G000XXXXXXXXXXXX", product="Kindle"):
    return [
        {
            "vendor_id": detect.KINDLE_VENDOR_ID,
            "product_id": product_id,
            "serial": serial,
            "product": product,
        }
    ]


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


def test_model_hint_comes_from_what_the_device_calls_itself(tmp_path):
    """`Device.model_hint` existed and was never set by anything, so
    `status.data.device.model_hint` was always `null` and `backup.device_key`'s second
    fallback was dead code. It is the name the DEVICE reports over USB, not a lookup in
    a model table — detection has no such table and must not grow one."""
    mount = _fake_mount(tmp_path)
    mass = detect.find_device(
        usb_lister=lambda: _kindle_usb(product="Kindle Paperwhite"),
        mount_lister=lambda: [mount],
    )
    assert mass.model_hint == "Kindle Paperwhite"

    mtp = detect.find_device(
        usb_lister=lambda: _kindle_usb(product_id=0x9981, serial=None, product="Kindle Scribe"),
        mount_lister=lambda: [],
    )
    assert mtp.model_hint == "Kindle Scribe"


def test_a_device_that_reports_no_product_name_still_resolves():
    device = detect.find_device(
        usb_lister=lambda: [
            {"vendor_id": detect.KINDLE_VENDOR_ID, "product_id": 0x9981, "serial": "S"}
        ],
        mount_lister=lambda: [],
    )
    assert device.model_hint is None


def test_identify_false_never_touches_the_usb_bus_for_a_mounted_kindle(tmp_path):
    """The gate Ruling R55 applied to the MTP driver probe, applied to the USB probe:
    `doctor` asks only whether a Kindle is attached and in which mode, and a mount
    answers both. It never prints a serial, so there is nothing left to enumerate for.
    """
    mount = _fake_mount(tmp_path)
    calls = []

    def must_not_be_called():
        calls.append(1)
        return _kindle_usb()

    device = detect.find_device(
        usb_lister=must_not_be_called, mount_lister=lambda: [mount], identify=False
    )
    assert calls == []
    assert device.mode == "mass_storage"
    assert device.mount == mount
    assert (device.serial, device.product_id, device.model_hint) == (None, None, None)


def test_identify_false_still_asks_when_there_is_no_mount_to_answer(tmp_path):
    """An MTP Kindle has no mount, so the bus is the only thing that can answer — the
    gate skips a probe that cannot change the answer, not one that can."""
    calls = []

    def lister():
        calls.append(1)
        return _kindle_usb(product_id=0x9981)

    device = detect.find_device(usb_lister=lister, mount_lister=lambda: [], identify=False)
    assert calls == [1]
    assert device.mode == "mtp"
