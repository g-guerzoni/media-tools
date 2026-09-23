"""ebook build: wire every stage — scan, metadata, normalize, dedup, covers,
convert, verify, organize — into one command. Each of the other ebook
subcommands (`scan`, `normalize`, `dedup`, `covers`, `convert`) shares this same
pipeline and simply stops after its own named stage, per the plan's Step 3.

The heavy lifting (metadata reads, LLM classification, dedup grouping/merging,
cover resolution) all happens BEFORE the batch is locked: those calls only ever
touch the output root's shared `.cache/` (never the batch directory itself), so
computing them ahead of `RunState.open()` cannot corrupt another run's batch —
it can, in the rare case of a batch-name conflict, spend LLM tokens whose
answers are then simply left in the cache for the next attempt to reuse for
free, rather than wasted outright.

Conversion writes each winning book straight to its already-planned final
location (`tasks.ebook.library.plan_placement`/`reconcile`), so the "convert"
and "organize" stages are two announcements around one physical write instead
of a convert-then-move pass — a naive re-run must never reconvert a book only
because it needs to be filed under a fixed language folder.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path

from media_tools.core.events import (
    EXIT_DEPENDENCY,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    Reporter,
)
from media_tools.core.inputs import InputError, expand_inputs, parse_extensions
from media_tools.core.paths import (
    BatchNameError,
    batch_hash,
    fsync_replace,
    output_root,
    sanitize_batch,
)
from media_tools.core.runner import build_result, empty_result, write_summary_json
from media_tools.core.state import BatchInUse, BatchTaskMismatch, RunState, resolve_batch_name
from media_tools.integrations import calibre, openrouter
from media_tools.tasks.common import UsageError, add_common_flags
from media_tools.tasks.ebook import covers as covers_stage
from media_tools.tasks.ebook import dedup as dedup_stage
from media_tools.tasks.ebook import exth, library, opf
from media_tools.tasks.ebook import metadata as metadata_stage
from media_tools.tasks.ebook import normalize as normalize_stage

NAME = "ebook"

EBOOK_EXTENSIONS = frozenset({".epub", ".mobi", ".azw", ".azw3", ".prc", ".pdf"})
EBOOK_OUTPUTS = ("azw3", "epub", "mobi", "pdf")
DEFAULT_MODEL = "openai/gpt-4o-mini"
KINDLE_FORMATS = frozenset({"azw3", "mobi"})

# The pipeline's full order (spec 8.2). Every subcommand below is named after the
# stage it stops after; `build` is the only one that runs all the way to "organize".
STAGE_ORDER = ("scan", "metadata", "normalize", "dedup", "covers", "convert", "verify", "organize")

# subcommand name -> (stage it stops after, help text)
#
# `scan` stops at the same stage as `dedup` — it is the cheap "what is in this
# folder" command an agent runs before committing to a real build (spec 8.1: it
# "creates/updates the batch run.json" with the inventory and "converts nothing").
# What actually sets it apart from `dedup` is the LLM policy (see `run()`): `scan`
# never blocks on a missing key, silently falling back to the offline heuristic,
# while every other subcommand keeps the LLM-on-by-default / missing-key-is-fatal
# contract `build` has.
SUBCOMMANDS = {
    "scan": (
        "dedup",
        "Inventory sources — metadata, resolved title/author/language, duplicates — "
        "into the batch's run.json. Converts nothing; never requires an API key.",
    ),
    "normalize": ("normalize", "Scan, read metadata, and clean title/author/language."),
    "dedup": ("dedup", "...through duplicate grouping (see `scan`/`normalize`)."),
    "covers": ("covers", "...through cover resolution."),
    "convert": ("convert", "...through conversion (books land in their final folder)."),
    "build": ("organize", "The full pipeline: scan through organize."),
}


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise UsageError(f"--workers must be a positive integer, got {value!r}")
    return parsed


def add_pipeline_flags(parser) -> None:
    """Every ebook subcommand shares this flag set (Decision 1: recursion is ON by
    default for ebook sources, the documented exception to the common flag set)."""
    add_common_flags(parser, scan_flags=False)
    parser.add_argument(
        "--no-recursive", action="store_true", help="Do not scan subfolders (default: on)."
    )
    parser.add_argument(
        "-e", "--extensions", default=None, help="Comma-separated extensions to pick from folders."
    )
    parser.add_argument(
        "--to",
        choices=EBOOK_OUTPUTS,
        default="azw3",
        help="Target format to convert to (default: azw3).",
    )
    parser.add_argument(
        "--prefer",
        default=",".join(dedup_stage.DEFAULT_PREFERENCE),
        help="Comma-separated format preference for picking a duplicate's winner "
        "(default: %(default)s).",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Offline heuristics only: no OpenRouter calls, no API key needed.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="OpenRouter model for normalize/dedup (default: %(default)s).",
    )
    parser.add_argument(
        "--op-item",
        default=None,
        metavar="NAME",
        help="Named 1Password item to resolve the OpenRouter key from.",
    )
    parser.add_argument(
        "--no-cover-fetch",
        action="store_true",
        help="Never look up a cover online; embedded covers only.",
    )
    parser.add_argument(
        "--workers",
        type=_positive_int,
        default=None,
        help="Concurrent conversions (default: min(4, cpu_count)).",
    )
    parser.add_argument(
        "--list",
        dest="list_file",
        type=Path,
        default=None,
        metavar="FILE",
        help="Read sources from a JSON list file instead of positional sources: "
        '["path", …] or [{"path": …, "title": …, "author": …, "language": …}, …].',
    )


@dataclass(frozen=True)
class ListEntry:
    path: Path
    title: str | None = None
    author: str | None = None
    language: str | None = None


def load_list(path: Path) -> list[ListEntry]:
    """`--list FILE`: a JSON array of plain path strings, of `{"path", "title",
    "author", "language"}` records, or a mix of both. The three optional fields
    override whatever normalize would have produced for that book (see
    `examples/ebook-list.json`)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise UsageError(f"list file not found: {path}") from error
    except json.JSONDecodeError as error:
        raise UsageError(f"list file is not valid JSON: {error}") from error

    if not isinstance(data, list):
        raise UsageError(
            'list file must be ["path", …] or '
            '[{"path": …, "title": …, "author": …, "language": …}, …]'
        )

    entries: list[ListEntry] = []
    for raw in data:
        if isinstance(raw, str):
            entries.append(ListEntry(Path(raw)))
        elif isinstance(raw, dict) and isinstance(raw.get("path"), str):
            entries.append(
                ListEntry(
                    Path(raw["path"]),
                    title=raw.get("title"),
                    author=raw.get("author"),
                    language=raw.get("language"),
                )
            )
        else:
            raise UsageError(f"invalid entry in list file: {raw!r}")
    if not entries:
        raise UsageError(f"list file has no paths: {path}")
    return entries


