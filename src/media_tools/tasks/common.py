"""Flags and preparation shared by every file task."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from media_tools.core.engine import missing_dependencies, select_engine
from media_tools.core.events import EXIT_DEPENDENCY, EXIT_USAGE, Reporter
from media_tools.core.inputs import InputError, Source, expand_inputs, parse_extensions
from media_tools.core.paths import BatchNameError, batch_hash, output_root, sanitize_batch
from media_tools.core.state import resolve_batch_name


def _positive_limit(value: str) -> int:
    """--limit's argparse `type`: 0 matches nothing (silently, if not rejected here) and
    a negative value means `sources[:-N]`, which *drops* the last N items instead of
    limiting anything — both are surprising enough to reject outright rather than do
    what Python slicing happens to do with them."""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"--limit must be a positive integer, got {value!r}")
    return parsed


class UsageError(Exception):
    """A problem with the command line or its inputs; `main` maps this to an exit code."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "usage",
        hint: str | None = None,
        exit_code: int = EXIT_USAGE,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.exit_code = exit_code


@dataclass
class Prepared:
    reporter: Reporter
    sources: list[Source]
    batch_dir: Path
    options: dict
    deps: dict[str, str]


def add_common_flags(
    parser: argparse.ArgumentParser,
    *,
    inputs_type: Callable[[str], object] = Path,
    metavar: str = "INPUT",
    scan_flags: bool = True,
) -> None:
    parser.add_argument(
        "inputs",
        nargs="*",
        type=inputs_type,
        metavar=metavar,
        help="URLs to process." if metavar == "URL" else "Files and/or folders to process.",
    )
    if scan_flags:
        # -r/--recursive and -e/--extensions only mean something for a folder scan of
        # local files; `download`'s inputs are URLs, so it passes scan_flags=False and
        # never sees these two (they would otherwise show up in --help and silently do
        # nothing, since download.run() never reads args.recursive/args.extensions).
        parser.add_argument("-r", "--recursive", action="store_true", help="Scan subfolders.")
        parser.add_argument(
            "-e",
            "--extensions",
            default=None,
            help="Comma-separated extensions to pick from folders.",
        )
    parser.add_argument("--include", default=None, help="Only paths containing this text.")
    parser.add_argument("--exclude", default=None, help="Skip paths containing this text.")
    parser.add_argument(
        "--limit", type=_positive_limit, default=None, help="Process at most N items (> 0)."
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="Output root (default: MEDIA_TOOLS_OUT, the repo's media/, or ./media).",
    )
    parser.add_argument(
        "-b",
        "--batch",
        default=None,
        help="Batch folder name (default: a hash of task, options and inputs).",
    )
    parser.add_argument("--force", action="store_true", help="Redo items whose output exists.")
    parser.add_argument("--dry-run", action="store_true", help="Plan only; write nothing.")
    parser.add_argument(
        "--stop-on-error", action="store_true", help="Stop after the first failed item."
    )
    parser.add_argument(
        "--summary-json",
        type=Path,
        default=None,
        help="Also write the final result object to this path.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_mode",
        help="Emit JSON Lines events on stdout.",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Only errors and the summary.")


def prepare(args, *, task: str, engines: list, to: str | None = None) -> Prepared:
    reporter = Reporter(json_mode=args.json_mode, quiet=args.quiet)
    if not args.inputs:
        raise UsageError(f"no input given; see `media-tools {task} --help`")

    accepted = {
        ext for engine in engines for ext in engine.inputs if to is None or to in engine.outputs
    }
    # Only treat an empty accepted set as "nothing can produce `to`" when `to` was actually
    # requested. With no engines registered yet (the stub tasks), `accepted` is always empty
    # regardless of `to`; without this guard every stub would report the wrong reason for a
    # missing/empty input instead of letting `expand_inputs` raise its own, more specific error.
    if to is not None and not accepted:
        raise UsageError(f"nothing can produce {to!r}; run `media-tools formats`")

    def warn(code: str, message: str) -> None:
        reporter.warning(code=code, message=message)

    try:
        sources = expand_inputs(
            args.inputs,
            recursive=args.recursive,
            extensions=parse_extensions(args.extensions) or None,
            accepted=accepted,
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
            hint="check --extensions, or pass -r to scan subfolders",
        )

    used = {select_engine(engines, source.path, to) for source in sources}
    used.discard(None)
    if not used:
        raise UsageError(f"no engine handles {sources[0].path.suffix}")
    for engine in used:
        missing = missing_dependencies(engine)
        if missing:
            raise UsageError(
                f"missing dependency: {missing[0].name}",
                code="dependency_missing",
                hint=missing[0].install_hint,
                exit_code=EXIT_DEPENDENCY,
            )

    # Options come from the engine that will do the work; when a run mixes engines,
    # each engine only reads the flags it declared, so merging them is safe.
    options: dict = {}
    for engine in sorted(used, key=lambda e: e.name):
        options.update(engine.hash_options(args))
    selection = {
        "recursive": args.recursive,
        "extensions": sorted(parse_extensions(args.extensions)),
    }
    root = output_root(args.output_dir)
    try:
        if args.batch:
            name = sanitize_batch(args.batch)
        else:
            base_name = batch_hash(
                task=task, options=options, selection=selection, inputs=list(args.inputs)
            )
            # Spec 7.1: a generated name whose run.json belongs to a different task or
            # options is not this run's batch to silently reuse or collide with —
            # append -2, -3, ... until a free or matching one is found. An explicit
            # --batch is never adjusted this way; RunState.open refuses (or, with
            # --force, updates) it instead.
            name = resolve_batch_name(root, base_name, task=task, options=options)
    except BatchNameError as error:
        raise UsageError(str(error)) from error

    deps = {dep.name: dep.locate() for engine in used for dep in engine.dependencies}
    return Prepared(
        reporter=reporter,
        sources=sources,
        batch_dir=root / name,
        options=options,
        deps=deps,
    )
