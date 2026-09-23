"""Where a converted book ends up: `<language>/Title - Author.<ext>`, or a review
folder when the LLM flagged it or no language could be determined.

Placement is split into two steps because a naive re-run must not reconvert a book
just because an LLM title correction moved its target filename:

- `plan_placement` decides where every source book *should* live, independent of
  what is already on disk.
- `reconcile` compares that plan against reality. A file already sitting at its
  planned target is left alone; a file elsewhere in the batch that carries the same
  stable book id (EXTH 113 — see `tasks.ebook.opf.book_id`) is RENAMED into place
  instead of asking the caller to reconvert the whole book. Only what is genuinely
  missing (`ReconcileReport.missing`) needs a real conversion.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from media_tools.core.paths import truncate_name
from media_tools.tasks.ebook import exth
from media_tools.tasks.ebook.normalize import Verdict

REVIEW_DIR = "_review"
LEFTOVER_DIR = "_leftover"
UNKNOWN_LANGUAGE = "unknown-language"
MAX_FILENAME = 255

# Characters no mainstream filesystem accepts in a single path component, plus
# control characters. Deliberately narrower than `core.paths.sanitize_batch` (built
# for machine-chosen batch names, which also folds whitespace to '-'): a book title
# is meant to be read, so ordinary spacing and punctuation must survive untouched.
_FS_ILLEGAL = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_EXTRA_SPACE = re.compile(r" {2,}")


def safe_filename(name: str) -> str:
    """A single path component safe to write to disk on any common filesystem."""
    text = unicodedata.normalize("NFC", name or "")
    text = _FS_ILLEGAL.sub("", text)
    text = _EXTRA_SPACE.sub(" ", text).strip()
    text = text.rstrip(". ")  # Windows rejects a component ending in '.' or ' '
    return text or "untitled"


def target_path(batch_dir: Path, verdict: Verdict, extension: str) -> Path:
    """`<lang>/Title - Author.<ext>` for a clean, identified book; a review
    subfolder otherwise — `_review/unknown-language/...` when no language could be
    placed, `_review/<status>/...` for anything the LLM flagged (`invalid`/
    `irrelevant`/`unidentified`). A flagged book is still placed here, never
    dropped — just somewhere a human will look at it."""
    name = f"{verdict.title} - {verdict.author}" if verdict.author else verdict.title
    filename = truncate_name(f"{safe_filename(name)}.{extension}", MAX_FILENAME)
    if verdict.status != "ok":
        return batch_dir / REVIEW_DIR / verdict.status / filename
    if not verdict.language:
        return batch_dir / REVIEW_DIR / UNKNOWN_LANGUAGE / filename
    return batch_dir / verdict.language / filename


def _suffixed(target: Path, n: int) -> Path:
    return target.parent / truncate_name(f"{target.stem} ({n}){target.suffix}", MAX_FILENAME)


def plan_placement(
    batch_dir: Path, entries: dict[Path, tuple[Verdict, str, str]]
) -> tuple[dict[Path, Path], dict[Path, str]]:
    """Where every source book should end up. `entries` maps each source to its
    `(verdict, extension, book_id)` — only `verdict`/`extension` decide the target
    here; the book id is what `reconcile` uses separately to avoid reconverting.

    Two different books can sanitise to the identical target — e.g. "AC/DC Story"
    and "AC:DC Story" by the same author both lose their only distinguishing
    character. Silently letting that happen would mean the second book converted
    just overwrites the first the moment it is written. Sources are processed in a
    deterministic order (sorted by source path), independent of dict iteration
    order, so the outcome never varies between runs; the first source to reach a
    given target keeps the clean name, and every later collision gets a numeric
    suffix before the extension — " (2)", " (3)", ... — each candidate re-checked
    against every target already handed out so a suffixed name can't itself
    collide. The second return value lists every source that had to be suffixed,
    keyed by source, so the caller can attach a `name_collision_suffixed` warning
    to that book.
    """
    plan: dict[Path, Path] = {}
    used: set[Path] = set()
    collisions: dict[Path, str] = {}

    for source in sorted(entries, key=str):
        verdict, extension, _book_id = entries[source]
        target = target_path(batch_dir, verdict, extension)
        if target in used:
            n = 2
            candidate = _suffixed(target, n)
            while candidate in used:
                n += 1
                candidate = _suffixed(target, n)
            target = candidate
            collisions[source] = "name_collision_suffixed"
        used.add(target)
        plan[source] = target

    return plan, collisions


@dataclass
class ReconcileReport:
    kept: int
    renamed: int
    missing: list[Path]
    leftover: list[Path]


def _read_book_id(path: Path) -> str | None:
    return exth.record_text(exth.read_records(path), exth.TAG_UUID)


def reconcile(
    batch_dir: Path,
    planned: dict[Path, Path],
    book_ids: dict[Path, str],
    *,
    dry_run: bool,
    id_reader: Callable[[Path], str | None] | None = None,
) -> ReconcileReport:
    """Make the batch directory match `planned`, without reconverting anything
    that is already there under a different name.

    For each planned target: keep a file already sitting there; otherwise look for
    a file elsewhere in the batch whose own EXTH 113 id matches this source's book
    id (`id_reader`, defaulting to reading the real record — injectable so tests
    need no real AZW3 bytes) and RENAME that file into place — this is what makes
    an LLM title correction free instead of a full reconversion. A single existing
    file is never claimed for two different targets: once it has been kept or
    renamed, it is off the table for every later planned entry. Anything still
    left over once every planned target is settled — a book that fell out of the
    plan entirely — moves to `_leftover/`, never something this call just placed.
    `dry_run` moves and creates nothing, but the report (including `leftover`)
    still reflects what a real run would do.
    """
    id_reader = id_reader or _read_book_id
    existing = [
        path
        for path in batch_dir.rglob("*")
        if path.is_file() and LEFTOVER_DIR not in path.parts and path.name != "run.json"
    ]
    by_id: dict[str, Path] = {}
    for path in existing:
        found = id_reader(path)
        if found and found not in by_id:
            by_id[found] = path

    kept = renamed = 0
    missing: list[Path] = []
    claimed_targets: set[Path] = set()
    claimed_twins: set[Path] = set()
    for source, target in planned.items():
        if target in claimed_targets:
            # Another planned entry already produced this exact target; this one
            # still needs its own conversion rather than sharing that file.
            missing.append(source)
            continue
        if target.exists():
            kept += 1
            claimed_targets.add(target)
            continue
        twin = by_id.get(book_ids.get(source, ""))
        if twin is not None and twin not in claimed_twins:
            # The book is already converted, only its name changed: rename, never
            # reconvert.
            renamed += 1
            claimed_targets.add(target)
            claimed_twins.add(twin)
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                twin.replace(target)
            continue
        missing.append(source)

    planned_targets = set(planned.values())
    leftover = [
        path for path in existing if path not in claimed_twins and path not in planned_targets
    ]
    if leftover and not dry_run:
        destination = batch_dir / LEFTOVER_DIR
        destination.mkdir(parents=True, exist_ok=True)
        for path in leftover:
            path.replace(destination / path.name)
    return ReconcileReport(kept=kept, renamed=renamed, missing=missing, leftover=leftover)
