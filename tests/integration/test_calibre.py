import pytest

from media_tools.integrations import calibre

pytestmark = pytest.mark.skipif(
    calibre.find_tool("ebook-convert") is None, reason="Calibre is not installed"
)


def test_read_metadata_returns_the_embedded_fields(make_epub, tmp_path):
    book = make_epub(title="A Study in Scarlet", author="Arthur Conan Doyle", language="en")
    meta = calibre.read_metadata(book, cache_dir=tmp_path / "cache")
    assert meta.title == "A Study in Scarlet"
    assert meta.author == "Arthur Conan Doyle"
    assert meta.language == "en"
    assert meta.uuid


def test_read_metadata_of_a_missing_file_is_all_none(tmp_path):
    # `ebook-meta --to-opf` is deliberately lenient: given a file it CAN open — even
    # one that is not really a book, e.g. a .txt with no book-like structure — it
    # still succeeds and synthesizes a title from the filename and "Unknown" for the
    # author, rather than failing. A file that does not exist at all is the one input
    # that reliably makes it fail (non-zero exit, nothing written), so that is what
    # exercises read_metadata's "nothing readable" branch.
    missing = tmp_path / "does-not-exist.epub"
    meta = calibre.read_metadata(missing, cache_dir=tmp_path / "cache")
    assert meta.title is None and meta.author is None and meta.uuid is None


def test_convert_produces_the_target_format(make_epub, tmp_path):
    book = make_epub(title="Convertible")
    out = tmp_path / "out.azw3"
    calibre.convert(book, out, opf=None, cover=None, cache_dir=tmp_path / "cache")
    assert out.is_file() and out.stat().st_size > 1000


def test_convert_raises_with_the_tail_of_stderr(tmp_path):
    broken = tmp_path / "broken.epub"
    broken.write_bytes(b"not an epub")
    with pytest.raises(calibre.CalibreError) as excinfo:
        calibre.convert(
            broken, tmp_path / "out.azw3", opf=None, cover=None, cache_dir=tmp_path / "cache"
        )
    assert str(excinfo.value)


def test_calls_never_touch_the_user_config(make_epub, tmp_path):
    cache = tmp_path / "cache"
    calibre.read_metadata(make_epub(title="Isolated"), cache_dir=cache)
    assert (cache / "calibre-config").is_dir()
