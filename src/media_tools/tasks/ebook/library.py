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
from dataclasses import dataclass, field
from pathlib import Path

from media_tools.core.paths import truncate_name
from media_tools.core.state import LOCK_FILENAME
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
    # The specific sources reconcile renamed into place (as opposed to `renamed`,
    # the plain count `result.data` reports) — a caller needs these to know exactly
    # which files now carry stale embedded metadata under their new name and must
    # have it rewritten (RB20/C1).
    renamed_sources: list[Path] = field(default_factory=list)


def _read_book_id(path: Path) -> str | None:
    return exth.record_text(exth.read_records(path), exth.TAG_UUID)


def reconcile(
    batch_dir: Path,
    planned: dict[Path, Path],
    book_ids: dict[Path, str],
    *,
    dry_run: bool,
    force: bool = False,
    id_reader: Callable[[Path], str | None] | None = None,
    before_rename: Callable[[Path, Path], bool] | None = None,
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
    plan entirely — moves to `_leftover/`, mirroring its path relative to the
    batch directory (I3: flattening to `_leftover/<name>` silently destroys one of
    two same-named files from different language folders) — never something this
    call just placed. `dry_run` moves and creates nothing, but the report
    (including `leftover`) still reflects what a real run would do.

    `force=True` (minor finding) skips the book-id twin lookup entirely: `--force`
    means "redo items whose output exists", and a book `reconcile` can find under
    an old name is exactly such an item — silently renaming it into place instead
    would defeat `--force` for that one book. Its old file is then simply left
    unclaimed and swept to `_leftover/` like any other file the plan doesn't want,
    while the source gets a genuine reconversion.

    `before_rename(twin, source)` (RB23), when given, is called on a candidate twin
    BEFORE it is renamed into place, and the rename only happens when it returns
    True. The caller uses this to rewrite the twin's embedded metadata (still under
    its OLD name, OLD content) first — a failure there must never still rename the
    file, or the file ends up sitting at a NEW path whose name promises content the
    file does not actually have, with no way for a later run to tell "renamed and
    correct" apart from "renamed but still wrong" (this was RB20/C1's own bug, one
    layer deeper: renaming first and rewriting after left exactly that ambiguity
    whenever the rewrite failed). On a `False`/failed `before_rename`, the twin is
    left exactly where it is — not renamed, and explicitly protected from the
    `_leftover/` sweep below — so the NEXT `reconcile()` call finds it again and
    retries the same rename-and-rewrite attempt, instead of losing it to
    `_leftover/` or silently giving up on it. `before_rename` is never called
    during `dry_run` (which must never touch Calibre or the filesystem) or when the
    caller passes none at all — both cases behave as if it always succeeds, the
    original unconditional-rename behaviour.
    """
    id_reader = id_reader or _read_book_id
    existing = [
        path
        for path in batch_dir.rglob("*")
        if path.is_file()
        and LEFTOVER_DIR not in path.parts
        and path.name != "run.json"
        and path.name != LOCK_FILENAME
    ]
    by_id: dict[str, Path] = {}
    if not force:
        for path in existing:
            found = id_reader(path)
            if found and found not in by_id:
                by_id[found] = path

    kept = renamed = 0
    missing: list[Path] = []
    renamed_sources: list[Path] = []
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
        twin = None if force else by_id.get(book_ids.get(source, ""))
        if twin is not None and twin not in claimed_twins:
            ready = dry_run or before_rename is None or before_rename(twin, source)
            if ready:
                # The book is already converted, only its name changed: rename,
                # never reconvert.
                renamed += 1
                renamed_sources.append(source)
                claimed_targets.add(target)
                claimed_twins.add(twin)
                if not dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    twin.replace(target)
                continue
            # RB23: the rewrite failed. Deliberately NOT added to `missing` — that
            # would make the caller run a full reconversion this same run, which
            # would fill the target and leave nothing for a later run to retry,
            # contradicting "the next run sees the same rename-and-rewrite work to
            # do". Protect the twin from the leftover sweep below instead (it is
            # claimed, just not renamed) so it survives, under its own name and
            # content, for the next reconcile() call to find and retry.
            claimed_twins.add(twin)
            continue
        missing.append(source)

    planned_targets = set(planned.values())
    leftover = [
        path for path in existing if path not in claimed_twins and path not in planned_targets
    ]
    if leftover and not dry_run:
        destination_root = batch_dir / LEFTOVER_DIR
        for path in leftover:
            relative = path.relative_to(batch_dir)
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            path.replace(destination)
    return ReconcileReport(
        kept=kept,
        renamed=renamed,
        missing=missing,
        leftover=leftover,
        renamed_sources=renamed_sources,
    )