def _parse_prefer(raw: str) -> tuple[str, ...]:
    tokens = tuple(part.strip().lower() for part in (raw or "").split(",") if part.strip())
    return tokens or dedup_stage.DEFAULT_PREFERENCE


def _gather_sources(
    args, *, reporter: Reporter
) -> tuple[list[Path], dict[Path, tuple[str | None, str | None, str | None]]]:
    """Sources plus any `--list`-supplied title/author/language overrides, keyed by
    path. Never both positional sources and `--list`; always at least one."""
    if args.list_file:
        entries = load_list(args.list_file)
        if args.include:
            needle = args.include.lower()
            entries = [e for e in entries if needle in str(e.path).lower()]
        if args.exclude:
            needle = args.exclude.lower()
            entries = [e for e in entries if needle not in str(e.path).lower()]
        if args.limit is not None:
            entries = entries[: args.limit]
        if not entries:
            raise UsageError("no path matched (check --include/--exclude/--limit)")
        overrides = {
            e.path: (e.title, e.author, e.language)
            for e in entries
            if e.title or e.author or e.language
        }
        return [e.path for e in entries], overrides

    def warn(code: str, message: str) -> None:
        reporter.warning(code=code, message=message)

    try:
        sources = expand_inputs(
            args.inputs,
            recursive=not args.no_recursive,
            extensions=parse_extensions(args.extensions) or None,
            accepted=EBOOK_EXTENSIONS,
            output_root=output_root(args.output_dir),
            include=args.include,
            exclude=args.exclude,
            limit=args.limit,
            warn=warn,
        )
    except InputError as error:
        raise UsageError(str(error)) from error

    if not sources:
        raise UsageError(
            f"no input matched under {', '.join(str(p) for p in args.inputs)}",
            code="no_input_matched",
            hint="check --extensions, or pass --no-recursive",
        )
    return [s.path for s in sources], {}


def _apply_list_overrides(
    verdicts: dict[Path, normalize_stage.Verdict],
    overrides: dict[Path, tuple[str | None, str | None, str | None]],
) -> None:
    """RB22: the language precedence chain's top step (`--list` override > LLM >
    embedded tag > title heuristic > unknown) — the rest of the chain now lives
    entirely inside `normalize.heuristic()`/`_verdict_from()`; there is no longer a
    separate `_apply_language_fallback` pass here (an embedded tag used to only be
    consulted here, as a fallback AFTER the title heuristic already ran and could
    have already produced a wrong, confident guess — see `normalize.heuristic`'s
    own docstring for why that ordering was the actual bug)."""
    for path, (title, author, language) in overrides.items():
        base = verdicts.get(path)
        if base is None:
            continue
        verdicts[path] = replace(
            base,
            title=title if title else base.title,
            author=author if author else base.author,
            language=language if language else base.language,
            language_origin="list" if language else base.language_origin,
            source="list",
        )


def register_subparsers(ebook_parser) -> None:
    subparsers = ebook_parser.add_subparsers(dest="ebook_command", required=True)
    for command, (_, help_text) in SUBCOMMANDS.items():
        sub = subparsers.add_parser(command, help=help_text, description=help_text)
        add_pipeline_flags(sub)


