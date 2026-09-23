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
SUBCOMMANDS = {
    "scan": ("scan", "Inventory sources only; converts nothing."),
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


def _apply_language_fallback(
    verdicts: dict[Path, normalize_stage.Verdict],
    facts_by_path: dict[Path, metadata_stage.BookFacts],
) -> None:
    """Spec 8.2's offline chain is "title heuristic, then the embedded language
    field, else unknown" — but `normalize.heuristic()` (and the LLM path's own
    fallback) only ever calls `language.detect(title, author)`, which is
    deliberately conservative and returns None for most short/ambiguous titles
    (see its own module docstring). Filling in the embedded language here, once,
    for every verdict that still has none, is what actually gets a title like
    "Dom Casmurro" (no stopword the detector can key on) shelved under `pt/`
    instead of `_review/unknown-language/`."""
    for path, verdict in list(verdicts.items()):
        if verdict.language:
            continue
        facts = facts_by_path.get(path)
        if facts is None or not facts.meta_language:
            continue
        code = facts.meta_language.strip().lower()
        if len(code) == 2 and code.isalpha():
            verdicts[path] = replace(verdict, language=code)


def _apply_list_overrides(
    verdicts: dict[Path, normalize_stage.Verdict],
    overrides: dict[Path, tuple[str | None, str | None, str | None]],
) -> None:
    for path, (title, author, language) in overrides.items():
        base = verdicts.get(path)
        if base is None:
            continue
        verdicts[path] = replace(
            base,
            title=title if title else base.title,
            author=author if author else base.author,
            language=language if language else base.language,
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
            # "config_missing", not "dependency_missing": nothing to install fixes a
            # missing key. The hint is the resolver's own message, which already
            # names all three ways to supply one plus --no-llm (Decision 5).
            raise UsageError(
                "no OpenRouter API key available",
                code="config_missing",
                hint=str(error),
                exit_code=EXIT_DEPENDENCY,
            ) from error

    # `scan` never touches Calibre at all (pure filesystem listing); every other
    # subcommand reads metadata (ebook-meta) at minimum, and `build` also converts
    # (ebook-convert). Checked once, up front, so a real run fails fast with one
    # clear message instead of every single item failing with a cryptic engine_error.
    if not args.dry_run and stop_after != "scan":
        for dep in (calibre.EBOOK_CONVERT, calibre.EBOOK_META):
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

    if stop_after == "scan":
        return _run_scan_only(paths, reporter=reporter, batch_dir=batch_dir, options=options)

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


def _run_scan_only(paths: list[Path], *, reporter: Reporter, batch_dir: Path, options: dict) -> int:
    """`ebook scan`: inventory only, no metadata read, converts nothing."""
    stages = ["scan"]
    reporter.start(
        tool=NAME,
        batch=batch_dir.name,
        output_dir=batch_dir,
        stages=stages,
        items=len(paths),
        options=options,
    )
    reporter.stage(stage="scan", index=1, count=1)
    for index, path in enumerate(paths, start=1):
        exists = path.is_file()
        reporter.item(
            id=index,
            status="done" if exists else "failed",
            input=path,
            outputs=[],
            bytes_in=path.stat().st_size if exists else None,
            reason=None if exists else "source_missing",
        )
    ok = all(p.is_file() for p in paths)
    reporter.result(
        ok=ok,
        exit_code=EXIT_OK if ok else EXIT_FAILED,
        counts={
            "total": len(paths),
            "done": sum(1 for p in paths if p.is_file()),
            "skipped": 0,
            "failed": sum(1 for p in paths if not p.is_file()),
            "pending": 0,
        },
        failed=[],
        pending=[],
        outputs=[],
        run_file=None,
    )
    return EXIT_OK if ok else EXIT_FAILED


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
) -> _Plan:
    dropped: dict[Path, str] = {}

    def on_error(path: Path, message: str) -> None:
        dropped[path] = message

    facts = metadata_stage.read_all(
        paths, cache_dir=(None if dry_run else cache_dir), workers=8, on_error=on_error
    )
    facts_by_path = {f.path: f for f in facts}

    if llm_enabled:
        verdicts, normalize_stats = normalize_stage.classify(
            facts, model=args.model, api_key=api_key, cache_dir=(None if dry_run else cache_dir)
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

    _apply_language_fallback(verdicts, facts_by_path)
    _apply_list_overrides(verdicts, overrides)

    formats = {f.path: f.fmt for f in facts}
    groups = dedup_stage.group(verdicts, preference=prefer, formats=formats)
    dedup_stats = {"llm_calls": 0, "buckets": 0, "merges": 0, "errors": 0, "blocked_merges": 0}
    if llm_enabled and _stage_index(stop_after) >= _stage_index("dedup"):
        groups, dedup_stats = dedup_stage.refine(
            groups, verdicts, formats, model=args.model, api_key=api_key, preference=prefer
        )

    cover_results: dict[Path, covers_stage.CoverResult] = {}
    book_ids: dict[Path, str] = {}
    if _stage_index(stop_after) >= _stage_index("covers"):
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
            cover_results = covers_stage.resolve(
                books,
                cache_dir=cache_dir,
                fetch=(not args.no_cover_fetch),
                workers=(args.workers or 4),
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
    )

    stages = list(STAGE_ORDER[: _stage_index(stop_after) + 1])
    total_items = len(plan.groups) + len(plan.dropped)

    if dry_run:
        return _report_dry_run(
            plan,
            stages=stages,
            reporter=reporter,
            batch_dir=batch_dir,
            options=options,
            stop_after=stop_after,
        )

    reporter.start(
        tool=NAME,
        batch=batch_dir.name,
        output_dir=batch_dir,
        stages=stages,
        items=total_items,
        options=options,
    )
    for index, stage_name in enumerate(stages, start=1):
        reporter.stage(stage=stage_name, index=index, count=len(stages))

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
                    return _finish_and_report(state, batch_dir, reporter, exit_code, plan, args)

            convert_plan = None
            convert_errors: dict[Path, str] = {}
            if reaches_convert:
                convert_plan = _plan_and_reconcile(
                    plan, batch_dir=batch_dir, to=args.to, force=args.force
                )
                workers = args.workers or min(4, os.cpu_count() or 1)
                convert_errors = _convert_missing_books(
                    plan, convert_plan, cache_dir=cache_dir, to=args.to, workers=workers
                )

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
            result["data"] = {"llm": _llm_summary(plan)}
            reporter.result(**result)
            write_summary_json(args.summary_json, result)
            return EXIT_INTERRUPTED

        state.finish("done" if exit_code == EXIT_OK else "failed")

    return _finish_and_report(state, batch_dir, reporter, exit_code, plan, args)


def _finish_and_report(state, batch_dir, reporter, exit_code, plan, args) -> int:
    result = build_result(state, batch_dir, exit_code)
    result["data"] = {"llm": _llm_summary(plan)}
    reporter.result(**result)
    write_summary_json(args.summary_json, result)
    return exit_code


def _plan_and_reconcile(plan: _Plan, *, batch_dir: Path, to: str, force: bool):
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
    report = library.reconcile(batch_dir, planned, book_ids, dry_run=False)
    return {
        "planned": planned,
        "collisions": collisions,
        "missing": set(report.missing),
        "kept": report.kept,
        "renamed": report.renamed,
        "leftover": report.leftover,
    }


def _verify_output(target: Path, verdict: normalize_stage.Verdict, cache_dir: Path):
    """Re-read a produced file (spec 8.2's verify stage). Returns (ok, warnings)
    where ok=False means the file could not be confirmed at all (engine_error)."""
    warnings: list[str] = []
    try:
        meta = calibre.read_metadata(target, cache_dir=cache_dir)
    except calibre.CalibreError:
        return False, warnings
    if meta.title is None or meta.title.strip() != verdict.title.strip():
        return False, warnings
    if target.suffix.lower().lstrip(".") in KINDLE_FORMATS:
        records = exth.read_records(target)
        if not exth.record_text(records, exth.TAG_UUID):
            warnings.append("book_id_missing")
        if not (
            exth.record_text(records, exth.TAG_COVER_OFFSET)
            and exth.record_text(records, exth.TAG_THUMB_OFFSET)
        ):
            warnings.append("cover_not_embedded")
    return True, warnings


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

    if group.winner not in convert_plan["missing"]:
        # Already there (kept) or renamed into place by reconcile(): nothing to convert.
        status, reason = "skipped", "exists"
    else:
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
        status, reason = "done", None

    if reaches_organize and target.exists():
        ok, verify_warnings = _verify_output(target, verdict, cache_dir)
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
            error = future.result()
            if error is not None:
                errors[source] = error
    return errors


def _report_dry_run(
    plan: _Plan,
    *,
    stages: list[str],
    reporter: Reporter,
    batch_dir: Path,
    options: dict,
    stop_after: str,
) -> int:
    total = len(plan.groups) + len(plan.dropped)
    reporter.start(
        tool=NAME,
        batch=batch_dir.name,
        output_dir=batch_dir,
        stages=stages,
        items=total,
        options=options,
    )
    reaches_convert = _stage_index(stop_after) >= _stage_index("convert")

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
        target = None
        reason = None
        if reaches_convert:
            verdict = plan.verdicts[group.winner]
            target = library.target_path(batch_dir, verdict, options["to"])
            if target.exists():
                reason = "exists"
        reporter.item(
            id=item_id,
            status="skipped",
            input=group.winner,
            outputs=[target] if target else [],
            bytes_in=None,
            reason=reason,
        )

    reporter.result(
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
    return exit_code
