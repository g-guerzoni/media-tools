"""Guess a book's language from its title, offline.

Deliberately conservative: a title that does not clearly belong to one of the
supported languages returns None, so the book lands in the review folder instead
of the wrong shelf. With the LLM enabled, the model answers instead; this module
is the offline path and the fallback when the model abstains.
"""

from __future__ import annotations

import re
import unicodedata

SUPPORTED = ("en", "pt", "es", "it", "fr", "de", "pl")

_STOPWORDS = {
    "en": frozenset(
        {
            "the",
            "of",
            "and",
            "in",
            "on",
            "to",
            "a",
            "an",
            "for",
            "from",
            "with",
            "his",
            "her",
            "is",
            "at",
        }
    ),
    "pt": frozenset(
        {
            "de",
            "da",
            "do",
            "dos",
            "das",
            "e",
            "o",
            "a",
            "os",
            "as",
            "um",
            "uma",
            "no",
            "na",
            "nos",
            "nas",
            "para",
            "com",
            "que",
        }
    ),
    "es": frozenset(
        {
            "el",
            "la",
            "los",
            "las",
            "del",
            "y",
            "en",
            "un",
            "una",
            "por",
            "para",
            "con",
            "que",
        }
    ),
    "it": frozenset(
        {
            "il",
            "lo",
            "la",
            "i",
            "gli",
            "le",
            "dei",
            "della",
            "e",
            "un",
            "una",
            "nel",
            "con",
            "che",
        }
    ),
    "fr": frozenset(
        {
            "le",
            "la",
            "les",
            "des",
            "du",
            "et",
            "un",
            "une",
            "dans",
            "sur",
            "pour",
            "avec",
            "qui",
        }
    ),
    "de": frozenset(
        {"der", "die", "das", "und", "des", "ein", "eine", "im", "von", "zu", "mit", "den", "dem"}
    ),
    "pl": frozenset({"i", "w", "na", "z", "do", "nie", "się", "od", "po", "za"}),
}

# Words common enough across Romance languages that, alone, they are not decisive —
# they only confirm a language once one of that language's own (less ambiguous)
# stopwords has already matched. "de" already sits in `_STOPWORDS["pt"]` above and
# is not shared with any other language's set there, so it stays a fully decisive,
# standalone signal for Portuguese (e.g. a bare "de" is enough: see
# test_detects_the_obvious_cases and the accent-invariance test). For French, "de"
# would otherwise tie a Portuguese-only title 1-for-1 (both match nothing but "de"),
# which is exactly the kind of shared-vocabulary collision this module must not
# guess through — so it counts for French only once a French-specific stopword
# (le/la/les/un/une/...) has already matched, the same way "La Sombra del Viento"
# is only decisive for Spanish because "del" backs up the "la" it shares with
# Italian and French.
_CONFIRMING = {
    "fr": frozenset({"de"}),
}

# Characters that only a few languages use; weaker than a stopword but decisive
# for a short title.
_HINT_CHARS = {
    "pt": "ãõç",
    "es": "ñ¿¡",
    "de": "ßüöä",
    "pl": "łżźćęąś",
    "fr": "çœàèùâêî",
    "it": "àèìòù",
}

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
MIN_SCORE = 2
MIN_MARGIN = 1


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
        primary_hits = words & _STOPWORDS[code]
        confirming = _CONFIRMING.get(code, frozenset())
        confirming_hits = (words & confirming) if primary_hits else frozenset()
        stopword_hits = len(primary_hits) + len(confirming_hits)
        hint_hits = sum(1 for c in _HINT_CHARS.get(code, "") if c in lowered)
        score = 2 * stopword_hits + hint_hits
        if score:
            scores[code] = score
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    # Portuguese and Spanish share several stopwords: when they (or any pair) tie
    # within the margin, say nothing rather than guess.
    if best_score < MIN_SCORE or best_score - runner_up < MIN_MARGIN:
        return None
    return best
