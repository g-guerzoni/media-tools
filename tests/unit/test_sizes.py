import pytest

from media_tools.core.sizes import format_size, parse_size


@pytest.mark.parametrize(
    "text,expected",
    [
        ("25", 25_000_000),
        ("25MB", 25_000_000),
        ("25 mb", 25_000_000),
        ("1.5GB", 1_500_000_000),
        ("25MiB", 26_214_400),
        ("1GiB", 1_073_741_824),
        ("500KB", 500_000),
        ("500KiB", 512_000),
    ],
)
def test_parse_size(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "25XB", "-5MB", "MB"])
def test_parse_size_rejects(text):
    with pytest.raises(ValueError):
        parse_size(text)


def test_format_size_uses_decimal_units():
    assert format_size(25_000_000) == "25.0 MB"
    assert format_size(999) == "999 B"
