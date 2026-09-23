from pathlib import Path

import pytest

from media_tools.core.state import LOCK_FILENAME, BatchInUse, RunState
from media_tools.tasks.ebook import library
from media_tools.tasks.ebook.normalize import Verdict


def test_an_ok_book_lands_in_its_language_folder(tmp_path):
    verdict = Verdict("ok", "Dom Casmurro", "Machado de Assis", "pt", "llm")
    assert (
        library.target_path(tmp_path, verdict, "azw3")
        == tmp_path / "pt" / "Dom Casmurro - Machado de Assis.azw3"
    )


def test_an_unknown_language_goes_to_review(tmp_path):
    verdict = Verdict("ok", "Solaris", "Stanislaw Lem", None, "heuristic")
    assert (
        library.target_path(tmp_path, verdict, "azw3")
        == tmp_path / "_review" / "unknown-language" / "Solaris - Stanislaw Lem.azw3"
    )


def test_a_flagged_book_goes_to_its_review_folder(tmp_path):
    verdict = Verdict("invalid", "scan0001", None, None, "llm")
    assert (
        library.target_path(tmp_path, verdict, "azw3")
        == tmp_path / "_review" / "invalid" / "scan0001.azw3"
    )


def test_an_unidentified_book_still_gets_placed_in_review(tmp_path):
    verdict = Verdict("unidentified", "documento1", None, None, "llm")
    target = library.target_path(tmp_path, verdict, "azw3")
    assert target == tmp_path / "_review" / "unidentified" / "documento1.azw3"


def test_a_name_with_slashes_is_made_safe(tmp_path):
    verdict = Verdict("ok", "AC/DC: The Story", "Someone", "en", "llm")
    target = library.target_path(tmp_path, verdict, "azw3")
    assert "/" not in target.name
    assert target.parent == tmp_path / "en"


def test_a_very_long_title_is_truncated_without_losing_the_extension(tmp_path):
    verdict = Verdict("ok", "A" * 300, "Author", "en", "llm")
    target = library.target_path(tmp_path, verdict, "azw3")
    assert target.name.endswith(".azw3")
    assert len(target.name) <= library.MAX_FILENAME


def test_safe_filename_strips_filesystem_illegal_characters():
    assert "/" not in library.safe_filename("AC/DC")
    assert library.safe_filename("Normal Title") == "Normal Title"


def test_safe_filename_never_returns_empty():
    assert library.safe_filename("///") == "untitled"


def test_plan_placement_maps_each_source_to_its_target(tmp_path):
    verdict = Verdict("ok", "Solaris", "Stanislaw Lem", "en", "llm")
    source = Path("/books/solaris.epub")
    entries = {source: (verdict, "azw3", "id-1")}
    plan, collisions = library.plan_placement(tmp_path, entries)
    assert plan == {source: tmp_path / "en" / "Solaris - Stanislaw Lem.azw3"}
    assert collisions == {}


def test_colliding_titles_after_sanitising_get_distinct_targets(tmp_path):
    # "AC/DC Story" and "AC:DC Story" both lose their only distinguishing
    # character once sanitised, and would otherwise land on the same path.
    verdict_a = Verdict("ok", "AC/DC Story", "X", "en", "llm")
    verdict_b = Verdict("ok", "AC:DC Story", "X", "en", "llm")
    source_a = Path("/books/a.epub")
    source_b = Path("/books/b.epub")
    entries = {
        source_b: (verdict_b, "azw3", "id-b"),
        source_a: (verdict_a, "azw3", "id-a"),
    }

    plan, collisions = library.plan_placement(tmp_path, entries)

    assert plan[source_a] == tmp_path / "en" / "ACDC Story - X.azw3"
    assert plan[source_b] == tmp_path / "en" / "ACDC Story - X (2).azw3"
    assert collisions == {source_b: "name_collision_suffixed"}

    # Simulate the pipeline actually writing each book's converted output: both
    # must land on disk, distinct, with their own content intact.
    for source, target in plan.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("BOOK A CONTENT" if source == source_a else "BOOK B CONTENT")

    assert plan[source_a].read_text() == "BOOK A CONTENT"
    assert plan[source_b].read_text() == "BOOK B CONTENT"


