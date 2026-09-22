"""download: fetch media from a URL with yt-dlp.

Unlike the file tasks (`compress`, `convert`, `split`), a download's inputs are URLs,
not local paths, so this module does not go through `tasks.common.prepare`/
`expand_inputs` (those are file-oriented and would mangle a URL into something like
"input not found: https:/example.com/…") or `core.runner.run_items` (which plans
engines off a file's suffix and stats a local source path). Instead it resolves its own
batch name and drives its own per-item loop, calling `download_one` for each URL —
while still emitting the same `start`/`item`/`result` events and writing the same
`run.json` shape every other task does, so an agent driving this tool sees one
consistent contract regardless of task.
"""

from __future__ import annotations

import json
from pathlib import Path

from media_tools.core.events import (
    EXIT_DEPENDENCY,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    Reporter,
)
from media_tools.core.ffmpeg import ffmpeg_exe
from media_tools.core.paths import BatchNameError, batch_hash, output_root, sanitize_batch
from media_tools.core.redact import redact_text, redact_url
from media_tools.core.runner import Outcome
from media_tools.core.state import BatchInUse, BatchTaskMismatch, RunState
from media_tools.tasks.common import UsageError, add_common_flags
from media_tools.tasks.download.ytdlp import Entry, download_one, list_formats, load_list

NAME = "download"
HELP = "Download media from a URL (yt-dlp)."
ENGINES: list = []


def register(subparsers):
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP)
    # Downloads take URLs, not local paths.
    add_common_flags(parser, inputs_type=str, metavar="URL")
    group = parser.add_argument_group("download options")
    group.add_argument(
        "--list",
        dest="list_file",
        type=Path,
        default=None,
        metavar="FILE",
        help="Read URLs from a JSON list file instead of positional URLs: "
        '["url", …], [{"url": …, "name": …}, …] or {"urls": […]}.',
    )
    group.add_argument(
        "--list-formats",
        dest="list_formats",
        action="store_true",
        help="Print the formats available for each URL to stdout and exit; nothing is downloaded.",
    )
    group.add_argument(
        "--type",
        choices=["video", "audio"],
        default="video",
        help="Media type to download (default: video).",
    )
    group.add_argument(
        "--best",
        action="store_true",
        help="Use yt-dlp's own best quality instead of the default (smallest available).",
    )
    group.add_argument(
        "--format",
        dest="format_id",
        default=None,
        metavar="ID",
        help="Explicit yt-dlp format selector, passed through as-is (overrides --best).",
    )
    group.add_argument(
        "--name",
        default=None,
        help="Explicit output name for a single URL (not valid with --list or several URLs).",
    )
    return parser


def run(args) -> int:
    if args.best and args.format_id:
        raise UsageError("--best and --format are mutually exclusive")
    if args.inputs and args.list_file:
        raise UsageError("pass URLs or --list, not both")
    if args.name and args.list_file:
        raise UsageError("--name cannot be combined with --list (names come from the list file)")
    if args.name and len(args.inputs) > 1:
        raise UsageError("--name only applies to a single URL")

    if args.list_file:
        entries = load_list(args.list_file)
    elif args.inputs:
        entries = [Entry(url, args.name) for url in args.inputs]
    else:
        raise UsageError(f"no input given; see `media-tools {NAME} --help`")

    if args.include:
        needle = args.include.lower()
        entries = [e for e in entries if needle in e.url.lower()]
    if args.exclude:
        needle = args.exclude.lower()
        entries = [e for e in entries if needle not in e.url.lower()]
    if args.limit is not None:
        entries = entries[: args.limit]
    if not entries:
        raise UsageError("no URL matched (check --list/--include/--exclude/--limit)")

    reporter = Reporter(json_mode=args.json_mode, quiet=args.quiet)
    ffmpeg = ffmpeg_exe()

    if args.list_formats:
        return _list_formats(entries, ffmpeg=ffmpeg, json_mode=args.json_mode, reporter=reporter)

    quality = "best" if args.best else "worst"
    options = {
        "type": args.type,
        "quality": "explicit" if args.format_id else quality,
        "format": args.format_id,
    }

    root = output_root(args.output_dir)
    try:
        batch_name = (
            sanitize_batch(args.batch)
            if args.batch
            else sanitize_batch(args.list_file.stem)
            if args.list_file
            else batch_hash(
                task=NAME,
                options=options,
                selection={},
                inputs=[redact_url(e.url) for e in entries],
            )
        )
    except BatchNameError as error:
        raise UsageError(str(error)) from error
    batch_dir = root / batch_name

    if args.dry_run:
        return _dry_run(entries, batch_dir=batch_dir, reporter=reporter, options=options)

    return _download_all(
        entries,
        batch_dir=batch_dir,
        reporter=reporter,
        ffmpeg=ffmpeg,
        type_=args.type,
        quality=quality,
        format_id=args.format_id,
        options=options,
        force=args.force,
        stop_on_error=args.stop_on_error,
        summary_json=args.summary_json,
    )


