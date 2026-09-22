"""media-tools: one command, one subcommand per task."""

from __future__ import annotations

import argparse
import sys

from media_tools import __version__
from media_tools.core.events import EXIT_USAGE, Reporter
from media_tools.tasks import compress, convert, doctor, download, ebook, formats, split, status
from media_tools.tasks.common import UsageError

TASKS = [compress, convert, split, download, ebook, formats, doctor, status]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media-tools",
        description="Compress, convert, split, download and organise media and ebooks.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="task")
    for task in TASKS:
        task.register(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "task", None):
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    task = next(t for t in TASKS if args.task == t.NAME)
    try:
        return task.run(args)
    except UsageError as error:
        reporter = Reporter(
            json_mode=getattr(args, "json_mode", False),
            quiet=getattr(args, "quiet", False),
        )
        reporter.error(code=error.code, message=str(error), hint=error.hint)
        return error.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
