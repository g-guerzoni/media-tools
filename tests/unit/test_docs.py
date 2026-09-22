import re
from pathlib import Path

from media_tools.tasks.formats import as_markdown, collect

MARKER = re.compile(r"<!-- formats:start -->\n(.*?)\n<!-- formats:end -->", re.DOTALL)
DOCS = [Path("README.md"), Path("CLAUDE.md")]


def test_docs_embed_the_generated_formats_table():
    expected = as_markdown(collect()).strip()
    for doc in DOCS:
        match = MARKER.search(doc.read_text(encoding="utf-8"))
        assert match, f"{doc} has no formats markers"
        assert match.group(1).strip() == expected, f"{doc} formats table is stale"


def test_docs_document_the_list_file_shapes():
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for shape in ('["https://', '{"url"', '{"urls"'):
            assert shape in text, f"{doc} is missing the {shape!r} list-file shape"


def test_docs_have_no_local_or_personal_paths():
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        assert "/Users/" not in text
        assert "Downloads/Livros" not in text
