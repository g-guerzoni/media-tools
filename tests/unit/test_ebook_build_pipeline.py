"""Unit-level coverage for `tasks/ebook/build.py`'s internal stage-wiring and
per-item helpers — fast, offline, no Calibre required. The end-to-end regressions
(a real rename + metadata rewrite, a real OSError mid-batch) live in
`tests/integration/test_ebook_build.py` instead, since those need a real Calibre
install to be a meaningful proof."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from media_tools.integrations import calibre as calibre_mod
from media_tools.tasks.ebook import build
from media_tools.tasks.ebook.dedup import Group
from media_tools.tasks.ebook.metadata import BookFacts
from media_tools.tasks.ebook.normalize import Verdict


def _facts(path: Path) -> BookFacts:
    return BookFacts(
        path=path,
        size=10,
        fmt=path.suffix.lstrip("."),
        meta_title=None,
        meta_author=None,
        meta_language=None,
        meta_uuid=None,
        has_cover=False,
        file_title=path.stem,
        file_author=None,
    )


def _plan(verdicts: dict[Path, Verdict], groups: list[Group]) -> build._Plan:
    return build._Plan(
        facts_by_path={p: _facts(p) for p in verdicts},
        dropped={},
        verdicts=verdicts,
        groups=groups,
        normalize_stats={
            "llm_calls": 0,
            "cache_hits": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "errors": 0,
        },
        dedup_stats={"llm_calls": 0, "buckets": 0, "merges": 0, "errors": 0, "blocked_merges": 0},
        cover_results={},
        book_ids={p: f"id-{p.name}" for p in verdicts},
    )


# -- C1: _verify_output had NO test at all before this fix --------------------------


def test_verify_output_passes_once_a_renamed_book_is_honest(monkeypatch, tmp_path):
    """The renamed case: after RB20's fix, a file reconcile renamed into place has
    ALSO had its embedded metadata rewritten to match — so `_verify_output` (which
    compares the file's embedded title against the plan's verdict) must pass on
    its own terms, not merely because the check was skipped."""
    target = tmp_path / "book.epub"
    target.write_bytes(b"x")

    def fake_read_metadata(path, *, cache_dir, timeout=120):
        return calibre_mod.BookMetadata(
            title="New Title", author="An Author", language="en", uuid="id-1", has_cover=True
        )

    monkeypatch.setattr(calibre_mod, "read_metadata", fake_read_metadata)
    verdict = Verdict("ok", "New Title", "An Author", "en", "list", "list")

    ok, warnings, title_mismatch = build._verify_output(target, verdict, tmp_path / "cache")

    assert ok is True
    assert warnings == []
    assert title_mismatch is False


def test_verify_output_fails_on_a_genuine_mismatch(monkeypatch, tmp_path):
    target = tmp_path / "book.azw3"
    target.write_bytes(b"x")

    def fake_read_metadata(path, *, cache_dir, timeout=120):
        return calibre_mod.BookMetadata(
            title="A Totally Different Title",
            author=None,
            language=None,
            uuid=None,
            has_cover=False,
        )

    monkeypatch.setattr(calibre_mod, "read_metadata", fake_read_metadata)
    verdict = Verdict("ok", "The Real Title", "An Author", "en", "heuristic", "heuristic")

    ok, _warnings, title_mismatch = build._verify_output(target, verdict, tmp_path / "cache")

    assert ok is False
    assert title_mismatch is True  # a readable file, just with the wrong title


def test_verify_output_does_not_flag_title_mismatch_when_the_file_is_unreadable(
    monkeypatch, tmp_path
):
    # RB23's self-heal repairs a title MISMATCH specifically; an unreadable file is
    # a different class of problem and must not be flagged as repair-eligible.
    target = tmp_path / "book.azw3"
    target.write_bytes(b"x")

    def fake_read_metadata(path, *, cache_dir, timeout=120):
        raise calibre_mod.CalibreError("boom")

    monkeypatch.setattr(calibre_mod, "read_metadata", fake_read_metadata)
    verdict = Verdict("ok", "The Real Title", "An Author", "en", "heuristic", "heuristic")

    ok, _warnings, title_mismatch = build._verify_output(target, verdict, tmp_path / "cache")

    assert ok is False
    assert title_mismatch is False


# -- M5: cover-offset presence is a key check, not a UTF-8 decode -------------------


def test_verify_output_checks_cover_offset_presence_by_key_not_decode(monkeypatch, tmp_path):
    target = tmp_path / "book.azw3"
    target.write_bytes(b"x")

    def fake_read_metadata(path, *, cache_dir, timeout=120):
        return calibre_mod.BookMetadata(
            title="Title", author=None, language="en", uuid="uuid-1", has_cover=True
        )

    monkeypatch.setattr(calibre_mod, "read_metadata", fake_read_metadata)
    from media_tools.tasks.ebook import exth

    # Genuine binary (non-UTF-8-decodable-as-text-meaningfully, but present) offset
    # values: presence must be judged by the key existing, not by decoding it.
    fake_records = {
        exth.TAG_UUID: b"uuid-1",
        exth.TAG_COVER_OFFSET: b"\xff\xfe\x00\x01",
        exth.TAG_THUMB_OFFSET: b"\xff\xfe\x00\x02",
    }
    monkeypatch.setattr(exth, "read_records", lambda path: fake_records)

    verdict = Verdict("ok", "Title", None, "en", "heuristic", "heuristic")
    ok, warnings, _title_mismatch = build._verify_output(target, verdict, tmp_path / "cache")

    assert ok is True
    assert "cover_not_embedded" not in warnings


def test_verify_output_reports_cover_not_embedded_when_the_records_are_absent(
    monkeypatch, tmp_path
):
    target = tmp_path / "book.azw3"
    target.write_bytes(b"x")

    def fake_read_metadata(path, *, cache_dir, timeout=120):
        return calibre_mod.BookMetadata(
            title="Title", author=None, language="en", uuid="uuid-1", has_cover=False
        )

    monkeypatch.setattr(calibre_mod, "read_metadata", fake_read_metadata)
    from media_tools.tasks.ebook import exth

    monkeypatch.setattr(exth, "read_records", lambda path: {exth.TAG_UUID: b"uuid-1"})

    verdict = Verdict("ok", "Title", None, "en", "heuristic", "heuristic")
    ok, warnings, _title_mismatch = build._verify_output(target, verdict, tmp_path / "cache")

    assert ok is True
    assert warnings == ["cover_not_embedded"]


# -- C2: an unexpected exception from _convert_one must fail only that one book -----


def test_convert_missing_books_isolates_an_unexpected_exception(monkeypatch, tmp_path):
    good = Path("/books/good.epub")
    bad = Path("/books/bad.epub")
    verdicts = {
        good: Verdict("ok", "Good", None, "en", "heuristic", "heuristic"),
        bad: Verdict("ok", "Bad", None, "en", "heuristic", "heuristic"),
    }
    groups = [
        Group(members=[good], winner=good, language="en"),
        Group(members=[bad], winner=bad, language="en"),
    ]
    plan = _plan(verdicts, groups)
    convert_plan = {
        "missing": {good, bad},
        "planned": {good: tmp_path / "good.azw3", bad: tmp_path / "bad.azw3"},
    }

    def fake_convert_one(source, target, **kwargs):
        if source == bad:
            raise OSError(28, "No space left on device")
        return None

    monkeypatch.setattr(build, "_convert_one", fake_convert_one)

    errors = build._convert_missing_books(
        plan, convert_plan, cache_dir=tmp_path, to="azw3", workers=2
    )

    assert set(errors) == {bad}
    assert "No space left" in errors[bad]


# -- I5: each stage fires as it begins, with real progress events in between -------


class _SpyReporter:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def stage(self, *, stage, index, count):
        self.events.append(("stage", stage))

    def progress(self, *, stage, index, count, path, percent, eta_s=None):
        self.events.append(("progress", stage))


def test_build_plan_emits_progress_between_stage_announcements(monkeypatch, tmp_path):
    paths = [Path("/books/a.epub"), Path("/books/b.epub")]

    def fake_read_all(paths, *, cache_dir, workers, on_error=None, on_progress=None):
        facts = [_facts(p) for p in paths]
        if on_progress:
            for index, f in enumerate(facts, start=1):
                on_progress(index, len(facts), f.path)
        return facts

    def fake_classify(facts, *, model, api_key, cache_dir, on_progress=None, **kwargs):
        if on_progress:
            on_progress(1, 1)
        from media_tools.tasks.ebook.normalize import Verdict as V

        return {f.path: V("ok", f.path.stem, None, "en", "llm", "llm") for f in facts}, {
            "llm_calls": 1,
            "cache_hits": 0,
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "errors": 0,
        }

    def fake_resolve(books, *, cache_dir, fetch, workers, on_progress=None, **kwargs):
        if on_progress:
            on_progress(1, len(books), "extract")
        from media_tools.tasks.ebook.covers import CoverResult

        return {source: CoverResult(path=None, source="none") for source in books}

    monkeypatch.setattr(build.metadata_stage, "read_all", fake_read_all)
    monkeypatch.setattr(build.normalize_stage, "classify", fake_classify)
    monkeypatch.setattr(build.covers_stage, "resolve", fake_resolve)
    monkeypatch.setattr(build.opf, "book_id", lambda source: f"id-{source.name}")

    spy = _SpyReporter()
    args = SimpleNamespace(model="m", no_cover_fetch=False, workers=None)
    build._build_plan(
        paths,
        {},
        args=args,
        stop_after="organize",
        dry_run=False,
        llm_enabled=True,
        api_key="k",
        cache_dir=tmp_path,
        prefer=("epub",),
        reporter=spy,
        stages=list(build.STAGE_ORDER),
    )

    kinds = [kind for kind, _name in spy.events]
    names = [name for _kind, name in spy.events]

    def index_of(kind, name):
        return spy.events.index((kind, name))

    assert index_of("stage", "metadata") < index_of("progress", "metadata")
    assert index_of("progress", "metadata") < index_of("stage", "normalize")
    assert index_of("stage", "normalize") < index_of("progress", "normalize")
    assert index_of("progress", "normalize") < index_of("stage", "covers")
    assert index_of("stage", "covers") < index_of("progress", "covers")
    assert kinds.count("stage") >= 4  # scan, metadata, normalize, (dedup,) covers
    assert "dedup" not in names or index_of("stage", "dedup") < index_of("stage", "covers")


def test_build_plan_never_announces_a_stage_outside_this_runs_declared_list(monkeypatch, tmp_path):
    """A subcommand that stops after "normalize" never lists "dedup"/"covers" in
    `start`'s own `stages` — announcing one anyway would violate the JSON event
    contract documented in CLAUDE.md (every `stage` event must be one of the names
    `start` declared)."""
    paths = [Path("/books/a.epub")]

    def fake_read_all(paths, *, cache_dir, workers, on_error=None, on_progress=None):
        return [_facts(p) for p in paths]

    monkeypatch.setattr(build.metadata_stage, "read_all", fake_read_all)

    spy = _SpyReporter()
    args = SimpleNamespace(model="m", no_cover_fetch=False, workers=None)
    build._build_plan(
        paths,
        {},
        args=args,
        stop_after="normalize",
        dry_run=False,
        llm_enabled=False,
        api_key=None,
        cache_dir=tmp_path,
        prefer=("epub",),
        reporter=spy,
        stages=["scan", "metadata", "normalize"],
    )

    announced = {name for kind, name in spy.events if kind == "stage"}
    assert announced <= {"scan", "metadata", "normalize"}


# -- M2: the dry-run preview must use plan_placement, and honour --summary-json -----


class _RecordingReporter:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.results: list[dict] = []

    def item(self, **kwargs):
        self.items.append(kwargs)

    def result(self, **kwargs):
        self.results.append(kwargs)


def test_dry_run_preview_uses_plan_placement_so_collisions_get_suffixed(tmp_path):
    verdict_a = Verdict("ok", "AC/DC Story", "X", "en", "heuristic", "title_heuristic")
    verdict_b = Verdict("ok", "AC:DC Story", "X", "en", "heuristic", "title_heuristic")
    source_a = Path("/books/a.epub")
    source_b = Path("/books/b.epub")
    groups = [
        Group(members=[source_a], winner=source_a, language="en"),
        Group(members=[source_b], winner=source_b, language="en"),
    ]
    plan = _plan({source_a: verdict_a, source_b: verdict_b}, groups)

    reporter = _RecordingReporter()
    args = SimpleNamespace(summary_json=None)
    build._report_dry_run(
        plan,
        reporter=reporter,
        batch_dir=tmp_path,
        options={"to": "azw3"},
        stop_after="organize",
        args=args,
    )

    suffixed = [i for i in reporter.items if i["outputs"] and "(2)" in str(i["outputs"][0])]
    assert len(suffixed) == 1
    assert suffixed[0]["warnings"] == ["name_collision_suffixed"]
    plain = [i for i in reporter.items if i is not suffixed[0]]
    assert all(not w for i in plain for w in [i.get("warnings") or []])


def test_dry_run_honours_summary_json(tmp_path):
    source = Path("/books/a.epub")
    verdict = Verdict("ok", "Solo", "X", "en", "heuristic", "title_heuristic")
    groups = [Group(members=[source], winner=source, language="en")]
    plan = _plan({source: verdict}, groups)

    reporter = _RecordingReporter()
    summary = tmp_path / "summary.json"
    args = SimpleNamespace(summary_json=summary)
    build._report_dry_run(
        plan,
        reporter=reporter,
        batch_dir=tmp_path,
        options={"to": "azw3"},
        stop_after="organize",
        args=args,
    )

    assert summary.is_file()
    import json

    payload = json.loads(summary.read_text())
    assert payload["type"] == "result"
    assert payload["exit_code"] == 0
