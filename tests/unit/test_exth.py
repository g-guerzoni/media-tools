from pathlib import Path

import pytest

from media_tools.tasks.ebook import exth, opf


def test_records_of_a_non_mobi_file_are_empty(tmp_path):
    junk = tmp_path / "x.txt"
    junk.write_bytes(b"hello")
    assert exth.read_records(junk) == {}


def test_book_id_is_stable_for_identical_content(tmp_path):
    first = tmp_path / "a.epub"
    second = tmp_path / "b.epub"
    first.write_bytes(b"same bytes")
    second.write_bytes(b"same bytes")
    assert opf.book_id(first) == opf.book_id(second)
    assert len(opf.book_id(first)) == 36


def test_book_id_differs_for_different_content(tmp_path):
    first = tmp_path / "a.epub"
    second = tmp_path / "b.epub"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    assert opf.book_id(first) != opf.book_id(second)


def test_write_opf_round_trips_the_fields(tmp_path):
    target = tmp_path / "book.opf"
    opf.write_opf(
        target,
        title="Título & Cia",
        author="Machado de Assis",
        language="pt",
        book_uuid="12345678-1234-5678-1234-567812345678",
    )
    text = target.read_text(encoding="utf-8")
    assert "Título &amp; Cia" in text
    assert "Machado de Assis" in text
    assert 'opf:scheme="uuid"' in text
    assert "12345678-1234-5678-1234-567812345678" in text


# --- the guarded wrappers ------------------------------------------------------------
#
# `read_records` absorbs everything a malformed MOBI can do itself (`struct.error`,
# `IndexError`, and the `OSError` of a file it cannot open), so the wrappers exist for
# the two families it does NOT absorb. Only one of those can be provoked for real.


def test_read_records_or_none_answers_none_for_a_path_open_itself_refuses():
    """The live arm, through the REAL function and no stub at all: `Path.read_bytes()`
    opens the file, and `open()` raises `ValueError` — not `OSError` — for a path
    carrying an embedded NUL byte, so `read_records`' own `except OSError` does not
    catch it."""
    with pytest.raises(ValueError):
        exth.read_records(Path("no\0pe"))
    assert exth.read_records_or_none(Path("no\0pe")) is None
    assert exth.read_records_safe(Path("no\0pe")) == {}


def test_read_records_or_none_answers_none_for_a_file_it_cannot_OPEN(tmp_path):
    """The `OSError` arm, which was live all along and answered WRONGLY. `read_records`
    absorbs that error into `{}` itself, so a wrapper that merely delegated to it could
    never tell a file it could not open from one that parsed as carrying no records.
    `kindle.cli._book_id_of` is built on exactly that distinction: it must not CACHE a
    failure as if it were an answer, under a key (size|mtime) a device file never
    changes."""
    missing = tmp_path / "never-fetched.azw3"
    assert exth.read_records(missing) == {}, "read_records keeps its own contract"
    assert exth.read_records_or_none(missing) is None
    assert exth.read_records_safe(missing) == {}

    directory = tmp_path / "a-directory.azw3"
    directory.mkdir()
    assert exth.read_records_or_none(directory) is None


def test_read_records_or_none_answers_none_for_a_memory_error(monkeypatch):
    """The other live arm. `MemoryError` cannot be provoked deterministically, so this
    one IS a stub — of the whole-file read `read_records` performs to reach a header in
    the first hundred bytes, which is where the real one would come from."""

    def out_of_memory(self):
        raise MemoryError("cannot allocate")

    monkeypatch.setattr(Path, "read_bytes", out_of_memory)
    assert exth.read_records_or_none(Path("anything.azw3")) is None


def test_a_bug_in_this_module_is_not_disguised_as_a_book_with_no_records(monkeypatch):
    """Deliberately NOT a bare `except Exception`: an `AttributeError`/`TypeError` from
    a future refactor here is this project's own bug, and the convention is that those
    escape as an honest `internal_error` rather than reading as an unparseable book."""

    def refactored_away(data):
        raise AttributeError("_records_from no longer has that attribute")

    monkeypatch.setattr(exth, "_records_from", refactored_away)
    with pytest.raises(AttributeError):
        exth.read_records_or_none(Path(__file__))


def test_a_malformed_mobi_still_reads_as_empty_rather_than_as_a_failure(tmp_path):
    """Pinning the division of labour: a file that will not PARSE is `read_records`'
    own business and comes back `{}`, never `None`. The wrappers are not what handles
    it, and a test that assumes otherwise is testing the wrong layer."""
    broken = tmp_path / "broken.azw3"
    broken.write_bytes(b"\x00" * 64)
    assert exth.read_records(broken) == {}
    assert exth.read_records_or_none(broken) == {}
