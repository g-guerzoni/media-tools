"""ebook: build a library and manage a Kindle. (stub — Plan B replaces this.)"""

from __future__ import annotations

from media_tools.core.events import EXIT_DEPENDENCY, Reporter

NAME = "ebook"
HELP = "Build an ebook library and manage a Kindle."


def register(subparsers):
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP)
    parser.add_argument("--json", action="store_true", dest="json_mode")
    parser.add_argument("-q", "--quiet", action="store_true")
    return parser


def run(args) -> int:
    # "config_missing", not "dependency_missing": nothing is missing from this machine
    # to go install — the task itself just doesn't exist yet, and "dependency_missing"
    # would tell an agent to go find and install something for a problem no install
    # fixes. The registry is closed, so this is the nearest honest code available.
    Reporter(json_mode=args.json_mode, quiet=args.quiet).error(
        code="config_missing", message="the ebook task is not implemented yet"
    )
    return EXIT_DEPENDENCY
