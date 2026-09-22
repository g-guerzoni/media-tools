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
) -> None:
    parser.add_argument(
        "inputs",
        nargs="*",
        type=inputs_type,
        metavar=metavar,
        help="URLs to process." if metavar == "URL" else "Files and/or folders to process.",
    )
    parser.add_argument("-r", "--recursive", action="store_true", help="Scan subfolders.")
    parser.add_argument(
        "-e",
        "--extensions",
        default=None,
        help="Comma-separated extensions to pick from folders.",
    )
    parser.add_argument("--include", default=None, help="Only paths containing this text.")
    parser.add_argument("--exclude", default=None, help="Skip paths containing this text.")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N items.")
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
        name = (
            sanitize_batch(args.batch)
            if args.batch
            else batch_hash(
                task=task, options=options, selection=selection, inputs=list(args.inputs)
            )
        )
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
