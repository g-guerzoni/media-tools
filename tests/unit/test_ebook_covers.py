from media_tools.tasks.ebook import covers


def _book(tmp_path, name="book.epub"):
    path = tmp_path / name
    path.write_bytes(b"content")
    return path


def test_an_embedded_cover_is_used_and_cached(tmp_path):
    book = _book(tmp_path)
    cache = tmp_path / "cache"

    def extract(src, dest, *, cache_dir):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 2000)
        return True

    results = covers.resolve(
        {book: ("Title", "Author", "id-1")}, cache_dir=cache, fetch=False, extract=extract
    )
    assert results[book].source == "embedded"
    assert results[book].path.is_file()
    assert (cache / "covers" / "id-1.jpg").is_file()


def test_a_cached_cover_skips_extraction(tmp_path):
    book = _book(tmp_path)
    cache = tmp_path / "cache"
    cached = cache / "covers" / "id-1.jpg"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"x" * 2000)
    calls = []

    def extract(src, dest, *, cache_dir):
        calls.append(src)
        return False

    results = covers.resolve(
        {book: ("Title", "Author", "id-1")}, cache_dir=cache, fetch=False, extract=extract
    )
    assert calls == []
    assert results[book].path == cached


def test_fetching_is_skipped_when_disabled(tmp_path):
    book = _book(tmp_path)
    fetched = []

    def extract(src, dest, *, cache_dir):
        return False

    def fetch_cover(title, author, dest, *, cache_dir):
        fetched.append(title)
        return True

    results = covers.resolve(
        {book: ("Title", "Author", "id-1")},
        cache_dir=tmp_path / "c",
        fetch=False,
        extract=extract,
        fetch_cover=fetch_cover,
    )
    assert fetched == []
    assert results[book].source == "none"


def test_fetching_runs_only_for_books_without_an_embedded_cover(tmp_path):
    with_cover = _book(tmp_path, "has.epub")
    without = _book(tmp_path, "hasnt.epub")
    fetched = []

    def extract(src, dest, *, cache_dir):
        if src == with_cover:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x" * 2000)
            return True
        return False

    def fetch_cover(title, author, dest, *, cache_dir):
        fetched.append(title)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"y" * 2000)
        return True

    results = covers.resolve(
        {with_cover: ("Has", "A", "id-1"), without: ("Hasnt", "B", "id-2")},
        cache_dir=tmp_path / "c",
        fetch=True,
        extract=extract,
        fetch_cover=fetch_cover,
    )
    assert fetched == ["Hasnt"]
    assert results[with_cover].source == "embedded"
    assert results[without].source == "fetched"


def test_a_tiny_stale_cache_file_is_not_treated_as_a_valid_cover(tmp_path):
    book = _book(tmp_path)
    cache = tmp_path / "cache"
    stale = cache / "covers" / "id-1.jpg"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x")  # far smaller than a real cover
    calls = []

    def extract(src, dest, *, cache_dir):
        calls.append(src)
        dest.write_bytes(b"y" * 2000)
        return True

    results = covers.resolve(
        {book: ("Title", "Author", "id-1")}, cache_dir=cache, fetch=False, extract=extract
    )
    assert calls == [book]
    assert results[book].path.stat().st_size == 2000


def test_a_failed_fetch_leaves_the_book_without_a_cover(tmp_path):
    book = _book(tmp_path)

    def extract(src, dest, *, cache_dir):
        return False

    def fetch_cover(title, author, dest, *, cache_dir):
        return False

    results = covers.resolve(
        {book: ("Title", "Author", "id-1")},
        cache_dir=tmp_path / "c",
        fetch=True,
        extract=extract,
        fetch_cover=fetch_cover,
    )
    assert results[book] == covers.CoverResult(path=None, source="none")
