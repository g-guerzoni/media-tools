"""Resolve one cover per book before conversion, so it can ride along in the same
`ebook-convert` pass that creates the book. The old script converted a book, noticed
the cover was missing, and converted it a SECOND time just to attach one — this stage
exists to make that a single pass instead of two.

Order per book: a cover already cached at `<cache_dir>/covers/<book id>.jpg` is
reused; otherwise the embedded cover is extracted locally; only when that also fails
and `fetch` is true does it ask `fetch-ebook-metadata` online. `fetch=False` — what
`--no-cover-fetch` and every dry run pass — must never reach the network: the fetch
phase is skipped entirely rather than called and told not to run.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from media_tools.integrations import calibre

# Mirrors calibre.extract_cover/fetch_cover's own floor for "this is a real cover,
# not an empty/broken file" — applied here too so a small leftover from a previous
# failed attempt is never mistaken for a valid cached cover.
_MIN_COVER_BYTES = 1000


@dataclass(frozen=True)
class CoverResult:
    path: Path | None
    source: str  # "embedded" | "fetched" | "none"


def cache_path(cache_dir: Path, book_id: str) -> Path:
    """Where a book's resolved cover lives, keyed by its stable EXTH 113 id. PUBLIC on
    purpose: `tasks.ebook.kindle.thumbnails` reuses this exact path rather than
    inventing a second cache for the same cover, the same way `massstorage.py`'s
    exclusion constants are public for `mtp.py` to import instead of restating them."""
    return Path(cache_dir) / "covers" / f"{book_id}.jpg"


def _is_cached(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > _MIN_COVER_BYTES


def resolve(
    books: dict[Path, tuple[str, str | None, str]],
    *,
    cache_dir: Path,
    fetch: bool,
    workers: int = 4,
    extract=None,
    fetch_cover=None,
    on_progress=None,
) -> dict[Path, CoverResult]:
    """`books` maps each source path to `(title, author, book_id)` — title/author
    for the online lookup, book_id to name the cache file. Extraction is local and
    cheap, so it runs one book at a time; fetching is a slow network call, so only
    that phase runs through a `ThreadPoolExecutor`.

    `on_progress(done, total, phase)` fires once per book, with `phase` either
    `"extract"` or `"fetch"` and `done`/`total` counted separately per phase — NOT
    combined into one running total against `len(books)`. Extraction always covers
    every book, but the fetch phase only ever covers `pending` (whatever extraction
    could not resolve locally); a single combined counter would walk past 100% the
    moment any pending book entered the fetch phase (minor finding)."""
    extract = extract or calibre.extract_cover
    fetch_cover = fetch_cover or calibre.fetch_cover
    cache_dir = Path(cache_dir)
    (cache_dir / "covers").mkdir(parents=True, exist_ok=True)

    extract_total = len(books)
    results: dict[Path, CoverResult] = {}
    pending: list[Path] = []

    for done, (source, (_title, _author, book_id)) in enumerate(books.items(), start=1):
        dest = cache_path(cache_dir, book_id)
        try:
            found = _is_cached(dest) or extract(source, dest, cache_dir=cache_dir)
        except Exception:
            # A corrupt file or a flaky extractor must not take the whole batch down
            # with it — every other book's already-resolved cover still matters.
            results[source] = CoverResult(path=None, source="none")
        else:
            if found:
                results[source] = CoverResult(path=dest, source="embedded")
            else:
                pending.append(source)
        if on_progress:
            on_progress(done, extract_total, "extract")

    if not fetch:
        for source in pending:
            results[source] = CoverResult(path=None, source="none")
        return results

    def _fetch_one(source: Path) -> CoverResult:
        title, author, book_id = books[source]
        dest = cache_path(cache_dir, book_id)
        if fetch_cover(title, author, dest, cache_dir=cache_dir):
            return CoverResult(path=dest, source="fetched")
        return CoverResult(path=None, source="none")

    if pending:
        fetch_total = len(pending)
        fetch_done = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(_fetch_one, source): source for source in pending}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    results[source] = future.result()
                except Exception:
                    # Same isolation as the extraction phase: one flaky fetch must
                    # not discard every other book's already-resolved cover.
                    results[source] = CoverResult(path=None, source="none")
                fetch_done += 1
                if on_progress:
                    on_progress(fetch_done, fetch_total, "fetch")

    return results