def _cell(value) -> str:
    return "-" if value in (None, "") else str(value)


def _list_formats(entries: list[Entry], *, ffmpeg: str, json_mode: bool, reporter: Reporter) -> int:
    """Print formats for each URL to STDOUT and exit; nothing is downloaded and no
    batch directory is ever created. Errors are per-URL: one bad URL is reported and
    the rest still print, but the process still exits 1 (spec 7 applies here too)."""
    exit_code = EXIT_OK
    for entry in entries:
        safe_url = redact_url(entry.url)
        try:
            formats = list_formats(entry.url, ffmpeg=ffmpeg, reporter=reporter)
        except Exception as error:
            exit_code = EXIT_FAILED
            message = redact_text(str(error))
            if json_mode:
                print(
                    json.dumps(
                        {
                            "v": 1,
                            "type": "error",
                            "code": "engine_error",
                            "url": safe_url,
                            "message": message,
                        },
                        ensure_ascii=False,
                    )
                )
            else:
                print(f"error: {safe_url}: {message}")
            continue

        if json_mode:
            print(
                json.dumps(
                    {"v": 1, "type": "formats", "url": safe_url, "formats": formats},
                    ensure_ascii=False,
                )
            )
        else:
            print(f"==> {safe_url}")
            print(
                f"{'ID':<12}{'EXT':<8}{'RESOLUTION':<14}{'FPS':<6}{'FILESIZE':<12}"
                f"{'VCODEC':<10}{'ACODEC':<10}NOTE"
            )
            for f in formats:
                print(
                    f"{_cell(f['format_id']):<12}{_cell(f['ext']):<8}{_cell(f['resolution']):<14}"
                    f"{_cell(f['fps']):<6}{_cell(f['filesize']):<12}{_cell(f['vcodec']):<10}"
                    f"{_cell(f['acodec']):<10}{_cell(f['note'])}"
                )
    return exit_code


def _dry_run(entries: list[Entry], *, batch_dir: Path, reporter: Reporter, options: dict) -> int:
    reporter.start(
        tool=NAME,
        batch=batch_dir.name,
        output_dir=batch_dir,
        stages=["download"],
        items=len(entries),
        options=options,
    )
    for index, entry in enumerate(entries, start=1):
        reporter.item(
            id=index, status="skipped", input=entry.url, outputs=[], bytes_in=None, reason=None
        )
    reporter.result(
        ok=True,
        exit_code=EXIT_OK,
        counts={"total": len(entries), "planned": len(entries)},
        failed=[],
        pending=[],
        outputs=[],
        run_file=None,
    )
    return EXIT_OK


def _empty_result(exit_code: int) -> dict:
    return {
        "ok": False,
        "exit_code": exit_code,
        "counts": {"total": 0, "done": 0, "skipped": 0, "failed": 0, "pending": 0},
        "failed": [],
        "pending": [],
        "outputs": [],
        "run_file": None,
    }


def _build_result(state: RunState, batch_dir: Path, exit_code: int) -> dict:
    return {
        "ok": exit_code == EXIT_OK,
        "exit_code": exit_code,
        "counts": state.counts(),
        "failed": [
            {"id": i["id"], "input": i["input"], "reason": i["reason"]}
            for i in state.data["items"]
            if i["status"] == "failed"
        ],
        "pending": [i["input"] for i in state.data["items"] if i["status"] == "pending"],
        "outputs": [str(batch_dir / o["path"]) for i in state.data["items"] for o in i["outputs"]],
        "run_file": state.path,
    }


