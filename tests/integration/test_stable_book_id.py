from media_tools.integrations import calibre
from media_tools.tasks.ebook import exth, opf
from tests.conftest import requires_calibre

pytestmark = requires_calibre


def test_the_same_source_converts_to_the_same_embedded_id_every_time(make_epub, tmp_path):
    """The Kindle names a cover thumbnail after EXTH 113; if it changed per run,
    every rebuild would orphan every cover. Plan C depends on this holding."""
    book = make_epub(title="Stable", author="Ada Lovelace", language="en")
    cache = tmp_path / "cache"
    descriptor = tmp_path / "book.opf"
    opf.write_opf(
        descriptor,
        title="Stable",
        author="Ada Lovelace",
        language="en",
        book_uuid=opf.book_id(book),
    )

    ids = []
    for index in (1, 2):
        out = tmp_path / f"out{index}.azw3"
        calibre.convert(book, out, opf=descriptor, cover=None, cache_dir=cache)
        records = exth.read_records(out)
        ids.append(exth.record_text(records, exth.TAG_UUID))
        assert exth.record_text(records, exth.TAG_TITLE) == "Stable"
        assert exth.record_text(records, exth.TAG_AUTHOR) == "Ada Lovelace"
        assert exth.record_text(records, exth.TAG_CDETYPE) == "EBOK"
        assert exth.record_text(records, exth.TAG_LANGUAGE) == "en"

    assert ids[0] == ids[1] == opf.book_id(book)