def run(args) -> int:
    stop_after, _ = SUBCOMMANDS[args.ebook_command]
    reporter = Reporter(json_mode=args.json_mode, quiet=args.quiet)

    if args.inputs and args.list_file:
        raise UsageError("pass sources or --list FILE, not both")
    if not args.inputs and not args.list_file:
        raise UsageError(f"no input given; see `media-tools ebook {args.ebook_command} --help`")

    llm_enabled = not args.no_llm and not args.dry_run
    api_key: str | None = None
    if llm_enabled:
        try:
            api_key = openrouter.resolve_key(args.op_item)
        except openrouter.OpenRouterError as error:
            if args.ebook_command == "scan":
                # `scan` is the cheap "what is in this folder" command an agent runs
                # before committing to a real build (fix round 1 ruling) — it never
                # blocks on a missing key, it just falls back to the offline
                # heuristic instead of the LLM passes.
                llm_enabled = False
            else:
                # "config_missing", not "dependency_missing": nothing to install
                # fixes a missing key. The hint is the resolver's own message,
                # which already names all three ways to supply one plus --no-llm
                # (Decision 5).
                raise UsageError(
                    "no OpenRouter API key available",
                    code="config_missing",
                    hint=str(error),
                    exit_code=EXIT_DEPENDENCY,
                ) from error

    # Every subcommand reads metadata (ebook-meta) at minimum; only `convert`/`build`
    # also convert (ebook-convert). Checked once, up front, so a real run fails fast
    # with one clear message instead of every single item failing with a cryptic
    # engine_error.
    if not args.dry_run:
        needed = [calibre.EBOOK_META]
        if _stage_index(stop_after) >= _stage_index("convert"):
            needed.append(calibre.EBOOK_CONVERT)
        for dep in needed:
            if dep.locate() is None:
                raise UsageError(
                    f"missing dependency: {dep.name}",
                    code="dependency_missing",
                    hint=dep.install_hint,
                    exit_code=EXIT_DEPENDENCY,
                )

    paths, overrides = _gather_sources(args, reporter=reporter)
    prefer = _parse_prefer(args.prefer)
    root = output_root(args.output_dir)
    cache_dir = root / ".cache"

    options = {
        "to": args.to,
        "prefer": list(prefer),
        "llm": llm_enabled,
        "model": args.model if llm_enabled else None,
        "cover_fetch": not args.no_cover_fetch,
        "stop_after": stop_after,
    }
    selection = {
        "recursive": not args.no_recursive,
        "extensions": sorted(parse_extensions(args.extensions)),
    }
    hash_inputs = [str(args.list_file)] if args.list_file else list(args.inputs)
    try:
        if args.batch:
            batch_name = sanitize_batch(args.batch)
        else:
            base_name = batch_hash(
                task=NAME, options=options, selection=selection, inputs=hash_inputs
            )
            batch_name = resolve_batch_name(root, base_name, task=NAME, options=options)
    except BatchNameError as error:
        raise UsageError(str(error)) from error
    batch_dir = root / batch_name

    return _run_pipeline(
        paths,
        overrides,
        args,
        stop_after=stop_after,
        reporter=reporter,
        batch_dir=batch_dir,
        cache_dir=cache_dir,
        options=options,
        prefer=prefer,
        api_key=api_key,
        llm_enabled=llm_enabled,
    )


@dataclass
class _Plan:
    facts_by_path: dict[Path, metadata_stage.BookFacts]
    dropped: dict[Path, str]
    verdicts: dict[Path, normalize_stage.Verdict]
    groups: list[dedup_stage.Group]
    normalize_stats: dict
    dedup_stats: dict
    cover_results: dict[Path, covers_stage.CoverResult]
    book_ids: dict[Path, str]


def _stage_index(name: str) -> int:
    return STAGE_ORDER.index(name)


def _announce_stage(reporter: Reporter, stage_name: str, stages: list[str]) -> None:
    """Emit `stage` for `stage_name` only when it is actually one of THIS run's
    declared stages (`start`'s own `stages` list) — a subcommand that stops before
    a given stage never announces it, even though some of that stage's cheap work
    (e.g. `dedup.group`'s exact pass) still runs unconditionally further down."""
    if stage_name in stages:
        reporter.stage(stage=stage_name, index=stages.index(stage_name) + 1, count=len(stages))


