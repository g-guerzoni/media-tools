from media_tools.tasks.ebook import exth, opf


def test_records_of_a_non_mobi_file_are_empty(tmp_path):
    junk = tmp_path / "x.txt"
    junk.write_bytes(b"hello")
    assert exth.read_records(junk) == {}


def test_book_id_is_stable_for_identical_content(tmp_path):
    first = tmp_path / "a.epub"
    second = tmp_path / "b.epub"
    first.write_bytes(b"same bytes")
    second.write_bytes(b"same bytes")
    assert opf.book_id(first) == opf.book_id(second)
    assert len(opf.book_id(first)) == 36


def test_book_id_differs_for_different_content(tmp_path):
    first = tmp_path / "a.epub"
    second = tmp_path / "b.epub"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    assert opf.book_id(first) != opf.book_id(second)


def test_write_opf_round_trips_the_fields(tmp_path):
    target = tmp_path / "book.opf"
    opf.write_opf(
        target,
        title="Título & Cia",
        author="Machado de Assis",
        language="pt",
        uuid="12345678-1234-5678-1234-567812345678",
    )
    text = target.read_text(encoding="utf-8")
    assert "Título &amp; Cia" in text
    assert "Machado de Assis" in text
    assert 'opf:scheme="uuid"' in text
    assert "12345678-1234-5678-1234-567812345678" in text
