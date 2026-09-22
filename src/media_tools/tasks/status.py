"""status: show a batch's progress, or every batch's, without anyone parsing
`run.json` by hand.

This is the command an agent runs after an interrupted or partial run to find out
what is missing: which items failed and why, and which are still pending — not just
totals (see `_single_batch_payload`).
"""

from __future__ import annotations

import json
from pathlib import Path

from media_tools.core.events import EXIT_OK
from media_tools.core.paths import RESERVED_ROOT_ENTRIES, output_root
from media_tools.tasks.common import UsageError

NAME = "status"
HELP = "Show progress for one batch or all batches."


def register(subparsers):
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP)
    parser.add_argument(
        "batch", nargs="?", default=None, help="Batch name to show (default: list all)."
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="Output root to look under (default: MEDIA_TOOLS_OUT, the repo's media/, or ./media).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Also list reserved entries under the root (.cache, _kindle) as batches.",
    )
    parser.add_argument("--json", action="store_true", dest="json_mode")
    parser.add_argument("-q", "--quiet", action="store_true", help="Summary line only.")
    return parser


def _read_one(batch_dir: Path) -> dict:
    run_file = batch_dir / "run.json"
    try:
        data = json.loads(run_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return {"batch": batch_dir.name, "readable": False, "error": str(error)}
    data["readable"] = True
    return data


def read_batches(root: Path, *, include_reserved: bool = False) -> list[dict]:
    """One dict per subfolder of `root`: the parsed `run.json` (plus `readable: True`),
    or `{"batch", "readable": False, "error"}` when it is missing or corrupt — a
    batch's own bad state must never take the whole listing down with it."""
    root = Path(root)
    if not root.is_dir():
        return []
    batches = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir():
            continue
        if not include_reserved and entry.name in RESERVED_ROOT_ENTRIES:
            continue
        batches.append(_read_one(entry))
    return batches


def _summary_row(data: dict) -> dict:
    if not data.get("readable", True):
        return {"batch": data["batch"], "readable": False, "error": data.get("error")}
    return {
        "batch": data.get("batch"),
        "task": data.get("task"),
        "status": data.get("status"),
        "updated_at": data.get("updated_at"),
        "active": data.get("owner") is not None,
        "counts": data.get("counts", {}),
        "readable": True,
    }


def _single_batch_payload(data: dict) -> dict:
    if not data.get("readable", True):
        return {"batch": data["batch"], "readable": False, "error": data.get("error")}
    items = data.get("items", [])
    failed = [
        {"id": item["id"], "input": item["input"], "reason": item.get("reason")}
        for item in items
        if item.get("status") == "failed"
    ]
    pending = [
        {"id": item["id"], "input": item["input"]}
        for item in items
        if item.get("status") == "pending"
    ]
    return {
        "batch": data.get("batch"),
        "task": data.get("task"),
        "status": data.get("status"),
        "updated_at": data.get("updated_at"),
        "active": data.get("owner") is not None,
        "counts": data.get("counts", {}),
        "failed": failed,
        "pending": pending,
        "readable": True,
    }


def _print_single_human(payload: dict, *, quiet: bool) -> None:
    if not payload.get("readable", True):
        print(f"{payload['batch']}: unreadable ({payload.get('error')})")
        return
    active = "active" if payload["active"] else "not active"
    print(f"{payload['batch']} ({payload['task']}) — {payload['status']} — {active}")
    print(f"  updated: {payload['updated_at']}")
    counts = payload["counts"]
    print("  counts: " + " ".join(f"{k}={v}" for k, v in counts.items()))
    if quiet:
        return
    if payload["failed"]:
        print("  failed:")
        for item in payload["failed"]:
            print(f"    #{item['id']} {item['input']} — {item['reason']}")
    if payload["pending"]:
        print("  pending:")
        for item in payload["pending"]:
            print(f"    #{item['id']} {item['input']}")


def _print_list_human(rows: list[dict], *, quiet: bool) -> None:
    if not rows:
        print("no batches found")
        return
    for row in rows:
        if not row.get("readable", True):
            print(f"{row['batch']}: unreadable ({row.get('error')})")
            continue
        active = " (active)" if row["active"] else ""
        line = f"{row['batch']} [{row['task']}] {row['status']}{active}"
        if not quiet:
            counts = row["counts"]
            line += " — " + " ".join(f"{k}={v}" for k, v in counts.items())
        print(line)


def run(args) -> int:
    root = output_root(args.output_dir)

    if args.batch:
        batch_dir = root / args.batch
        if not batch_dir.is_dir():
            raise UsageError(
                f"unknown batch {args.batch!r} under {root}",
                hint="run `media-tools status -o ...` with no batch name to list them",
            )
        payload = _single_batch_payload(_read_one(batch_dir))
        if args.json_mode:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            _print_single_human(payload, quiet=args.quiet)
        return EXIT_OK

    rows = [_summary_row(d) for d in read_batches(root, include_reserved=args.all)]
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    if args.json_mode:
        print(json.dumps(rows, ensure_ascii=False))
    else:
        _print_list_human(rows, quiet=args.quiet)
    return EXIT_OK
