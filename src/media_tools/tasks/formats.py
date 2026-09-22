"""formats: list what each task and engine can read and write.

This answers "what can this thing read and write" straight from the code — the
`ENGINES` every file task declares (see `tasks/compress/`, the worked example) — so the
README and CLAUDE.md can embed a table that a test keeps honest instead of hand-written
prose that quietly drifts out of date.
"""

from __future__ import annotations

import json

from media_tools.core.events import EXIT_OK
from media_tools.tasks import compress, convert, download, ebook, split

NAME = "formats"
HELP = "List supported input/output formats and their requirements."

# The task modules that process media and may declare `ENGINES`. `formats`, `status`
# and `doctor` are commands *about* the tool, not media tasks, so they are not listed
# here even though they live in the same `tasks` package.
TASK_MODULES = [compress, convert, split, download, ebook]

_HEADER = ("Task", "Engine", "Input formats", "Output formats", "Requires")


def register(subparsers):
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP)
    parser.add_argument("--json", action="store_true", dest="json_mode")
    parser.add_argument("--markdown", action="store_true", help="Render a Markdown table.")
    parser.add_argument("-q", "--quiet", action="store_true", help="Omit the table header.")
    return parser


def collect() -> list[dict]:
    """One row per (task, engine). A task with no `ENGINES` (R5: `download` and `ebook`
    legitimately have none — a plain attribute access would crash this command) still
    gets a single row with `engine: None`, so it is not silently missing from the
    table; its formats are simply not declared through the `Engine` protocol."""
    rows: list[dict] = []
    for module in TASK_MODULES:
        engines = getattr(module, "ENGINES", [])
        if not engines:
            rows.append(
                {"task": module.NAME, "engine": None, "inputs": [], "outputs": [], "requires": []}
            )
            continue
        for engine in engines:
            rows.append(
                {
                    "task": module.NAME,
                    "engine": engine.name,
                    "inputs": sorted(engine.inputs),
                    "outputs": sorted(engine.outputs),
                    "requires": [dep.name for dep in engine.dependencies],
                }
            )
    return rows


def as_markdown(rows: list[dict]) -> str:
    lines = [
        "| " + " | ".join(_HEADER) + " |",
        "| " + " | ".join("---" for _ in _HEADER) + " |",
    ]
    for row in rows:
        cells = (
            row["task"],
            row["engine"] or "-",
            ", ".join(row["inputs"]) or "-",
            ", ".join(row["outputs"]) or "-",
            ", ".join(row["requires"]) or "-",
        )
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _as_plain_table(rows: list[dict], *, header: bool) -> str:
    body = [
        (
            row["task"],
            row["engine"] or "-",
            ",".join(row["inputs"]) or "-",
            ",".join(row["outputs"]) or "-",
            ",".join(row["requires"]) or "-",
        )
        for row in rows
    ]
    all_rows = ([_HEADER] if header else []) + body
    widths = [max(len(r[i]) for r in all_rows) for i in range(len(_HEADER))] if all_rows else []
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(r, widths, strict=True)) for r in all_rows]
    return "\n".join(lines) + ("\n" if lines else "")


def run(args) -> int:
    rows = collect()
    if args.json_mode:
        envelope = {"v": 1, "type": "formats", "formats": rows}
        print(json.dumps(envelope, ensure_ascii=False))
    elif args.markdown:
        print(as_markdown(rows), end="")
    else:
        print(_as_plain_table(rows, header=not args.quiet), end="")
    return EXIT_OK
