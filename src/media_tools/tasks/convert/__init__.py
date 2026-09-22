"""convert: turn a file into another format."""

from __future__ import annotations

from media_tools.core.ffmpeg import ffmpeg_exe
from media_tools.core.runner import run_items
from media_tools.tasks.common import UsageError, add_common_flags, prepare
from media_tools.tasks.convert.audio import AudioEngine

NAME = "convert"
HELP = "Convert media to a different format (e.g. video/audio to mp3)."
ENGINES = [AudioEngine()]


def register(subparsers):
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP)
    add_common_flags(parser)
    parser.add_argument(
        "--to", required=True, help="Target format (run `media-tools formats` to list them)."
    )
    group = parser.add_argument_group("conversion options")
    for engine in ENGINES:
        engine.add_arguments(group)
    return parser


def run(args) -> int:
    targets = {fmt for engine in ENGINES for fmt in engine.outputs}
    if args.to not in targets:
        raise UsageError(f"cannot convert to {args.to!r}; supported: {', '.join(sorted(targets))}")

    prepared = prepare(args, task=NAME, engines=ENGINES, to=args.to)
    deps = {"ffmpeg": ffmpeg_exe(), "options": prepared.options}
    return run_items(
        prepared.sources,
        task=NAME,
        engines=ENGINES,
        to=args.to,
        args=args,
        reporter=prepared.reporter,
        batch_dir=prepared.batch_dir,
        stages=["scan", "convert"],
        options=prepared.options,
        dry_run=args.dry_run,
        force=args.force,
        stop_on_error=args.stop_on_error,
        deps=deps,
        summary_json=args.summary_json,
    )
