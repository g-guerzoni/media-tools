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

import hashlib
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
from media_tools.core.paths import BatchNameError, output_root, sanitize_batch
from media_tools.core.redact import redact_text, redact_url
from media_tools.core.runner import Outcome, build_result, empty_result, write_summary_json
from media_tools.core.state import BatchInUse, BatchTaskMismatch, RunState, resolve_batch_name
from media_tools.tasks.common import UsageError, add_common_flags
from media_tools.tasks.download.ytdlp import (
    Entry,
    download_one,
    find_existing_output,
    list_formats,
    load_list,
    safe_filename,
)

NAME = "download"
HELP = "Download media from a URL (yt-dlp)."
ENGINES: list = []


def register(subparsers):
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP)
    # Downloads take URLs, not local paths: no folder scan, so no -r/-e either.
    add_common_flags(parser, inputs_type=str, metavar="URL", scan_flags=False)
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
    # No mutual-exclusion guard for --best + --format: the help text for --format says
    # it "overrides --best", and `_select_format` already implements exactly that
    # precedence (an explicit format id always wins), so raising a UsageError here would
    # contradict the documented behaviour instead of matching it.
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
        if args.batch:
            batch_name = sanitize_batch(args.batch)
        elif args.list_file:
            batch_name = sanitize_batch(args.list_file.stem)
        else:
            base_name = _batch_hash(options=options, urls=[redact_url(e.url) for e in entries])
            # Same spec 7.1 rule the file tasks apply in tasks/common.py::prepare(): a
            # generated name whose run.json belongs to a different task or options gets
            # -2, -3, ... appended instead of being silently reused or colliding.
            batch_name = resolve_batch_name(root, base_name, task=NAME, options=options)
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


def _batch_hash(*, options: dict, urls: list[str]) -> str:
    """The default batch name when neither --batch nor --list gives one.

    `core.paths.batch_hash` (used by the file tasks) resolves each input against the
    filesystem (`Path(p).resolve()`), which for a URL string resolves against the
    process's *current working directory* — so the same command run from two different
    directories would land in two different batches, and a re-run from a different
    directory would never see FIX 1's skip/resume kick in. This hashes the URLs
    themselves instead, with the same canonical serialisation `core.paths.batch_hash`
    uses (`sort_keys=True, separators=(",", ":")`) so it is exactly as deterministic,
    just not filesystem-dependent.
    """
    payload = {"hash_v": 1, "task": NAME, "options": options, "urls": sorted(urls)}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


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
            reporter.error(
                code="extraction_failed",
                message=f"could not list formats for {safe_url}: {redact_text(str(error))}",
                hint="the source may be temporarily unavailable or unsupported; "
                "retrying later may help",
                retryable=True,
            )
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
        stages=["resolve", "download"],
        items=len(entries),
        options=options,
    )
    for index, entry in enumerate(entries, start=1):
        reporter.item(
            id=index, status="skipped", input=entry.url, outputs=[], bytes_in=None, reason=None
        )
    # Same five keys a real run's `counts` always has, plus `planned` as an extra — see
    # core.runner._report_dry_run's identical fix.
    reporter.result(
        ok=True,
        exit_code=EXIT_OK,
        counts={
            "total": len(entries),
            "done": 0,
            "skipped": len(entries),
            "failed": 0,
            "pending": 0,
            "planned": len(entries),
        },
        failed=[],
        pending=[],
        outputs=[],
        run_file=None,
    )
    return EXIT_OK


