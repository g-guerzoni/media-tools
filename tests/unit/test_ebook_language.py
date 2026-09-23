import pytest

from media_tools.tasks.ebook import language


@pytest.mark.parametrize(
    "title,expected",
    [
        ("A Igreja do Diabo", "pt"),
        ("The Blade Itself", "en"),
        ("A Deepness in the Sky", "en"),
        ("La Sombra del Viento", "es"),
        ("Cien Años de Soledad", "es"),
        ("La Casa de los Espíritus", "es"),
        ("Il Nome della Rosa", "it"),
        ("Les Misérables", "fr"),
        ("Der Prozess", "de"),
        ("Miłość", "pl"),
    ],
)
def test_detects_the_obvious_cases(title, expected):
    assert language.detect(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Solaris",
        "1984",
        "Dune",
        "R",
        # Both words here matched a per-language list before RULING RB7: "le"
        # (shared by French and Italian) and "de" (which used to count for
        # Portuguese unconditionally). Neither is exclusive to any one language,
        # so this must come back None, not a guessed shelf.
        "Le Comte de Monte-Cristo",
        # Only "de" matched here previously (Portuguese's own list, back when it
        # counted "de" unconditionally). It is no longer scored for anyone, and
        # nothing else in this title is an exclusive marker.
        "Memórias Póstumas de Brás Cubas",
        # A real book that the old, shared-word-scoring design mis-shelved as
        # "pt" (via the same unconditional "de"). Nothing here is exclusive.
        "Notre-Dame de Paris",
        # No letters at all -> no words, no diacritics, nothing to score.
        "!!!",
    ],
)
def test_returns_none_rather_than_guessing(title):
    assert language.detect(title) is None


def test_case_and_accents_do_not_change_the_answer():
    assert language.detect("A IGREJA DO DIABO") == "pt"
    assert language.detect("a igreja do diabo") == "pt"


def test_a_language_specific_diacritic_alone_is_enough():
    # "Cien Años de Soledad" (in test_detects_the_obvious_cases) already covers
    # this via "ñ", with no other Spanish marker in the title. This case checks
    # the mechanism directly: a title whose only marker at all is one exclusive
    # diacritic still clears MIN_SCORE on its own.
    assert language.detect("Ñoño") == "es"


def test_no_word_is_shared_between_two_languages():
    """The design in RULING RB7 depends on every stopword being exclusive to one
    language; this pins that invariant so a future edit can't silently reintroduce
    a collision like the "das" one caught during review (Portuguese's "das" is
    also German's "das")."""
    codes = list(language._STOPWORDS)
    for i, code_a in enumerate(codes):
        for code_b in codes[i + 1 :]:
            shared = language._STOPWORDS[code_a] & language._STOPWORDS[code_b]
            assert not shared, f"{code_a} and {code_b} both claim {shared}"


def test_no_hint_character_is_shared_between_two_languages():
    codes = list(language._HINT_CHARS)
    for i, code_a in enumerate(codes):
        for code_b in codes[i + 1 :]:
            shared = set(language._HINT_CHARS[code_a]) & set(language._HINT_CHARS[code_b])
            assert not shared, f"{code_a} and {code_b} both claim {shared}"
