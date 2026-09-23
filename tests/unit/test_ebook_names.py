from pathlib import Path

import pytest

from media_tools.tasks.ebook import names


@pytest.mark.parametrize(
    "stem,title,author",
    [
        ("Vinge, Vernor - A Deepness in the Sky", "A Deepness in the Sky", "Vernor Vinge"),
        ("Cidade de Deus - Paulo Lins", "Cidade de Deus", "Paulo Lins"),
        ("A Igreja do Diabo - Machado de Assis", "A Igreja do Diabo", "Machado de Assis"),
        ("The Blade Itself", "The Blade Itself", None),
        ("Goosebumps 2 - R. L. Stine", "Goosebumps 2", "R. L. Stine"),
    ],
)
def test_parse_filename(stem, title, author):
    assert names.parse_filename(Path(f"/books/{stem}.epub")) == (title, author)


def test_a_trailing_segment_that_is_not_a_name_stays_in_the_title():
    title, author = names.parse_filename(Path("/books/Report - 2nd Edition.pdf"))
    assert author is None
    assert "2nd Edition" in title


@pytest.mark.parametrize(
    "value", ["Microsoft Word - doc1.doc", "tmp1603", "book.indd", "scan0001.pdf"]
)
def test_junk_titles_are_recognised(value):
    assert names.is_junk_title(value)


@pytest.mark.parametrize(
    "value", ["Isabel] Isabel Allende [Allende", "unknown", "", "administrator.doc"]
)
def test_junk_authors_are_recognised(value):
    assert names.is_junk_author(value)


def test_a_real_author_is_not_junk():
    assert not names.is_junk_author("Machado de Assis")
