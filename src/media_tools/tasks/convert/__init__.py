"""convert: change media formats. (stub — the audio engine arrives in Task 11.)"""

from __future__ import annotations

from media_tools.core.events import EXIT_DEPENDENCY
from media_tools.tasks.common import add_common_flags, prepare

NAME = "convert"
HELP = "Convert media to a different format (e.g. video/audio to mp3)."
ENGINES: list = []


def register(subparsers):
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP)
    add_common_flags(parser)
    parser.add_argument("--to", required=True, help="Target format, e.g. mp3 (required).")
    return parser


def run(args) -> int:
    # With no engines registered yet, `prepare` always raises `UsageError` for any real
    # input (nothing can produce `--to`, or nothing handles the given extension), so the
    # fallback below only matters if that invariant ever changes.
    prepared = prepare(args, task=NAME, engines=ENGINES, to=args.to)
    prepared.reporter.error(
        code="dependency_missing", message=f"the {NAME} task is not implemented yet"
    )
    return EXIT_DEPENDENCY
