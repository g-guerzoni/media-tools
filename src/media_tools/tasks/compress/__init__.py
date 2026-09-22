"""compress: make media files smaller. (stub — the video engine arrives in Task 10.)"""

from __future__ import annotations

from media_tools.core.events import EXIT_DEPENDENCY
from media_tools.tasks.common import add_common_flags, prepare

NAME = "compress"
HELP = "Compress media files (video today; PDF and images later)."
ENGINES: list = []


def register(subparsers):
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP)
    add_common_flags(parser)
    return parser


def run(args) -> int:
    # With no engines registered yet, `prepare` always raises `UsageError` for any real
    # input (nothing can be produced, or nothing handles the given extension), so the
    # fallback below only matters if that invariant ever changes.
    prepared = prepare(args, task=NAME, engines=ENGINES)
    prepared.reporter.error(
        code="dependency_missing", message=f"the {NAME} task is not implemented yet"
    )
    return EXIT_DEPENDENCY
