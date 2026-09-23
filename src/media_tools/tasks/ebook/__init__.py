"""ebook: build a language-sorted ebook library, all at once or stage by stage.

`build` wires every stage — scan, metadata, normalize, dedup, covers, convert,
verify, organize — into one command (see `tasks.ebook.build`). The other
subcommands (`scan`, `normalize`, `dedup`, `covers`, `convert`) share the exact
same pipeline and simply stop after their own named stage.
"""

from __future__ import annotations

from media_tools.tasks.ebook import build
from media_tools.tasks.ebook.kindle import cli as kindle

NAME = "ebook"
HELP = "Build an ebook library, all at once (`build`) or stage by stage."


def register(subparsers):
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP)
    # `build.register_subparsers` gives `parser` its one subparsers action (argparse
    # allows only one per parser) with dest="ebook_command", covering its six
    # stage-based subcommands (`SUBCOMMANDS`), AND RETURNS that action for exactly this
    # reason: `kindle` is not a stage in that pipeline — it gets its own nested dispatch
    # instead (dest="kindle_command": status/scan/backup/thumbnails/add/remove/sync/
    # restore/eject, wired by `kindle.register_subparsers` below) rather than a slot
    # in `build.SUBCOMMANDS` —
    # so it is added to that SAME action (`.add_parser()`, fully public API) rather
    # than a second one, which argparse would refuse outright ("cannot have multiple
    # subparser arguments"). `run()` below is `kindle`'s own dispatch branch: it never
    # reaches `build.run`, which would otherwise KeyError on `SUBCOMMANDS["kindle"]`.
    subparsers_action = build.register_subparsers(parser)
    kindle_parser = subparsers_action.add_parser(
        kindle.NAME, help=kindle.HELP, description=kindle.HELP
    )
    kindle.register_subparsers(kindle_parser)
    return parser


def run(args) -> int:
    if args.ebook_command == kindle.NAME:
        return kindle.run(args)
    return build.run(args)
