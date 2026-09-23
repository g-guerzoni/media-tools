import pytest

from media_tools.integrations import calibre
from tests.conftest import requires_calibre

pytestmark = requires_calibre


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


def test_read_metadata_reports_a_cover_when_the_book_has_one(make_epub, tmp_path):
    # ebook-convert auto-generates a default cover for every book that doesn't supply
    # one, so has_cover should be True for any book made by the make_epub fixture.
    book = make_epub(title="Has A Cover")
    meta = calibre.read_metadata(book, cache_dir=tmp_path / "cache")
    assert meta.has_cover is True


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
    # Pin the actual message Calibre prints for a corrupt/non-ZIP epub, not just "some
    # string" — a bare truthiness check would still pass if the tail were empty text.
    assert "Not a ZIP file" in str(excinfo.value)


def test_calls_never_touch_the_user_config(make_epub, tmp_path):
    cache = tmp_path / "cache"
    calibre.read_metadata(make_epub(title="Isolated"), cache_dir=cache)
    assert (cache / "calibre-config").is_dir()


def test_extract_cover_returns_false_on_internal_failure(monkeypatch, tmp_path):
    def _boom(*args, **kwargs):
        raise calibre.CalibreError("boom")

    monkeypatch.setattr(calibre, "_run", _boom)
    result = calibre.extract_cover(
        tmp_path / "book.epub", tmp_path / "cover.jpg", cache_dir=tmp_path / "cache"
    )
    assert result is False


def test_fetch_cover_returns_false_on_internal_failure(monkeypatch, tmp_path):
    def _boom(*args, **kwargs):
        raise calibre.CalibreError("boom")

    monkeypatch.setattr(calibre, "_run", _boom)
    result = calibre.fetch_cover(
        "Some Title", "Some Author", tmp_path / "cover.jpg", cache_dir=tmp_path / "cache"
    )
    assert result is False