def _build_plan(
    paths: list[Path],
    overrides: dict[Path, tuple[str | None, str | None, str | None]],
    *,
    args,
    stop_after: str,
    dry_run: bool,
    llm_enabled: bool,
    api_key: str | None,
    cache_dir: Path,
    prefer: tuple[str, ...],
    reporter: Reporter,
    stages: list[str],
) -> _Plan:
    # I5: each stage is announced as it actually begins (metadata/normalize/dedup/
    # covers all happen in here, one after another) instead of all eight firing
    # upfront before any work starts — and `on_progress`, which `read_all`/
    # `classify`/`resolve` already accept, is now actually wired to
    # `Reporter.progress` so the metadata-read phase (the ~113s-of-silence case on
    # a real 3,590-book library) reports something between the "metadata" and
    # "normalize" stage events.
    _announce_stage(reporter, "scan", stages)  # sources were already expanded by
    # `_gather_sources` before `_run_pipeline` even emitted `start` — see its docstring.

    dropped: dict[Path, str] = {}

    def on_error(path: Path, message: str) -> None:
        dropped[path] = message

    _announce_stage(reporter, "metadata", stages)

    def _metadata_progress(done: int, total: int, path: Path) -> None:
        reporter.progress(
            stage="metadata", index=done, count=total, path=path, percent=100 * done / total
        )

    facts = metadata_stage.read_all(
        paths,
        cache_dir=(None if dry_run else cache_dir),
        workers=8,
        on_error=on_error,
        on_progress=_metadata_progress,
    )
    facts_by_path = {f.path: f for f in facts}

    _announce_stage(reporter, "normalize", stages)
    if llm_enabled:

        def _normalize_progress(done: int, total: int) -> None:
            reporter.progress(
                stage="normalize", index=done, count=total, path="batch", percent=100 * done / total
            )

        verdicts, normalize_stats = normalize_stage.classify(
            facts,
            model=args.model,
            api_key=api_key,
            cache_dir=(None if dry_run else cache_dir),
            on_progress=_normalize_progress,
        )
    else:
        verdicts = {f.path: normalize_stage.heuristic(f) for f in facts}
        normalize_stats = {
            "llm_calls": 0,
            "cache_hits": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "errors": 0,
        }

    _apply_list_overrides(verdicts, overrides)

    formats = {f.path: f.fmt for f in facts}
    _announce_stage(reporter, "dedup", stages)
    groups = dedup_stage.group(verdicts, preference=prefer, formats=formats)
    dedup_stats = {"llm_calls": 0, "buckets": 0, "merges": 0, "errors": 0, "blocked_merges": 0}
    if llm_enabled and _stage_index(stop_after) >= _stage_index("dedup"):
        groups, dedup_stats = dedup_stage.refine(
            groups, verdicts, formats, model=args.model, api_key=api_key, preference=prefer
        )

    cover_results: dict[Path, covers_stage.CoverResult] = {}
    book_ids: dict[Path, str] = {}
    if _stage_index(stop_after) >= _stage_index("covers"):
        _announce_stage(reporter, "covers", stages)
        for group in groups:
            book_ids[group.winner] = opf.book_id(group.winner)
        if dry_run:
            for group in groups:
                winner_facts = facts_by_path.get(group.winner)
                has_cover = bool(winner_facts and winner_facts.has_cover)
                cover_results[group.winner] = covers_stage.CoverResult(
                    path=None, source="embedded" if has_cover else "none"
                )
        else:
            books = {
                group.winner: (
                    verdicts[group.winner].title,
                    verdicts[group.winner].author,
                    book_ids[group.winner],
                )
                for group in groups
            }

            def _covers_progress(done: int, total: int, phase: str) -> None:
                reporter.progress(
                    stage="covers", index=done, count=total, path=phase, percent=100 * done / total
                )

            cover_results = covers_stage.resolve(
                books,
                cache_dir=cache_dir,
                fetch=(not args.no_cover_fetch),
                workers=(args.workers or 4),
                on_progress=_covers_progress,
            )

    return _Plan(
        facts_by_path=facts_by_path,
        dropped=dropped,
        verdicts=verdicts,
        groups=groups,
        normalize_stats=normalize_stats,
        dedup_stats=dedup_stats,
        cover_results=cover_results,
        book_ids=book_ids,
    )


def _llm_summary(plan: _Plan) -> dict:
    return {
        "requests": plan.normalize_stats["llm_calls"] + plan.dedup_stats["llm_calls"],
        "cache_hits": plan.normalize_stats["cache_hits"],
        "prompt_tokens": plan.normalize_stats["prompt_tokens"],
        "completion_tokens": plan.normalize_stats["completion_tokens"],
        "heuristic_fallback_batches": (plan.normalize_stats["errors"] + plan.dedup_stats["errors"]),
    }


def _item_data(
    group: dedup_stage.Group, plan: _Plan, *, reaches_covers: bool, output: Path | None
) -> dict:
    verdict = plan.verdicts[group.winner]
    facts = plan.facts_by_path.get(group.winner)
    cover = plan.cover_results.get(group.winner) if reaches_covers else None
    return {
        "book_id": plan.book_ids.get(group.winner),
        "format": facts.fmt if facts else None,
        "size": facts.size if facts else None,
        "meta": {
            "title": facts.meta_title if facts else None,
            "author": facts.meta_author if facts else None,
            "language": facts.meta_language if facts else None,
        },
        "title": verdict.title,
        "author": verdict.author,
        "language": verdict.language,
        "origin": verdict.source,
        "language_origin": verdict.language_origin,
        "duplicates": [str(p) for p in sorted(group.members, key=str) if p != group.winner],
        "cover_source": cover.source if cover else None,
        "output": str(output) if output else None,
    }