def _download_all(
    entries: list[Entry],
    *,
    batch_dir: Path,
    reporter: Reporter,
    ffmpeg: str,
    type_: str,
    quality: str,
    format_id: str | None,
    options: dict,
    force: bool,
    stop_on_error: bool,
    summary_json: Path | None,
) -> int:
    reporter.start(
        tool=NAME,
        batch=batch_dir.name,
        output_dir=batch_dir,
        stages=["download"],
        items=len(entries),
        options=options,
    )

    try:
        state_cm = RunState.open(
            batch_dir, task=NAME, options=options, inputs=[redact_url(e.url) for e in entries]
        )
    except BatchInUse as error:
        reporter.error(code="batch_in_use", message=str(error))
        reporter.result(**_empty_result(EXIT_USAGE))
        return EXIT_USAGE
    except BatchTaskMismatch as error:
        reporter.error(code="batch_task_mismatch", message=str(error))
        reporter.result(**_empty_result(EXIT_USAGE))
        return EXIT_USAGE
    except OSError as error:
        reporter.error(
            code="output_not_writable",
            message=f"cannot create batch directory {batch_dir}: {error}",
            hint="pass -o/--output-dir to a writable location, or fix permissions on this one",
        )
        reporter.result(**_empty_result(EXIT_DEPENDENCY))
        return EXIT_DEPENDENCY

    exit_code = EXIT_OK
    with state_cm as state:
        for entry in entries:
            state.add_item(entry.url)

        try:
            for index, entry in enumerate(entries, start=1):
                try:
                    outcome = download_one(
                        entry,
                        output_dir=batch_dir,
                        type_=type_,
                        quality=quality,
                        format_id=format_id,
                        ffmpeg=ffmpeg,
                        reporter=reporter,
                        force=force,
                        index=index,
                        count=len(entries),
                    )
                except Exception as error:
                    # A single broken download must never take the rest of the batch
                    # down with it — `download_one` already guards this internally,
                    # this is the same belt-and-suspenders net `run_items` uses.
                    outcome = Outcome(
                        status="failed",
                        outputs=[],
                        bytes_out=None,
                        reason="engine_error",
                        data={
                            "error_type": type(error).__name__,
                            "error_message": redact_text(str(error)),
                        },
                    )

                if outcome.status == "failed":
                    exit_code = EXIT_FAILED

                # There is no local "before" size for a URL. 0 (not None) is used so
                # Reporter.item's "in→out" human summary — which calls format_size on
                # bytes_in whenever bytes_out is set — never crashes on a real download.
                state.update(
                    index,
                    status=outcome.status,
                    reason=outcome.reason,
                    bytes_in=0,
                    warnings=outcome.warnings or [],
                    data=outcome.data or {},
                    outputs=[
                        {
                            "path": str(Path(o).relative_to(batch_dir)),
                            "bytes": Path(o).stat().st_size,
                        }
                        for o in outcome.outputs
                        if Path(o).exists()
                    ],
                )
                reporter.item(
                    id=index,
                    status=outcome.status,
                    input=entry.url,
                    outputs=outcome.outputs,
                    bytes_in=0,
                    bytes_out=outcome.bytes_out,
                    reason=outcome.reason,
                    warnings=outcome.warnings,
                )
                if outcome.status == "failed" and stop_on_error:
                    break
        except KeyboardInterrupt:
            state.finish("interrupted")
            reporter.error(code="interrupted", message="interrupted by user")
            reporter.result(**_empty_result(EXIT_INTERRUPTED))
            return EXIT_INTERRUPTED

        state.finish("done" if exit_code == EXIT_OK else "failed")

    result = _build_result(state, batch_dir, exit_code)
    reporter.result(**result)
    if summary_json is not None:
        payload = {"v": 1, "type": "result", **result, "run_file": str(result["run_file"])}
        Path(summary_json).parent.mkdir(parents=True, exist_ok=True)
        Path(summary_json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return exit_code
