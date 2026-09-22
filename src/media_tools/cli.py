"""media-tools: one command, one subcommand per task."""

from __future__ import annotations

import argparse
import sys

from media_tools import __version__
from media_tools.core.events import EXIT_USAGE, Reporter
from media_tools.tasks import compress, convert, doctor, download, ebook, formats, split, status
from media_tools.tasks.common import UsageError

TASKS = [compress, convert, split, download, ebook, formats, doctor, status]


class _JSONAwareArgumentParser(argparse.ArgumentParser):
    """argparse's own usage errors (unknown subcommand, a missing required flag, a bad
    flag type, ...) normally only print text to stderr and exit, leaving stdout silent
    even under --json. This subclass also emits the matching `error` event on stdout
    first, through the same Reporter used everywhere else, so an agent driving the tool
    with --json never sees an empty stdout on a usage error argparse rejects on its own."""

    _argv: list[str] | None = None

    def parse_known_args(self, args=None, namespace=None):
        self._argv = list(sys.argv[1:] if args is None else args)
        return super().parse_known_args(args, namespace)

    def error(self, message: str) -> None:
        if self._argv is not None and "--json" in self._argv:
            Reporter(json_mode=True, quiet=False).error(
                code="usage", message=message, hint=self.format_usage().strip()
            )
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _JSONAwareArgumentParser(
        prog="media-tools",
        description="Compress, convert, split, download and organise media and ebooks.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="task", parser_class=_JSONAwareArgumentParser)
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