def test_a_third_collision_gets_a_3_suffix(tmp_path):
    verdict = Verdict("ok", "Same Title", "Author", "en", "llm")
    sources = [Path(f"/books/{letter}.epub") for letter in ("a", "b", "c")]
    entries = {source: (verdict, "azw3", f"id-{i}") for i, source in enumerate(sources)}

    plan, collisions = library.plan_placement(tmp_path, entries)

    assert plan[sources[0]] == tmp_path / "en" / "Same Title - Author.azw3"
    assert plan[sources[1]] == tmp_path / "en" / "Same Title - Author (2).azw3"
    assert plan[sources[2]] == tmp_path / "en" / "Same Title - Author (3).azw3"
    assert collisions == {
        sources[1]: "name_collision_suffixed",
        sources[2]: "name_collision_suffixed",
    }


def test_collision_disambiguation_is_deterministic_regardless_of_dict_order(tmp_path):
    verdict = Verdict("ok", "Same Title", "Author", "en", "llm")
    sources = [Path(f"/books/{letter}.epub") for letter in ("a", "b", "c")]
    forward = {source: (verdict, "azw3", f"id-{i}") for i, source in enumerate(sources)}
    backward = dict(reversed(list(forward.items())))

    plan_forward, collisions_forward = library.plan_placement(tmp_path, forward)
    plan_backward, collisions_backward = library.plan_placement(tmp_path, backward)

    assert plan_forward == plan_backward
    assert collisions_forward == collisions_backward


def test_reconcile_renames_a_book_whose_title_changed(tmp_path):
    old = tmp_path / "pt" / "Cidade de Deus.azw3"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"book")
    source = Path("/books/cidade.epub")
    new = tmp_path / "pt" / "Cidade de Deus - Paulo Lins.azw3"

    report = library.reconcile(
        tmp_path,
        {source: new},
        {source: "id-1"},
        dry_run=False,
        id_reader=lambda path: "id-1" if path == old else None,
    )
    assert new.is_file() and not old.exists()
    assert report.renamed == 1 and report.missing == []
    assert report.renamed_sources == [source]


def test_reconcile_moves_files_that_left_the_list(tmp_path):
    stale = tmp_path / "en" / "Gone.azw3"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x")
    report = library.reconcile(tmp_path, {}, {}, dry_run=False, id_reader=lambda p: None)
    # I3: leftover mirrors the file's path relative to the batch, not flattened.
    assert (tmp_path / "_leftover" / "en" / "Gone.azw3").is_file()
    assert report.leftover == [stale]


def test_leftover_mirrors_the_relative_path_instead_of_flattening(tmp_path):
    # I3: flattening to `_leftover/<name>` silently destroyed one of two same-named
    # files from different language folders (`en/Title.azw3` vs `pt/Title.azw3`).
    en = tmp_path / "en" / "Title.azw3"
    en.parent.mkdir(parents=True)
    en.write_bytes(b"EN CONTENT")
    pt = tmp_path / "pt" / "Title.azw3"
    pt.parent.mkdir(parents=True)
    pt.write_bytes(b"PT CONTENT")

    report = library.reconcile(tmp_path, {}, {}, dry_run=False, id_reader=lambda p: None)

    assert (tmp_path / "_leftover" / "en" / "Title.azw3").read_bytes() == b"EN CONTENT"
    assert (tmp_path / "_leftover" / "pt" / "Title.azw3").read_bytes() == b"PT CONTENT"
    assert set(report.leftover) == {en, pt}


def test_dry_run_moves_nothing(tmp_path):
    stale = tmp_path / "en" / "Gone.azw3"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x")
    report = library.reconcile(tmp_path, {}, {}, dry_run=True, id_reader=lambda p: None)
    assert stale.is_file()
    assert not (tmp_path / "_leftover").exists()
    assert report.leftover == [stale]


