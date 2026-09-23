import json

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