def _run_pipeline(
    paths: list[Path],
    overrides: dict[Path, tuple[str | None, str | None, str | None]],
    args,
    *,
    stop_after: str,
    reporter: Reporter,
    batch_dir: Path,
    cache_dir: Path,
    options: dict,
    prefer: tuple[str, ...],
    api_key: str | None,
    llm_enabled: bool,
) -> int:
    dry_run = args.dry_run
    stages = list(STAGE_ORDER[: _stage_index(stop_after) + 1])

    # `start` is emitted BEFORE the expensive planning phase below — metadata reads
    # across the whole library, the LLM normalize/dedup passes, cover resolution —
    # rather than after it. For a library of any size that phase is minutes of work
    # plus network calls: the single most likely place a person presses Ctrl+C, and
    # until `start` reaches stdout a --json caller has nothing to make sense of.
    # `items` is the raw source count; the true post-dedup item count is only known
    # once planning finishes and is what `result.counts` reports. Unlike before
    # (I5), the `stage` events themselves are NOT all announced here up front —
    # `_build_plan` emits each one as that stage's work actually begins, and wires
    # real progress events (metadata/normalize/covers) in between them; "convert"/
    # "verify"/"organize" are announced further down, the same way.
    reporter.start(
        tool=NAME,
        batch=batch_dir.name,
        output_dir=batch_dir,
        stages=stages,
        items=len(paths),
        options=options,
    )

    try:
        plan = _build_plan(
            paths,
            overrides,
            args=args,
            stop_after=stop_after,
            dry_run=dry_run,
            llm_enabled=llm_enabled,
            api_key=api_key,
            cache_dir=cache_dir,
            prefer=prefer,
            reporter=reporter,
            stages=stages,
        )
    except KeyboardInterrupt:
        # No batch was ever opened yet (planning runs before RunState.open()), so
        # there is nothing for `run_file` to point at — same shape `empty_result`
        # already gives a batch-conflict abort.
        reporter.error(code="interrupted", message="interrupted by user")
        reporter.result(**empty_result(EXIT_INTERRUPTED))
        return EXIT_INTERRUPTED

    if dry_run:
        return _report_dry_run(
            plan,
            reporter=reporter,
            batch_dir=batch_dir,
            options=options,
            stop_after=stop_after,
            args=args,
        )

    try:
        state_cm = RunState.open(
            batch_dir,
            task=NAME,
            options=options,
            inputs=[str(p) for p in paths],
            force=args.force,
        )
    except BatchInUse as error:
        reporter.error(code="batch_in_use", message=str(error))
        reporter.result(**empty_result(EXIT_USAGE))
        return EXIT_USAGE
    except BatchTaskMismatch as error:
        reporter.error(code="batch_task_mismatch", message=str(error))
        reporter.result(**empty_result(EXIT_USAGE))
        return EXIT_USAGE
    except OSError as error:
        reporter.error(
            code="output_not_writable",
            message=f"cannot create batch directory {batch_dir}: {error}",
            hint="pass -o/--output-dir to a writable location, or fix permissions on this one",
        )
        reporter.result(**empty_result(EXIT_DEPENDENCY))
        return EXIT_DEPENDENCY

    reaches_convert = _stage_index(stop_after) >= _stage_index("convert")
    reaches_covers = _stage_index(stop_after) >= _stage_index("covers")
    reaches_organize = stop_after == "organize"

    exit_code = EXIT_OK
    with state_cm as state:
        for path in sorted(plan.dropped, key=str):
            state.add_item(str(path))
        for group in sorted(plan.groups, key=lambda g: str(g.winner)):
            state.add_item(str(group.winner))

        convert_plan = None  # initialized here so a KeyboardInterrupt during the
        # dropped-items loop below (before this is otherwise assigned) can still
        # report placement-free `result.data` instead of raising NameError.
        try:
            item_id = 0
            for path in sorted(plan.dropped, key=str):
                item_id += 1
                message = plan.dropped[path]
                state.update(
                    item_id,
                    status="failed",
                    reason="source_missing",
                    bytes_in=None,
                    warnings=[],
                    data={"error": message},
                    outputs=[],
                )
                reporter.item(
                    id=item_id,
                    status="failed",
                    input=path,
                    outputs=[],
                    bytes_in=None,
                    reason="source_missing",
                )
                exit_code = EXIT_FAILED
                if args.stop_on_error:
                    state.finish("failed")
                    return _finish_and_report(
                        state, batch_dir, reporter, exit_code, plan, args, convert_plan=None
                    )

            convert_errors: dict[Path, str] = {}
            if reaches_convert:
                _announce_stage(reporter, "convert", stages)
                convert_plan = _plan_and_reconcile(
                    plan, batch_dir=batch_dir, to=args.to, force=args.force, cache_dir=cache_dir
                )
                _report_leftovers(convert_plan["leftover"], batch_dir=batch_dir, reporter=reporter)
                # RB23: `convert_plan["rename_errors"]` comes from `reconcile()`'s
                # `before_rename` hook — a metadata rewrite attempted (and, here,
                # failed) on a twin BEFORE it was ever renamed, so merging it in
                # alongside a real conversion failure is exactly the same
                # "this book has no valid output this run" signal to `_finalize_group`.
                convert_errors.update(convert_plan["rename_errors"])
                workers = args.workers or min(4, os.cpu_count() or 1)
                convert_errors.update(
                    _convert_missing_books(
                        plan, convert_plan, cache_dir=cache_dir, to=args.to, workers=workers
                    )
                )

            if reaches_organize:
                _announce_stage(reporter, "verify", stages)
                _announce_stage(reporter, "organize", stages)

            for group in sorted(plan.groups, key=lambda g: str(g.winner)):
                item_id += 1
                outcome = _finalize_group(
                    group,
                    plan,
                    convert_plan=convert_plan,
                    convert_errors=convert_errors,
                    reaches_convert=reaches_convert,
                    reaches_covers=reaches_covers,
                    reaches_organize=reaches_organize,
                    cache_dir=cache_dir,
                )
                bytes_in = plan.facts_by_path.get(group.winner)
                bytes_in = bytes_in.size if bytes_in else None
                state.update(
                    item_id,
                    status=outcome["status"],
                    reason=outcome["reason"],
                    bytes_in=bytes_in,
                    warnings=outcome["warnings"],
                    data=outcome["data"],
                    outputs=(
                        [
                            {
                                "path": str(outcome["output"].relative_to(batch_dir)),
                                "bytes": outcome["output"].stat().st_size,
                            }
                        ]
                        if outcome["output"] and outcome["output"].exists()
                        else []
                    ),
                )
                reporter.item(
                    id=item_id,
                    status=outcome["status"],
                    input=group.winner,
                    outputs=[outcome["output"]] if outcome["output"] else [],
                    bytes_in=bytes_in,
                    bytes_out=(
                        outcome["output"].stat().st_size
                        if outcome["output"] and outcome["output"].exists()
                        else None
                    ),
                    reason=outcome["reason"],
                    warnings=outcome["warnings"],
                )
                if outcome["status"] == "failed":
                    exit_code = EXIT_FAILED
                    if args.stop_on_error:
                        break
        except KeyboardInterrupt:
            state.finish("interrupted")
            reporter.error(code="interrupted", message="interrupted by user")
            result = build_result(state, batch_dir, EXIT_INTERRUPTED)
            result["data"] = _result_data(plan, convert_plan)
            reporter.result(**result)
            write_summary_json(args.summary_json, result)
            return EXIT_INTERRUPTED

        state.finish("done" if exit_code == EXIT_OK else "failed")

    return _finish_and_report(state, batch_dir, reporter, exit_code, plan, args, convert_plan)


