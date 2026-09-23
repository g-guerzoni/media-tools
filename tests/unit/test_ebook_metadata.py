import json
import tempfile
from pathlib import Path

from media_tools.tasks.ebook import metadata


def test_reads_are_cached_by_path_size_and_mtime(tmp_path, monkeypatch):
    book = tmp_path / "Cidade de Deus - Paulo Lins.epub"
    book.write_bytes(b"x" * 100)
    cache = tmp_path / "cache"

    calls = []

    def fake_read(path, *, cache_dir, timeout=120):
        calls.append(path)
        from media_tools.integrations.calibre import BookMetadata

        return BookMetadata("Embedded Title", "Embedded Author", "pt", "uuid-1", True)

    monkeypatch.setattr(metadata.calibre, "read_metadata", fake_read)

    first = metadata.read_all([book], cache_dir=cache, workers=2)
    second = metadata.read_all([book], cache_dir=cache, workers=2)

    assert len(calls) == 1, "the second read must come from the cache"
    assert first[0].meta_title == "Embedded Title"
    assert second[0].meta_title == "Embedded Title"
    assert first[0].file_title == "Cidade de Deus"
    assert first[0].file_author == "Paulo Lins"
    assert json.loads((cache / "ebook-meta.json").read_text())


def test_a_changed_file_is_read_again(tmp_path, monkeypatch):
    book = tmp_path / "book.epub"
    book.write_bytes(b"x" * 10)
    cache = tmp_path / "cache"
    calls = []

    def fake_read(path, *, cache_dir, timeout=120):
        calls.append(path)
        from media_tools.integrations.calibre import BookMetadata

        return BookMetadata(None, None, None, None, False)

    monkeypatch.setattr(metadata.calibre, "read_metadata", fake_read)
    metadata.read_all([book], cache_dir=cache, workers=1)
    book.write_bytes(b"y" * 20)
    metadata.read_all([book], cache_dir=cache, workers=1)
    assert len(calls) == 2


def test_cache_dir_none_writes_nothing(tmp_path, monkeypatch):
    book = tmp_path / "book.epub"
    book.write_bytes(b"x")

    def fake_read(path, *, cache_dir, timeout=120):
        from media_tools.integrations.calibre import BookMetadata

        return BookMetadata(None, None, None, None, False)

    monkeypatch.setattr(metadata.calibre, "read_metadata", fake_read)
    metadata.read_all([book], cache_dir=None, workers=1)
    assert list(tmp_path.glob("**/ebook-meta.json")) == []


def test_a_vanished_path_is_reported_and_does_not_lose_the_rest(tmp_path, monkeypatch):
    good = tmp_path / "good.epub"
    good.write_bytes(b"x" * 10)
    missing = tmp_path / "missing.epub"  # never created: simulates moved/deleted mid-scan
    cache = tmp_path / "cache"

    def fake_read(path, *, cache_dir, timeout=120):
        from media_tools.integrations.calibre import BookMetadata

        return BookMetadata("Good", "Author", "en", "uuid-1", True)

    monkeypatch.setattr(metadata.calibre, "read_metadata", fake_read)

    errors = []
    facts = metadata.read_all(
        [good, missing],
        cache_dir=cache,
        workers=2,
        on_error=lambda path, message: errors.append((path, message)),
    )

    assert [f.path for f in facts] == [good]
    assert facts[0].meta_title == "Good"
    assert len(errors) == 1
    assert errors[0][0] == missing


def test_a_malformed_cache_entry_is_re_read_not_crashed_on(tmp_path, monkeypatch):
    book = tmp_path / "book.epub"
    book.write_bytes(b"x" * 10)
    cache = tmp_path / "cache"
    cache.mkdir()
    stat = book.stat()
    key = f"{book.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    (cache / "ebook-meta.json").write_text(json.dumps({key: "not-a-dict"}), encoding="utf-8")

    calls = []

    def fake_read(path, *, cache_dir, timeout=120):
        calls.append(path)
        from media_tools.integrations.calibre import BookMetadata

        return BookMetadata("Recovered Title", None, None, None, False)

    monkeypatch.setattr(metadata.calibre, "read_metadata", fake_read)

    facts = metadata.read_all([book], cache_dir=cache, workers=1)

    assert len(calls) == 1, "a malformed entry must be treated as a cache miss"
    assert facts[0].meta_title == "Recovered Title"
    assert json.loads((cache / "ebook-meta.json").read_text())[key]["title"] == "Recovered Title"


def test_dry_run_scratch_dir_is_private_and_cleaned_up(tmp_path, monkeypatch):
    book = tmp_path / "book.epub"
    book.write_bytes(b"x")
    seen_dirs = []

    def fake_read(path, *, cache_dir, timeout=120):
        seen_dirs.append(Path(cache_dir))
        assert Path(cache_dir).is_dir()
        from media_tools.integrations.calibre import BookMetadata

        return BookMetadata(None, None, None, None, False)

    monkeypatch.setattr(metadata.calibre, "read_metadata", fake_read)
    metadata.read_all([book], cache_dir=None, workers=1)

    assert len(seen_dirs) == 1
    # Not the single shared, predictable system temp dir itself (two concurrent runs
    # would otherwise fight over the same Calibre config there).
    assert seen_dirs[0] != Path(tempfile.gettempdir())
    # Removed once the call is done: a dry run leaves nothing behind.
    assert not seen_dirs[0].exists()
