"""Read every book's metadata once, in parallel, and cache it.

The old scripts this replaces called `ebook-meta` two or three times per book,
sequentially — half an hour for a few thousand books. `read_all` calls it once per
book, in a thread pool (the work is I/O/subprocess-bound, not CPU-bound), and caches
the result by path+size+mtime so a rebuild that touches no files is instant.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from media_tools.integrations import calibre
from media_tools.tasks.ebook import names

CACHE_FILENAME = "ebook-meta.json"


@dataclass(frozen=True)
class BookFacts:
    path: Path
    size: int
    fmt: str
    meta_title: str | None
    meta_author: str | None
    meta_language: str | None
    meta_uuid: str | None
    has_cover: bool
    file_title: str
    file_author: str | None


def _key(path: Path) -> str:
    stat = path.stat()
    return f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"


def _load_cache(cache_file: Path | None) -> dict[str, dict]:
    if cache_file is None or not cache_file.is_file():
        return {}
    try:
        loaded = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def read_all(
    paths: list[Path],
    *,
    cache_dir: Path | None,
    workers: int = 8,
    on_progress: Callable[[int, int, Path], None] | None = None,
) -> list[BookFacts]:
    """Read metadata for every path, hitting Calibre only for the ones the cache
    (keyed by resolved path + size + mtime_ns) doesn't already cover. `cache_dir=None`
    (what `--dry-run` passes) still reads through Calibre — it just never persists a
    cache file to disk, using a throwaway system temp dir for Calibre's own scratch
    work instead."""
    cache_file = Path(cache_dir) / CACHE_FILENAME if cache_dir else None
    cache = _load_cache(cache_file)

    todo = [path for path in paths if _key(path) not in cache]
    if todo:
        scratch_dir = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir())
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {
                pool.submit(calibre.read_metadata, path, cache_dir=scratch_dir): path
                for path in todo
            }
            for done, future in enumerate(as_completed(futures), start=1):
                path = futures[future]
                try:
                    meta = future.result()
                except calibre.CalibreError:
                    meta = calibre.BookMetadata(None, None, None, None, False)
                cache[_key(path)] = {
                    "title": meta.title,
                    "author": meta.author,
                    "language": meta.language,
                    "uuid": meta.uuid,
                    "has_cover": meta.has_cover,
                }
                if on_progress:
                    on_progress(done, len(todo), path)

    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    facts = []
    for path in paths:
        entry = cache.get(_key(path), {})
        file_title, file_author = names.parse_filename(path)
        facts.append(
            BookFacts(
                path=path,
                size=path.stat().st_size,
                fmt=path.suffix.lower().lstrip("."),
                meta_title=entry.get("title"),
                meta_author=entry.get("author"),
                meta_language=entry.get("language"),
                meta_uuid=entry.get("uuid"),
                has_cover=bool(entry.get("has_cover")),
                file_title=file_title,
                file_author=file_author,
            )
        )
    return facts
