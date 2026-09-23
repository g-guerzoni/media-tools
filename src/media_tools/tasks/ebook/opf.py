"""The per-book metadata descriptor Calibre converts from.

Passing --from-opf is the only way to make ebook-convert write a chosen, stable
identifier into the output's EXTH 113 record; the individual --title/--authors
flags cannot set it.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # RFC 4122 DNS namespace
_CHUNK = 1024 * 1024

_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uuid_id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" \
xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>{title}</dc:title>
{creator}    <dc:language>{language}</dc:language>
    <dc:identifier id="uuid_id" opf:scheme="uuid">{uuid}</dc:identifier>
  </metadata>
</package>
"""


def book_id(source: Path) -> str:
    """A UUID derived from the file's contents: same book, same id, on any machine."""
    digest = hashlib.sha256()
    with open(source, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return str(uuid.uuid5(_NAMESPACE, digest.hexdigest()))


def write_opf(
    path: Path, *, title: str, author: str | None, language: str | None, book_uuid: str
) -> None:
    creator = f'    <dc:creator opf:role="aut">{escape(author)}</dc:creator>\n' if author else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _TEMPLATE.format(
            title=escape(title),
            creator=creator,
            language=escape(language or "und"),
            uuid=escape(book_uuid),
        ),
        encoding="utf-8",
    )
