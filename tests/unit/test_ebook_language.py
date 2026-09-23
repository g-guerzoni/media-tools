import pytest

from media_tools.tasks.ebook import language


@pytest.mark.parametrize(
    "title,expected",
    [
        ("A Igreja do Diabo", "pt"),
        ("Memórias Póstumas de Brás Cubas", "pt"),
        ("The Blade Itself", "en"),
        ("A Deepness in the Sky", "en"),
        ("La Sombra del Viento", "es"),
        ("Il Nome della Rosa", "it"),
        ("Le Comte de Monte-Cristo", "fr"),
        ("Der Prozess", "de"),
    ],
)
def test_detects_the_obvious_cases(title, expected):
    assert language.detect(title) == expected


@pytest.mark.parametrize("title", ["Solaris", "1984", "Dune", "R"])
def test_returns_none_rather_than_guessing(title):
    assert language.detect(title) is None


def test_case_and_accents_do_not_change_the_answer():
    assert language.detect("MEMÓRIAS PÓSTUMAS DE BRÁS CUBAS") == "pt"
    assert language.detect("memorias postumas de bras cubas") == "pt"