def _result_data(plan: _Plan, convert_plan: dict | None) -> dict:
    """The final `result` event's (and `--summary-json`'s) `data` payload: the LLM
    cost summary every run has had since Task 12, plus (I4 — new) `placement`'s
    kept/renamed/leftover counts whenever this run actually reached the convert
    stage. Those three were computed by `reconcile()` all along but never surfaced
    anywhere a caller could see them."""
    data: dict = {"llm": _llm_summary(plan)}
    if convert_plan is not None:
        data["placement"] = {
            "kept": convert_plan["kept"],
            "renamed": convert_plan["renamed"],
            "leftover": len(convert_plan["leftover"]),
        }
    return data


def _finish_and_report(state, batch_dir, reporter, exit_code, plan, args, convert_plan=None) -> int:
    result = build_result(state, batch_dir, exit_code)
    result["data"] = _result_data(plan, convert_plan)
    reporter.result(**result)
    write_summary_json(args.summary_json, result)
    return exit_code


# A leftover is reported by name up to this many files; past it, one summarising
# warning is emitted instead of flooding the stream with one line per file (I4).
_LEFTOVER_WARNING_NAMED_LIMIT = 5


def _report_leftovers(leftover: list[Path], *, batch_dir: Path, reporter: Reporter) -> None:
    """I4: `reconcile()` already computes exactly which files matched nothing in
    this plan and swept them to `_leftover/` — silently, before this fix: no item,
    no warning, no `run.json` entry named them. This does not add an `item` (a
    leftover was never part of the plan to begin with, so it has no book id, no
    verdict, nothing an `item` event's shape expects) but it does make the move
    visible as a `warning`, by name when there are only a few."""
    if not leftover:
        return
    if len(leftover) <= _LEFTOVER_WARNING_NAMED_LIMIT:
        for path in sorted(leftover, key=str):
            try:
                relative = path.relative_to(batch_dir)
            except ValueError:
                relative = path
            reporter.warning(code="leftover_book", message=f"moved to _leftover/: {relative}")
    else:
        reporter.warning(
            code="leftover_book",
            message=f"{len(leftover)} files matched nothing in this plan; moved to _leftover/",
        )


def _plan_and_reconcile(plan: _Plan, *, batch_dir: Path, to: str, force: bool, cache_dir: Path):
    entries = {
        group.winner: (plan.verdicts[group.winner], to, plan.book_ids[group.winner])
        for group in plan.groups
    }
    planned, collisions = library.plan_placement(batch_dir, entries)
    if force:
        for target in planned.values():
            if target.exists():
                target.unlink()
    book_ids = {source: plan.book_ids[source] for source in planned}

    # RB23: rewrite a twin's embedded metadata BEFORE it is renamed, not after —
    # `reconcile()` only performs the rename once this returns True, so a failure
    # here leaves the twin under its old name/content instead of stranding it at a
    # new path whose name contradicts what the file actually contains (RB20/C1's
    # own bug, one layer deeper: renaming first and rewriting after meant a failed
    # rewrite still left the file "renamed" and never eligible for a retry, since a
    # later run would see the target already occupied and call it `kept`).
    rename_errors: dict[Path, str] = {}

    def _rewrite_before_rename(twin: Path, source: Path) -> bool:
        verdict = plan.verdicts[source]
        try:
            calibre.update_metadata(
                twin,
                title=verdict.title,
                author=verdict.author,
                language=verdict.language,
                cache_dir=cache_dir,
            )
        except calibre.CalibreError as error:
            rename_errors[source] = str(error)
            return False
        return True

    report = library.reconcile(
        batch_dir,
        planned,
        book_ids,
        dry_run=False,
        force=force,
        before_rename=_rewrite_before_rename,
    )
    return {
        "planned": planned,
        "collisions": collisions,
        "missing": set(report.missing),
        "kept": report.kept,
        "renamed": report.renamed,
        "leftover": report.leftover,
        "rename_errors": rename_errors,
    }


