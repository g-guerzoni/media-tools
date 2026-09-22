"""compress: make media files smaller. (video today; PDF and images later)."""

from __future__ import annotations

from media_tools.core.runner import run_items
from media_tools.tasks.common import UsageError, add_common_flags, prepare
from media_tools.tasks.compress.video import PRESETS, VideoEngine

NAME = "compress"
HELP = "Compress media files (video today; PDF and images later)."
ENGINES = [VideoEngine()]


def register(subparsers):
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP)
    add_common_flags(parser)
    group = parser.add_argument_group("video options")
    for engine in ENGINES:
        engine.add_arguments(group)
    return parser


def run(args) -> int:
    if args.crf is not None and not 0 <= args.crf <= 51:
        raise UsageError(f"--crf must be between 0 and 51, got {args.crf}")
    if args.preset not in PRESETS:
        raise UsageError(f"unknown preset: {args.preset}")

    prepared = prepare(args, task=NAME, engines=ENGINES)
    deps = {"ffmpeg": prepared.deps["ffmpeg"], "options": prepared.options}
    return run_items(
        prepared.sources,
        task=NAME,
        engines=ENGINES,
        args=args,
        reporter=prepared.reporter,
        batch_dir=prepared.batch_dir,
        stages=["scan", "encode"],
        options=prepared.options,
        dry_run=args.dry_run,
        force=args.force,
        stop_on_error=args.stop_on_error,
        deps=deps,
        summary_json=args.summary_json,
    )
