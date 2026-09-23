import re
from pathlib import Path

from media_tools.core.events import ERROR_CODES, ITEM_STATUSES, REASONS, WARNING_CODES
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
    """Widest of the three, on purpose. The formats-marker and list-file-shape tests
    above are about README.md/CLAUDE.md specifically, but `docs/*.md` — and
    `kindle-first-run.md` above all — is meant to be FOLLOWED verbatim on a machine
    with a real device attached, which is exactly the session where a real path gets
    pasted in while checking whether a step worked."""
    for doc in DOCS + sorted(Path("docs").glob("*.md")):
        text = doc.read_text(encoding="utf-8")
        assert "/Users/" not in text, doc
        assert "/home/" not in text, doc
        assert "~/Downloads" not in text, doc


# --- the closed registry, pinned against the doc that calls itself authoritative ---

# The registry in `core/events.py` is CLOSED — `Reporter` raises on anything not in it
# — and CLAUDE.md's "The closed code registry" section says so and then lists every
# name by hand. Nothing checked that the two agreed, and this plan already shipped two
# code names that did not exist, so the drift is not hypothetical.
_BACKTICKED = re.compile(r"`([a-z0-9_]+)`")
_STATUS_ANCHOR = "Item statuses (`item.status`, and `run.json`'s per-item `status`):"
_WARNING_ANCHOR = (
    "`warning` code (on an `item` event's `warnings`, or a standalone `warning` event):"
)
_TABLE_ROW = re.compile(r"^\| `([a-z0-9_]+)` \|", re.MULTILINE)


def _paragraph_after(text: str, anchor: str) -> str:
    """The first blank-line-delimited block following `anchor`."""
    assert anchor in text, f"CLAUDE.md no longer contains the anchor {anchor!r}"
    rest = text.split(anchor, 1)[1].lstrip("\n")
    return rest.split("\n\n", 1)[0]


def test_claude_md_lists_exactly_the_registry_in_events_py():
    """Every name in the closed registry appears in CLAUDE.md, and CLAUDE.md invents
    none. A code the doc names and the code does not is an agent emitting something
    `Reporter` raises on; a code the registry has and the doc omits is a consumer with
    no idea it can arrive."""
    text = Path("CLAUDE.md").read_text(encoding="utf-8")

    statuses = set(_BACKTICKED.findall(_paragraph_after(text, _STATUS_ANCHOR)))
    assert statuses == set(ITEM_STATUSES)

    reasons = set(
        _TABLE_ROW.findall(
            _paragraph_after(text, "`reason` (on an `item` event, and in `run.json`):")
        )
    )
    assert reasons == set(REASONS)

    errors = set(_BACKTICKED.findall(_paragraph_after(text, "`error` event `code`:")))
    assert errors == set(ERROR_CODES)

    warnings = set(_BACKTICKED.findall(_paragraph_after(text, _WARNING_ANCHOR)))
    assert warnings == set(WARNING_CODES)
