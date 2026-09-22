"""split: cut media into size-limited parts. (stub — the engine arrives in Task 12.)"""

from __future__ import annotations

from media_tools.core.events import EXIT_DEPENDENCY
from media_tools.tasks.common import add_common_flags, prepare

NAME = "split"
HELP = "Split media into parts under a size limit."
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
