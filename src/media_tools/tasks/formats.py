"""formats: list what each task and engine can produce. (stub — Task 14 replaces this.)"""

from __future__ import annotations

from media_tools.core.events import EXIT_DEPENDENCY, Reporter

NAME = "formats"
HELP = "List supported input/output formats and their requirements."


def register(subparsers):
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP)
    parser.add_argument("--json", action="store_true", dest="json_mode")
    parser.add_argument("-q", "--quiet", action="store_true")
    return parser


def run(args) -> int:
    Reporter(json_mode=args.json_mode, quiet=args.quiet).error(
        code="dependency_missing", message="the formats task is not implemented yet"
    )
    return EXIT_DEPENDENCY
