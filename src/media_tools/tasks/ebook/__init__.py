"""ebook: build a language-sorted ebook library, all at once or stage by stage.

`build` wires every stage — scan, metadata, normalize, dedup, covers, convert,
verify, organize — into one command (see `tasks.ebook.build`). The other
subcommands (`scan`, `normalize`, `dedup`, `covers`, `convert`) share the exact
same pipeline and simply stop after their own named stage.
"""

from __future__ import annotations

from media_tools.tasks.ebook import build

NAME = "ebook"
HELP = "Build an ebook library, all at once (`build`) or stage by stage."


def register(subparsers):
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP)
    build.register_subparsers(parser)
    return parser


def run(args) -> int:
    return build.run(args)