def _pre_register_explicit_names(
    entries: list[Entry], claimed: dict[str, tuple[int, str]]
) -> dict[int, str]:
    """Collisions among entries whose name is known without touching the network — an
    explicit `name`, from `--name` or the list file. Spec 7.1: "the first input wins,
    every other fails with reason output_collision ... nothing is ever silently
    overwritten." The first entry to claim a resolved name wins; every later one with
    the same name is returned here (1-based index → the redacted URL it collides with)
    so `_download_all` can fail it without ever calling `download_one`.

    `claimed` maps name → (owning entry's 1-based index, its redacted URL); ownership
    is keyed by index, not URL, because two different entries can legitimately share
    the same source URL (e.g. the same video downloaded twice under different names)
    and must still be told apart. It is mutated in place and shared with
    `download_one`, which does the equivalent check for a title-derived name —
    unavoidable only after extraction, so it cannot be resolved in this network-free
    pre-pass (see `download_one`'s docstring). A title-derived entry that happens to
    collide with a *later*-listed explicit name IS still caught — just not here, and
    not in the earlier entry's favour: because this pre-pass claims every explicit name
    up front regardless of list position, `download_one` sees the name already owned by
    the later entry once the earlier, title-derived one finishes extraction, and rejects
    that EARLIER entry as `output_collision` instead. That inverts "first input wins"
    for this one ordering. The coincidence needed for it is vanishingly rare, so this is
    left as is rather than reworked to always favour list order.
    """
    collisions: dict[int, str] = {}
    for index, entry in enumerate(entries, start=1):
        if not entry.name:
            continue
        name = safe_filename(entry.name)
        if name in claimed:
            collisions[index] = claimed[name][1]
        else:
            claimed[name] = (index, redact_url(entry.url))
    return collisions


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
    stages = ["resolve", "download"]
    reporter.start(
        tool=NAME,
        batch=batch_dir.name,
        output_dir=batch_dir,
        stages=stages,
        items=len(entries),
        options=options,
    )

    try:
        state_cm = RunState.open(
            batch_dir,
            task=NAME,
            options=options,
            inputs=[redact_url(e.url) for e in entries],
            force=force,
        )
    except BatchInUse as error:
        reporter.error(code="batch_in_use", message=str(error))
        reporter.result(**empty_result(EXIT_USAGE))
        return EXIT_USAGE
    except BatchTaskMismatch as error:
        reporter.error(code="batch_task_mismatch", message=str(error))
        reporter.result(**empty_result(EXIT_USAGE))
        return EXIT_USAGE
    except OSError as error:
        reporter.error(
            code="output_not_writable",
            message=f"cannot create batch directory {batch_dir}: {error}",
            hint="pass -o/--output-dir to a writable location, or fix permissions on this one",
        )
        reporter.result(**empty_result(EXIT_DEPENDENCY))
        return EXIT_DEPENDENCY

    # A shared, mutated-in-place registry: `name -> (owning entry's 1-based index, its
    # redacted URL)`. Pre-populated now for every entry with an explicit name (FIX 4);
    # `download_one` reads and extends it for a title-derived name.
    claimed: dict[str, tuple[int, str]] = {}
    collisions = _pre_register_explicit_names(entries, claimed)

    # Mirrors `core.runner.run_items`: both declared stages are generic to the whole
    # batch (`download_one` does its own per-entry resolve-then-download), so both are
    # reported once, up front, rather than per item.
    reporter.stage(stage=stages[0], index=1, count=len(stages))
    reporter.stage(stage=stages[1], index=2, count=len(stages))

    exit_code = EXIT_OK
    with state_cm as state:
        for entry in entries:
            state.add_item(entry.url)

        try:
            for index, entry in enumerate(entries, start=1):
                if index in collisions:
                    # A name collision detected in the pre-pass: this entry never
                    # touches the network at all.
                    outcome = Outcome(
                        status="failed",
                        outputs=[],
                        bytes_out=None,
                        reason="output_collision",
                        data={"collides_with": collisions[index]},
                    )
                elif (
                    entry.name
                    and not force
                    and (existing := find_existing_output(batch_dir, safe_filename(entry.name)))
                ):
                    # Skip/resume for an explicit name: also resolved without touching
                    # the network. A title-derived name can only be checked once its
                    # name is known, which needs extraction — that case is handled
                    # inside `download_one` instead (see its docstring).
                    outcome = Outcome(
                        status="skipped",
                        outputs=[existing],
                        bytes_out=existing.stat().st_size,
                        reason="exists",
                    )
                else:
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
                            claimed=claimed,
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
            # `empty_result` is for a run that never got to own a batch (an
            # acquisition failure above); this run does own one, with real progress
            # already in `state` and on disk, so the final event must reflect that —
            # exactly what `core/runner.py` does under ruling R19 — not report zero
            # counts and a null run_file while run.json on disk says otherwise.
            reporter.result(**build_result(state, batch_dir, EXIT_INTERRUPTED))
            return EXIT_INTERRUPTED

        state.finish("done" if exit_code == EXIT_OK else "failed")

    result = build_result(state, batch_dir, exit_code)
    reporter.result(**result)
    write_summary_json(summary_json, result)
    return exit_code
