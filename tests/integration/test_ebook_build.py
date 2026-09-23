import json
import subprocess
import sys

import pytest

from media_tools.integrations import calibre
from media_tools.tasks.ebook import exth

pytestmark = pytest.mark.skipif(
    calibre.find_tool("ebook-convert") is None, reason="Calibre is not installed"
)


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "media_tools", *args], capture_output=True, text=True
    )


def test_build_offline_produces_a_library_by_language(make_epub, tmp_path):
    books = make_epub(
        title="Dom Casmurro",
        author="Machado de Assis",
        language="pt",
        name="Dom Casmurro - Machado de Assis",
    ).parent
    make_epub(
        title="The Blade Itself",
        author="Joe Abercrombie",
        language="en",
        name="The Blade Itself - Joe Abercrombie",
    )
    out = tmp_path / "media"

    result = _cli(
        "ebook",
        "build",
        str(books),
        "--no-llm",
        "-o",
        str(out),
        "-b",
        "lib",
        "--no-cover-fetch",
        "--json",
    )
    assert result.returncode == 0
    assert (out / "lib" / "pt" / "Dom Casmurro - Machado de Assis.azw3").is_file()
    assert (out / "lib" / "en" / "The Blade Itself - Joe Abercrombie.azw3").is_file()

    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert events[-1]["type"] == "result"
    stages = [e["stage"] for e in events if e["type"] == "stage"]
    assert stages[0] == "scan" and stages[-1] == "organize"


def test_a_rebuild_converts_nothing_new(make_epub, tmp_path):
    books = make_epub(
        title="Stable", author="An Author", language="en", name="Stable - An Author"
    ).parent
    out = tmp_path / "media"
    _cli("ebook", "build", str(books), "--no-llm", "-o", str(out), "-b", "lib", "--no-cover-fetch")
    result = _cli(
        "ebook",
        "build",
        str(books),
        "--no-llm",
        "-o",
        str(out),
        "-b",
        "lib",
        "--no-cover-fetch",
        "--json",
    )
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    items = [e for e in events if e["type"] == "item"]
    assert all(i["status"] == "skipped" for i in items)


def test_duplicates_are_not_converted(make_epub, tmp_path):
    folder = make_epub(
        title="Twice", author="An Author", language="en", name="Twice - An Author"
    ).parent
    duplicate = folder / "Twice - An Author.mobi"
    calibre.convert(
        folder / "Twice - An Author.epub",
        duplicate,
        opf=None,
        cover=None,
        cache_dir=tmp_path / "cache",
    )
    out = tmp_path / "media"

    result = _cli(
        "ebook",
        "build",
        str(folder),
        "--no-llm",
        "-o",
        str(out),
        "-b",
        "dedup",
        "--no-cover-fetch",
        "--json",
    )
    assert result.returncode == 0
    produced = list((out / "dedup").rglob("*.azw3"))
    assert len(produced) == 1
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    item = next(e for e in events if e["type"] == "item" and e["status"] == "done")
    assert item["input"].endswith(".mobi"), "mobi outranks epub in the default preference"


def test_the_output_carries_the_clean_name_and_a_stable_id(make_epub, tmp_path):
    folder = make_epub(
        title="messy title", author="an author", language="en", name="Clean Name - Real Author"
    ).parent
    out = tmp_path / "media"
    _cli(
        "ebook", "build", str(folder), "--no-llm", "-o", str(out), "-b", "names", "--no-cover-fetch"
    )
    book = out / "names" / "en" / "Clean Name - Real Author.azw3"
    assert book.is_file()
    records = exth.read_records(book)
    assert exth.record_text(records, exth.TAG_TITLE) == "Clean Name"
    assert exth.record_text(records, exth.TAG_UUID)
