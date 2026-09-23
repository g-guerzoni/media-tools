"""media-tools: one command, one subcommand per task."""

from __future__ import annotations

import argparse
import sys

from media_tools import __version__
from media_tools.core.events import EXIT_FAILED, EXIT_INTERRUPTED, EXIT_USAGE, Reporter
from media_tools.core.runner import empty_result
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
        # A `UsageError` fires before a task's own `start` (it is exactly the
        # "validation failed before run_items/_download_all began" case) — but the
        # plan's own global contract is that `result` is the LAST line of every run,
        # including on failure, with no carve-out for "the run never started". Emitting
        # only `error` here (as this used to) left that contract false for every task's
        # earliest failures (a missing input, an unknown subcommand's own bad flag,
        # ...) while `KeyboardInterrupt` and the catch-all below both already emit a
        # matching `result`. `empty_result` is exactly what those two already use for a
        # run that never owned a batch.
        reporter = Reporter(
            json_mode=getattr(args, "json_mode", False),
            quiet=getattr(args, "quiet", False),
        )
        reporter.error(code=error.code, message=str(error), hint=error.hint)
        reporter.result(**empty_result(error.exit_code))
        return error.exit_code
    except KeyboardInterrupt:
        # C2: this outer handler is the last resort for a task whose own code (a
        # `_run_pipeline` planning phase, a batch loop, ...) let a KeyboardInterrupt
        # escape uncaught. It must leave the same guarantee every task-specific
        # interrupt handler already gives: an `error` AND a matching `result` (exit
        # 130), never a bare exit with nothing on stdout for a --json caller to parse.
        reporter = Reporter(
            json_mode=getattr(args, "json_mode", False),
            quiet=getattr(args, "quiet", False),
        )
        reporter.error(code="interrupted", message="interrupted by user")
        reporter.result(**empty_result(EXIT_INTERRUPTED))
        return EXIT_INTERRUPTED
    except Exception as error:
        # The net beneath every task-specific handler above: anything a task's own code
        # did not anticipate (a real bug, not a usage problem) must still leave an agent
        # driving `--json` with a `result`-shaped final line instead of a bare traceback
        # on stderr and a silent, event-less stdout.
        reporter = Reporter(
            json_mode=getattr(args, "json_mode", False),
            quiet=getattr(args, "quiet", False),
        )
        reporter.error(
            code="internal_error",
            message=f"{type(error).__name__}: {error}",
            hint="this is a bug in media-tools",
            retryable=False,
        )
        reporter.result(**empty_result(EXIT_FAILED))
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
