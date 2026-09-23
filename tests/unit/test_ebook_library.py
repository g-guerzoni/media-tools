from pathlib import Path

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
    plan = library.plan_placement(tmp_path, entries)
    assert plan == {source: tmp_path / "en" / "Solaris - Stanislaw Lem.azw3"}


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


def test_reconcile_moves_files_that_left_the_list(tmp_path):
    stale = tmp_path / "en" / "Gone.azw3"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x")
    report = library.reconcile(tmp_path, {}, {}, dry_run=False, id_reader=lambda p: None)
    assert (tmp_path / "_leftover" / "Gone.azw3").is_file()
    assert report.leftover == [stale]


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
    assert not (tmp_path / "_leftover" / "Cidade de Deus.azw3").exists()
    assert (tmp_path / "_leftover" / "Gone.azw3").is_file()
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
