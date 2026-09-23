"""Guess a book's language from its title, offline.

Deliberately conservative: a title that does not clearly belong to one of the
supported languages returns None, so the book lands in the review folder instead
of the wrong shelf. With the LLM enabled, the model answers instead; this module
is the offline path and the fallback when the model abstains.

Design: every marker below (a stopword or a diacritic) is exclusive to one
language among the seven supported here — a word or character that two languages
share is dropped from both rather than counted for either. An earlier version of
this module scored shared words (`de`, `le`, ...) per language and only tried to
break ties after the fact; that produced real mis-shelving (e.g. "Notre-Dame de
Paris" and "Cien Años de Soledad" both landing on "pt" because `de` counted for
Portuguese unconditionally), and a tie-break patch for one collision (French vs.
Italian's shared `le`) only pushed the false positives to a different pair.
Refusing to score shared words at all is not a smaller version of that bug — it
removes its cause, at the cost of more titles coming back None. That trade is the
point: a wrong shelf is worse than a review folder.
"""

from __future__ import annotations

import re
import unicodedata

SUPPORTED = ("en", "pt", "es", "it", "fr", "de", "pl")

# Every word below is, to the best of this review, exclusive to its language among
# the seven in SUPPORTED — checked pairwise (see test_no_word_is_shared_between_two_languages).
# Entries are stored already folded (lowercase, accents stripped) because matching
# folds the title the same way (see `_fold`): a real accented word like Portuguese
# "às" is written here as "as", and Polish "się" as "sie", since that is the form
# they take after folding and it is what a folded title word is compared against.
# A collision found during review is resolved by deleting the word from BOTH
# lists, never by picking a winner: "das" is the Portuguese contraction of "de"+
# "as" *and* the German neuter "the", so it appears in neither list.
_STOPWORDS = {
    "en": frozenset({"the", "of", "and", "to", "on", "for", "from", "with", "his", "her"}),
    "pt": frozenset(
        {"do", "da", "dos", "no", "na", "nos", "nas", "uma", "pelo", "pela", "aos", "as"}
    ),
    "es": frozenset({"el", "los", "las", "del", "una", "unos", "unas", "y"}),
    "it": frozenset({"il", "lo", "gli", "della", "dei", "delle", "degli", "nel", "nella", "che"}),
    "fr": frozenset({"les", "des", "du", "une", "dans", "sur", "pour", "avec"}),
    "de": frozenset({"der", "die", "und", "ein", "eine", "im", "von", "zu", "mit", "den", "dem"}),
    "pl": frozenset({"w", "z", "nie", "sie", "oraz", "przez"}),
}

# Diacritics kept here are exclusive too (checked against the title's lowercased
# but *unfolded* form, so the accent itself is still there to find — see
# `_HINT_CHARS` usage below). Italian has no exclusive diacritic among these seven
# languages and relies on its stopwords alone; that is fine, not a gap to fill.
_HINT_CHARS = {
    "pt": "ãõ",
    "es": "ñ¿¡",
    "de": "ß",
    "pl": "łżźęąśćń",
    "fr": "œ",
}

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
MIN_SCORE = 2
MIN_MARGIN = 1
# A stopword and an exclusive diacritic are equally strong, exclusive evidence, so
# each is worth the same amount: one hit already clears MIN_SCORE on its own,
# exactly as a single unambiguous stopword hit always has.
_HIT_WEIGHT = 2


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def detect(title: str, author: str | None = None) -> str | None:
    """Guess an ISO 639-1 code for `title`, or None when nothing is decisive.

    `author` is accepted for interface stability (the normalize stage always has
    one on hand) but is not used: a person's name does not reliably indicate the
    language a book was written in, and guessing from it would trade conservatism
    for coverage — exactly the tradeoff this module must not make.
    """
    words = set(_WORD.findall(_fold(title or "")))
    lowered = (title or "").lower()
    scores: dict[str, int] = {}
    for code in SUPPORTED:
        stopword_hits = len(words & _STOPWORDS[code])
        hint_hits = sum(1 for c in _HINT_CHARS.get(code, "") if c in lowered)
        score = _HIT_WEIGHT * (stopword_hits + hint_hits)
        if score:
            scores[code] = score
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    if best_score < MIN_SCORE or best_score - runner_up < MIN_MARGIN:
        return None
    return best
