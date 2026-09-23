"""Read a book's title and author from its filename.

This library's filenames follow two conventions and are far more reliable than the
embedded metadata, which is often the packager's name, a URL, or a temp filename:

- English: "Lastname, First - Title" (e.g. "Vinge, Vernor - A Deepness in the Sky")
- Portuguese: "Title - Author" (e.g. "Cidade de Deus - Paulo Lins")
"""

from __future__ import annotations

import re
from pathlib import Path

# Words that never begin a person's name, so a trailing segment starting with one
# is part of the title (English + Portuguese).
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "and",
        "or",
        "to",
        "in",
        "on",
        "for",
        "from",
        "o",
        "os",
        "as",
        "um",
        "uma",
        "de",
        "da",
        "do",
        "dos",
        "das",
        "e",
        "para",
        "com",
    }
)
_DOC_SUFFIXES = (".doc", ".docx", ".pdf", ".rtf", ".odt", ".txt")
_TMP_NAME = re.compile(r"^tmp[0-9a-f]{2,}", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


def clean_stem(path: Path) -> str:
    """Normalise a filename's stem for parsing: underscores become spaces, runs of
    whitespace collapse to one. Periods are left alone — this library's Portuguese
    author segments routinely carry them (e.g. "R. L. Stine"), so blindly stripping
    them would mangle a perfectly good name."""
    stem = path.stem.replace("_", " ")
    return _WHITESPACE.sub(" ", stem).strip()


def _tidy(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().strip("-").strip()


def _reorder_comma(name: str) -> str:
    """'Lastname, First' -> 'First Lastname'; anything else is returned as-is."""
    if "," in name:
        last, first = name.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return name.strip()


def _looks_like_name(segment: str) -> bool:
    tokens = segment.strip().strip("-").strip().split()
    if not (1 <= len(tokens) <= 4):
        return False
    if any(any(c.isdigit() for c in token) for token in tokens):
        return False
    if tokens[0].lower() in _STOPWORDS:
        return False
    return any(token[:1].isupper() for token in tokens)


def parse_filename(path: Path) -> tuple[str, str | None]:
    stem = clean_stem(path)
    parts = [part.strip() for part in stem.split(" - ")]
    if len(parts) == 1:
        return _tidy(parts[0]), None

    first = parts[0]
    # English convention: "Lastname, First - Title"
    if "," in first and len(first.split(",")[0].split()) <= 3:
        return _tidy(" - ".join(parts[1:])) or stem, _reorder_comma(first)

    # Portuguese convention: "Title - Author"
    if _looks_like_name(parts[-1]):
        return _tidy(" - ".join(parts[:-1])) or stem, _tidy(parts[-1])

    return _tidy(stem), None


def is_junk_title(title: str) -> bool:
    if any(ord(c) < 32 for c in title):
        return True
    low = title.strip().lower()
    if not low or low.startswith("microsoft word") or _TMP_NAME.match(low):
        return True
    if ".indd" in low or ".htm" in low or "#" in title:
        return True
    return low.endswith(_DOC_SUFFIXES)


def is_junk_author(author: str) -> bool:
    low = (author or "").strip().lower()
    if not low or low == "unknown" or "microsoft word" in low:
        return True
    if low.endswith(_DOC_SUFFIXES) or any(c in author for c in "[]{}<>|"):
        return True
    tokens = low.split()
    # Mangled metadata repeats a token: "Isabel] Isabel Allende [Allende"
    return len(tokens) > 2 and len(tokens) != len(set(tokens))
