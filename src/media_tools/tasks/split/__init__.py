"""split: cut a file into parts that fit a size limit."""

from __future__ import annotations

from media_tools.core.ffmpeg import ffmpeg_exe
from media_tools.core.runner import run_items
from media_tools.core.sizes import parse_size
from media_tools.tasks.common import UsageError, add_common_flags, prepare
from media_tools.tasks.split.media import MediaSplitEngine

NAME = "split"
HELP = "Split media into parts under a size limit."
ENGINES = [MediaSplitEngine()]
MINIMUM_BYTES = 1_000_000


def register(subparsers):
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP)
    add_common_flags(parser)
    group = parser.add_argument_group("split options")
    for engine in ENGINES:
        engine.add_arguments(group)
    return parser


def run(args) -> int:
    try:
        max_bytes = parse_size(args.max_size)
    except ValueError as error:
        raise UsageError(str(error)) from error
    if max_bytes < MINIMUM_BYTES:
        raise UsageError("--max-size must be at least 1MB")

    prepared = prepare(args, task=NAME, engines=ENGINES)
    deps = {"ffmpeg": ffmpeg_exe(), "max_bytes": max_bytes}
    return run_items(
        prepared.sources,
        task=NAME,
        engines=ENGINES,
        args=args,
        reporter=prepared.reporter,
        batch_dir=prepared.batch_dir,
        stages=["scan", "split", "verify"],
        options=prepared.options,
        dry_run=args.dry_run,
        force=args.force,
        stop_on_error=args.stop_on_error,
        deps=deps,
        summary_json=args.summary_json,
    )
