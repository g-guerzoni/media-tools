import json
import os
import subprocess
import sys
from pathlib import Path

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

    run_data = json.loads((out / "dedup" / "run.json").read_text())
    (winner,) = [i for i in run_data["items"] if i["status"] == "done"]
    assert winner["data"]["duplicates"] == [str(folder / "Twice - An Author.epub")]


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


def test_scan_persists_the_inventory_without_requiring_a_key(make_epub, tmp_path):
    """Fix round 1, FIX 3: `scan` runs scan/metadata/normalize/dedup and persists the
    inventory to the batch's run.json (so `status` can find it afterward) — and,
    unlike every other subcommand, never blocks on a missing OpenRouter key: with no
    `--no-llm` and no key configured, it silently falls back to the offline
    heuristic instead of exiting 3 with config_missing."""
    folder = make_epub(
        title="Scanned Book", author="Some Author", language="en", name="Scanned Book - Some Author"
    ).parent
    out = tmp_path / "media"
    env = {**os.environ}
    env.pop("OPENROUTER_API_KEY", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "media_tools",
            "ebook",
            "scan",
            str(folder),
            "-o",
            str(out),
            "-b",
            "scanned",
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert not any(e["type"] == "error" for e in events)
    assert events[-1]["type"] == "result"

    # scan converts nothing and writes no output files.
    assert not any(out.rglob("*.azw3"))

    run_file = out / "scanned" / "run.json"
    assert run_file.is_file()
    data = json.loads(run_file.read_text())
    (item,) = data["items"]
    assert item["status"] == "done"
    assert item["data"]["title"] == "Scanned Book"
    assert item["data"]["author"] == "Some Author"
    assert item["data"]["language"] == "en"
    assert item["data"]["duplicates"] == []
    assert item["data"]["output"] is None

    status = _cli("status", "scanned", "-o", str(out), "--json")
    assert status.returncode == 0
    payload = json.loads(status.stdout)
    assert payload["batch"]["batch"] == "scanned"
    assert payload["batch"]["counts"]["done"] == 1


# -- C1: a renamed book must be honest — its embedded metadata rewritten too, and
# it must never report failed just because it was renamed instead of reconverted --


def test_a_retitled_book_is_renamed_rewritten_and_never_reported_failed(make_epub, tmp_path):
    """The reviewer's exact reproduction: retitling one book through `--list`
    renames the file correctly on disk but used to report `failed`/`engine_error`
    forever afterward, because `_verify_output` compared the file's still-OLD
    embedded title against the NEW verdict. RB20 fixes this by also rewriting the
    file's embedded metadata when it is renamed, so the file genuinely matches the
    plan — this must converge, not fail on every rerun."""
    book = make_epub(
        title="Old Title", author="Jane Smith", language="en", name="Old Title - Jane Smith"
    )
    out = tmp_path / "media"

    first = _cli(
        "ebook",
        "build",
        str(book),
        "--no-llm",
        "-o",
        str(out),
        "-b",
        "rt",
        "--no-cover-fetch",
        "--json",
    )
    assert first.returncode == 0
    old_target = out / "rt" / "en" / "Old Title - Jane Smith.azw3"
    assert old_target.is_file()

    listing = tmp_path / "list.json"
    listing.write_text(
        json.dumps(
            [{"path": str(book), "title": "New Title", "author": "Jane Smith", "language": "en"}]
        )
    )

    second = _cli(
        "ebook",
        "build",
        "--list",
        str(listing),
        "--no-llm",
        "-o",
        str(out),
        "-b",
        "rt",
        "--no-cover-fetch",
        "--json",
    )
    assert second.returncode == 0
    new_target = out / "rt" / "en" / "New Title - Jane Smith.azw3"
    assert new_target.is_file() and not old_target.exists()

    events = [json.loads(line) for line in second.stdout.splitlines() if line.strip()]
    (item,) = [e for e in events if e["type"] == "item"]
    # A rename is free — no engine ran — so "skipped"/"exists" is the correct
    # status, exactly like a book already sitting at its target; what C1 fixes is
    # that this must never be "failed"/"engine_error" (the reviewer's reproduction:
    # it renamed correctly and STILL reported failed, forever).
    assert item["status"] == "skipped"
    assert item["reason"] == "exists"

    # the rename must be honest: the file's own embedded title now matches the plan.
    meta = calibre.read_metadata(new_target, cache_dir=tmp_path / "verify-cache")
    assert meta.title == "New Title"

    # and it must STAY converged on a third run — not fail forever, which is the
    # exact defect this fixes (reported "failed" on every rerun, not just once).
    third = _cli(
        "ebook",
        "build",
        "--list",
        str(listing),
        "--no-llm",
        "-o",
        str(out),
        "-b",
        "rt",
        "--no-cover-fetch",
        "--json",
    )
    assert third.returncode == 0
    events3 = [json.loads(line) for line in third.stdout.splitlines() if line.strip()]
    (item3,) = [e for e in events3 if e["type"] == "item"]
    assert item3["status"] == "skipped" and item3["reason"] == "exists"


# -- C2: an unexpected OSError during conversion must fail only that one book -------


def test_an_unexpected_oserror_fails_only_that_book_not_the_whole_batch(
    make_epub, tmp_path, monkeypatch, capsys
):
    """Reproduced by the reviewer with a simulated ENOSPC on the second of four
    books: three books converted, reported nowhere, run.json left status: failed
    with every item still pending. `_convert_one`'s OSErrors (mkdir/write_opf/
    mkstemp/fsync_replace — a full disk, a read-only mount) must be recorded as a
    per-item failure like any CalibreError, not propagate past the batch."""
    from media_tools.cli import build_parser
    from media_tools.tasks.ebook import build as build_mod

    titles = ["Alpha", "Beta", "Gamma", "Delta"]
    books_dir = None
    for title in titles:
        book = make_epub(title=title, author="Author", language="en", name=f"{title} - Author")
        books_dir = book.parent

    real_convert = calibre.convert

    def flaky_convert(src, dst, **kwargs):
        if "Beta" in str(src):
            raise OSError(28, "No space left on device")
        return real_convert(src, dst, **kwargs)

    monkeypatch.setattr(build_mod.calibre, "convert", flaky_convert)

    out = tmp_path / "media"
    args = build_parser().parse_args(
        [
            "ebook",
            "build",
            str(books_dir),
            "--no-llm",
            "-o",
            str(out),
            "-b",
            "enospc",
            "--no-cover-fetch",
            "--json",
        ]
    )
    exit_code = build_mod.run(args)
    assert exit_code == 1  # one item failed; the batch itself still finishes cleanly

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert events[-1]["type"] == "result"
    items = [e for e in events if e["type"] == "item"]
    assert len(items) == 4

    def title_of(item):
        return Path(item["input"]).stem.split(" - ")[0]

    statuses = {title_of(i): i["status"] for i in items}
    assert statuses["Beta"] == "failed"
    for title in ("Alpha", "Gamma", "Delta"):
        assert statuses[title] == "done"

    run_data = json.loads((out / "enospc" / "run.json").read_text())
    assert run_data["status"] == "failed"
    assert all(i["status"] != "pending" for i in run_data["items"])
    beta_item = next(i for i in run_data["items"] if "Beta" in i["input"])
    assert beta_item["reason"] == "engine_error"
    assert "No space left" in beta_item["data"]["error"]


# -- I3/I4: a leftover is reported by name, and both same-named leftovers survive --


def test_a_leftover_is_reported_and_two_same_named_files_both_survive(make_epub, tmp_path):
    (tmp_path / "en").mkdir()
    (tmp_path / "pt").mkdir()
    en_book = make_epub(
        title="Shared Title",
        author="Jane Smith",
        language="en",
        name="en/Shared Title - Jane Smith",
    )
    pt_book = make_epub(
        title="Shared Title",
        author="Jane Smith",
        language="pt",
        name="pt/Shared Title - Jane Smith",
    )
    out = tmp_path / "media"

    _cli(
        "ebook",
        "build",
        str(en_book),
        str(pt_book),
        "--no-llm",
        "-o",
        str(out),
        "-b",
        "lo",
        "--no-cover-fetch",
    )
    en_target = out / "lo" / "en" / "Shared Title - Jane Smith.azw3"
    pt_target = out / "lo" / "pt" / "Shared Title - Jane Smith.azw3"
    assert en_target.is_file() and pt_target.is_file()

    # Re-run against only the English book: both AZW3s now match nothing in the new
    # plan's language folder for the Portuguese one — it must become a leftover,
    # mirrored under its own relative path, not silently overwritten or dropped.
    result = _cli(
        "ebook",
        "build",
        str(en_book),
        "--no-llm",
        "-o",
        str(out),
        "-b",
        "lo",
        "--no-cover-fetch",
        "--json",
    )
    assert result.returncode == 0
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    warnings = [e for e in events if e["type"] == "warning"]
    assert any(w["code"] == "leftover_book" for w in warnings)

    (result_event,) = [e for e in events if e["type"] == "result"]
    assert result_event["data"]["placement"]["leftover"] == 1

    assert en_target.is_file()
    assert not pt_target.exists()
    assert (out / "lo" / "_leftover" / "pt" / "Shared Title - Jane Smith.azw3").is_file()


# -- I5: a multi-book build emits progress events between the stage events ---------


def test_a_multi_book_build_emits_progress_between_stage_events(make_epub, tmp_path):
    books_dir = None
    for title in ("First Book", "Second Book"):
        book = make_epub(title=title, author="An Author", language="en", name=title)
        books_dir = book.parent

    out = tmp_path / "media"
    result = _cli(
        "ebook",
        "build",
        str(books_dir),
        "--no-llm",
        "-o",
        str(out),
        "-b",
        "prog",
        "--no-cover-fetch",
        "--json",
    )
    assert result.returncode == 0
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    stage_positions = [i for i, e in enumerate(events) if e["type"] == "stage"]
    progress_positions = [i for i, e in enumerate(events) if e["type"] == "progress"]
    assert progress_positions, "no progress event was emitted at all"

    metadata_stage_at = next(i for i in stage_positions if events[i]["stage"] == "metadata")
    normalize_stage_at = next(i for i in stage_positions if events[i]["stage"] == "normalize")
    assert any(metadata_stage_at < p < normalize_stage_at for p in progress_positions), (
        "a progress event must land between the metadata and normalize stage events"
    )
