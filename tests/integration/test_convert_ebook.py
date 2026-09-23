import json
import subprocess
import sys

from media_tools.tasks.ebook import exth
from tests.conftest import requires_calibre

pytestmark = requires_calibre


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "media_tools", *args], capture_output=True, text=True
    )


def test_epub_to_azw3(make_epub, tmp_path):
    book = make_epub(title="Converted", author="Someone")
    out = tmp_path / "media"
    result = _cli("convert", str(book), "--to", "azw3", "-o", str(out), "-b", "one")
    assert result.returncode == 0
    produced = out / "one" / "Converted.azw3"
    assert produced.is_file()
    assert exth.record_text(exth.read_records(produced), exth.TAG_TITLE) == "Converted"


def test_same_format_is_skipped(make_epub, tmp_path):
    book = make_epub(title="Already")
    out = tmp_path / "media"
    result = _cli("convert", str(book), "--to", "epub", "-o", str(out), "-b", "same", "--json")
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    item = next(e for e in events if e["type"] == "item")
    assert item["status"] == "skipped"
    assert item["reason"] == "already_target_format"


def test_a_broken_book_fails_only_that_item(make_epub, tmp_path):
    folder = make_epub(title="Good", name="good").parent
    (folder / "bad.epub").write_bytes(b"not an epub")
    out = tmp_path / "media"
    result = _cli("convert", str(folder), "--to", "azw3", "-o", str(out), "-b", "mixed", "--json")
    assert result.returncode == 1
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    statuses = {e["input"].split("/")[-1]: e["status"] for e in events if e["type"] == "item"}
    assert statuses["bad.epub"] == "failed"
    assert statuses["good.epub"] == "done"
    assert events[-1]["type"] == "result"


def test_formats_lists_the_ebook_engine():
    rows = json.loads(_cli("formats", "--json").stdout)["formats"]
    ebook = next(r for r in rows if r["task"] == "convert" and r["engine"] == "ebook")
    assert "azw3" in ebook["outputs"] and ".epub" in ebook["inputs"]