def test_reconcile_does_not_move_a_book_it_just_renamed_into_leftover(tmp_path):
    old = tmp_path / "pt" / "Cidade de Deus.azw3"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"book")
    stale = tmp_path / "en" / "Gone.azw3"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x")
    source = Path("/books/cidade.epub")
    new = tmp_path / "pt" / "Cidade de Deus - Paulo Lins.azw3"

    report = library.reconcile(
        tmp_path,
        {source: new},
        {source: "id-1"},
        dry_run=False,
        id_reader=lambda path: "id-1" if path == old else None,
    )

    assert new.is_file()
    assert not (tmp_path / "_leftover" / "pt" / "Cidade de Deus.azw3").exists()
    assert (tmp_path / "_leftover" / "en" / "Gone.azw3").is_file()
    assert report.leftover == [stale]


def test_reconcile_never_claims_the_same_existing_file_for_two_targets(tmp_path):
    twin = tmp_path / "pt" / "Livro.azw3"
    twin.parent.mkdir(parents=True)
    twin.write_bytes(b"book")
    source_a = Path("/books/a.epub")
    source_b = Path("/books/b.epub")
    target_a = tmp_path / "pt" / "Livro A.azw3"
    target_b = tmp_path / "pt" / "Livro B.azw3"

    report = library.reconcile(
        tmp_path,
        {source_a: target_a, source_b: target_b},
        {source_a: "id-1", source_b: "id-1"},
        dry_run=False,
        id_reader=lambda path: "id-1" if path == twin else None,
    )

    assert report.renamed == 1
    assert report.missing == [source_b]
    assert target_a.is_file() and not target_b.exists()
    assert report.leftover == []


def test_reconcile_keeps_a_target_that_already_exists(tmp_path):
    target = tmp_path / "en" / "Book.azw3"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"already there")
    source = Path("/books/book.epub")

    report = library.reconcile(
        tmp_path, {source: target}, {source: "id-1"}, dry_run=False, id_reader=lambda p: None
    )
    assert report.kept == 1
    assert report.renamed == 0
    assert report.missing == []
    assert target.read_bytes() == b"already there"


def test_reconcile_does_not_disturb_the_batch_lock(tmp_path):
    """`reconcile()` walks the whole batch directory looking for existing books to
    keep, rename or sweep to `_leftover/` — it must never treat `RunState`'s own
    lock file as one of those. Before this exclusion, an `ebook build` run called
    `reconcile()` against the batch it had just locked, the lock matched no planned
    target, and `reconcile` moved it to `_leftover/.lock`: `release()` then unlinked
    a path that no longer existed (no error, since `missing_ok=True`), and a second
    `RunState.open()` on the same batch succeeded instead of raising `BatchInUse` —
    silently defeating the mutual-exclusion guarantee for the rest of the run."""
    state = RunState.open(tmp_path, task="ebook", options={}, inputs=[])
    lock = tmp_path / LOCK_FILENAME
    assert lock.is_file()

    library.reconcile(tmp_path, {}, {}, dry_run=False, id_reader=lambda p: None)

    assert lock.is_file(), "the lock must not be swept into _leftover/"
    assert not (tmp_path / library.LEFTOVER_DIR / LOCK_FILENAME).exists()

    with pytest.raises(BatchInUse):
        RunState.open(tmp_path, task="ebook", options={}, inputs=[])

    state.finish("done")


def test_force_skips_the_twin_lookup_and_reconverts_instead_of_renaming(tmp_path):
    # Minor finding: `--force` must skip the book-id twin lookup entirely, not just
    # the already-at-target check — otherwise a book reconcile can find under an
    # old name is silently renamed for free, defeating "redo items whose output
    # exists" for exactly the book `--force` was meant to redo.
    old = tmp_path / "pt" / "Cidade de Deus.azw3"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old book")
    source = Path("/books/cidade.epub")
    new = tmp_path / "pt" / "Cidade de Deus - Paulo Lins.azw3"

    report = library.reconcile(
        tmp_path,
        {source: new},
        {source: "id-1"},
        dry_run=False,
        force=True,
        id_reader=lambda path: "id-1" if path == old else None,
    )

    assert report.renamed == 0
    assert report.renamed_sources == []
    assert report.missing == [source]
    assert not new.exists()
    # the old file was never renamed or reused — it becomes a leftover, not lost.
    assert (tmp_path / library.LEFTOVER_DIR / "pt" / "Cidade de Deus.azw3").is_file()