def _verify_output(target: Path, verdict: normalize_stage.Verdict, cache_dir: Path):
    """Re-read a produced file (spec 8.2's verify stage). Returns
    `(ok, warnings, title_mismatch)`: `ok=False` means the file could not be
    confirmed at all (`engine_error`); `title_mismatch` is `True` only when the
    file was readable but its title genuinely did not match — RB23's self-heal
    (`_finalize_group`, below) repairs exactly this one failure mode with one
    rewrite-and-reverify attempt, and nothing else (an unreadable file is a
    different class of problem an in-place metadata rewrite is unlikely to fix)."""
    warnings: list[str] = []
    try:
        meta = calibre.read_metadata(target, cache_dir=cache_dir)
    except calibre.CalibreError:
        return False, warnings, False
    if meta.title is None or meta.title.strip() != verdict.title.strip():
        return False, warnings, True
    if target.suffix.lower().lstrip(".") in KINDLE_FORMATS:
        records = exth.read_records(target)
        if not exth.record_text(records, exth.TAG_UUID):
            warnings.append("book_id_missing")
        # M5: 201/202 are binary offset records, not text — decoding them as UTF-8
        # only to test presence (the old code) is the wrong tool for the job and
        # can occasionally "succeed" on garbage; a plain key check is what presence
        # actually means here.
        if records.get(exth.TAG_COVER_OFFSET) is None or records.get(exth.TAG_THUMB_OFFSET) is None:
            warnings.append("cover_not_embedded")
    return True, warnings, False


def _finalize_group(
    group: dedup_stage.Group,
    plan: _Plan,
    *,
    convert_plan,
    convert_errors: dict[Path, str],
    reaches_convert: bool,
    reaches_covers: bool,
    reaches_organize: bool,
    cache_dir: Path,
) -> dict:
    verdict = plan.verdicts[group.winner]

    if not reaches_convert or convert_plan is None:
        data = _item_data(group, plan, reaches_covers=reaches_covers, output=None)
        return {"status": "done", "reason": None, "warnings": [], "data": data, "output": None}

    target = convert_plan["planned"][group.winner]
    warnings = ["name_collision_suffixed"] if group.winner in convert_plan["collisions"] else []

    # C1/RB23: `convert_errors` holds BOTH a real conversion failure (a book that
    # needed `ebook-convert`) and a failed metadata rewrite on a twin `reconcile()`
    # tried to rename (`_plan_and_reconcile`'s `before_rename` hook, RB23) —
    # checking it before branching on `missing` means that book can fail here too,
    # instead of the old code's unconditional "skipped/exists" for anything
    # reconcile didn't have to convert. `output=None` is correct in both cases:
    # since RB23 rewrites BEFORE renaming, a failed rewrite here means the file was
    # never renamed either — nothing genuinely exists at `target`.
    error = convert_errors.get(group.winner)
    if error is not None:
        data = _item_data(group, plan, reaches_covers=reaches_covers, output=None)
        data["error"] = error
        return {
            "status": "failed",
            "reason": "engine_error",
            "warnings": warnings,
            "data": data,
            "output": None,
        }

    reused = group.winner not in convert_plan["missing"]
    if reused:
        # Already there (kept), or renamed into place by reconcile() with its
        # metadata already rewritten to match (RB23: rewritten BEFORE the rename,
        # so a file that reaches this point is honest by construction): nothing
        # left to convert.
        status, reason = "skipped", "exists"
    else:
        status, reason = "done", None

    if reaches_organize and target.exists():
        ok, verify_warnings, title_mismatch = _verify_output(target, verdict, cache_dir)
        if not ok and title_mismatch and reused:
            # RB23 point 2: this file was never touched THIS run (kept as-is, or
            # renamed in a run before its own metadata rewrite existed) — it can
            # still be sitting at its correct path with a stale embedded title, and
            # would otherwise fail forever with no path back to correct, exactly
            # C1's original defect one layer deeper. One rewrite-and-reverify
            # attempt self-heals it, including a book stranded on a user's machine
            # by the very version this fix replaces.
            try:
                calibre.update_metadata(
                    target,
                    title=verdict.title,
                    author=verdict.author,
                    language=verdict.language,
                    cache_dir=cache_dir,
                )
            except calibre.CalibreError as error:
                data = _item_data(group, plan, reaches_covers=reaches_covers, output=target)
                data["error"] = str(error)
                return {
                    "status": "failed",
                    "reason": "engine_error",
                    "warnings": warnings + verify_warnings,
                    "data": data,
                    "output": target,
                }
            ok, verify_warnings, _title_mismatch = _verify_output(target, verdict, cache_dir)
        warnings = warnings + verify_warnings
        if not ok:
            status, reason = "failed", "engine_error"

    data = _item_data(group, plan, reaches_covers=reaches_covers, output=target)
    return {
        "status": status,
        "reason": reason,
        "warnings": warnings,
        "data": data,
        "output": target,
    }


# Calibre's PDF input is memory-hungry; capped independently of --workers (Decision 7).
_PDF_SEMAPHORE = threading.Semaphore(2)


def _convert_one(
    source: Path,
    target: Path,
    *,
    verdict: normalize_stage.Verdict,
    book_id: str,
    cover: covers_stage.CoverResult | None,
    cache_dir: Path,
    to: str,
) -> str | None:
    """Convert one book straight to its final target. Returns an error message on
    failure, None on success. Safe to call from several threads at once: every
    scratch file this makes is uniquely named under `cache_dir` (mkstemp)."""
    is_pdf = source.suffix.lower() == ".pdf"
    if is_pdf:
        _PDF_SEMAPHORE.acquire()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        opf_handle, opf_name = tempfile.mkstemp(suffix=".opf", dir=cache_dir)
        os.close(opf_handle)
        opf_path = Path(opf_name)
        opf.write_opf(
            opf_path,
            title=verdict.title,
            author=verdict.author,
            language=verdict.language,
            book_uuid=book_id,
        )
        temp_handle, temp_name = tempfile.mkstemp(suffix=f".{to}", dir=cache_dir)
        os.close(temp_handle)
        temp = Path(temp_name)
        cover_path = cover.path if cover and cover.path else None
        try:
            calibre.convert(source, temp, opf=opf_path, cover=cover_path, cache_dir=cache_dir)
        except calibre.CalibreError as error:
            temp.unlink(missing_ok=True)
            return str(error)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        finally:
            opf_path.unlink(missing_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        fsync_replace(temp, target)
        return None
    finally:
        if is_pdf:
            _PDF_SEMAPHORE.release()


def _convert_missing_books(
    plan: _Plan, convert_plan, *, cache_dir: Path, to: str, workers: int
) -> dict[Path, str]:
    """Convert every winner `reconcile()` could not place for free, capped at
    `workers` concurrent conversions (Decision 7); PDF sources are further capped
    to 2 concurrent by `_convert_one`'s own semaphore regardless of `workers`.
    Returns {source: error message} for every conversion that failed; a source
    absent from the result converted successfully."""
    jobs = [group for group in plan.groups if group.winner in convert_plan["missing"]]
    if not jobs:
        return {}

    def _run(group: dedup_stage.Group) -> str | None:
        return _convert_one(
            group.winner,
            convert_plan["planned"][group.winner],
            verdict=plan.verdicts[group.winner],
            book_id=plan.book_ids[group.winner],
            cover=plan.cover_results.get(group.winner),
            cache_dir=cache_dir,
            to=to,
        )

    errors: dict[Path, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_run, group): group.winner for group in jobs}
        for future in as_completed(futures):
            source = futures[future]
            try:
                error = future.result()
            except Exception as exc:
                # C2: `_convert_one` already turns a `calibre.CalibreError` into a
                # returned message, but an OSError from `mkdir`/`write_opf`/
                # `mkstemp`/`fsync_replace` (a full disk, a read-only mount) is not
                # a CalibreError and used to propagate straight out of this
                # `future.result()`, past every caller's own handling, into
                # `cli.main`'s generic handler — killing the whole batch instead of
                # failing just this one book. Record it exactly like a CalibreError
                # would be.
                error = str(exc)
            if error is not None:
                errors[source] = error
    return errors


def _report_dry_run(
    plan: _Plan,
    *,
    reporter: Reporter,
    batch_dir: Path,
    options: dict,
    stop_after: str,
    args,
) -> int:
    """Report the plan as `item`/`result` events. `start` and every stage
    announcement reached so far were already emitted from inside `_build_plan`
    (see `_run_pipeline`), so this only ever adds `item`s and the final `result` —
    nothing here writes anything to disk.

    M2: the preview target for each book comes from `library.plan_placement` —
    the SAME function the real run uses via `_plan_and_reconcile` — not from
    calling `library.target_path` once per book in isolation. A lone `target_path`
    call cannot see any other book's target, so it can never produce the
    ` (2)`/` (3)` collision suffix `plan_placement` computes across the whole
    batch at once; a dry run using it was previewing a plan the real run would
    never actually produce."""
    total = len(plan.groups) + len(plan.dropped)
    reaches_convert = _stage_index(stop_after) >= _stage_index("convert")

    planned: dict[Path, Path] = {}
    collisions: dict[Path, str] = {}
    if reaches_convert:
        entries = {
            group.winner: (
                plan.verdicts[group.winner],
                options["to"],
                plan.book_ids.get(group.winner, ""),
            )
            for group in plan.groups
        }
        planned, collisions = library.plan_placement(batch_dir, entries)

    exit_code = EXIT_OK
    failed = []
    item_id = 0
    for path in sorted(plan.dropped, key=str):
        item_id += 1
        reporter.item(
            id=item_id,
            status="failed",
            input=path,
            outputs=[],
            bytes_in=None,
            reason="source_missing",
        )
        exit_code = EXIT_FAILED
        failed.append({"id": item_id, "input": str(path), "reason": "source_missing"})

    for group in sorted(plan.groups, key=lambda g: str(g.winner)):
        item_id += 1
        target = planned.get(group.winner) if reaches_convert else None
        reason = "exists" if target and target.exists() else None
        warnings = ["name_collision_suffixed"] if group.winner in collisions else []
        reporter.item(
            id=item_id,
            status="skipped",
            input=group.winner,
            outputs=[target] if target else [],
            bytes_in=None,
            reason=reason,
            warnings=warnings,
        )

    result = dict(
        ok=exit_code == EXIT_OK,
        exit_code=exit_code,
        counts={
            "total": total,
            "done": 0,
            "skipped": total - len(failed),
            "failed": len(failed),
            "pending": 0,
            "planned": total,
        },
        failed=failed,
        pending=[],
        outputs=[],
        run_file=None,
        data={"llm": _llm_summary(plan)},
    )
    reporter.result(**result)
    # M2: every other exit path honours --summary-json; a dry run used to be the
    # one silent exception, even though it reaches a normal end just like a real
    # run does (nothing about --dry-run makes the final result any less final).
    write_summary_json(args.summary_json, result)
    return exit_code
