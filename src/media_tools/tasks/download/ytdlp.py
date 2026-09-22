"""Downloading with yt-dlp: list files, output names and one download.

Replaces the old YouTube-downloader script, whose bugs this module must not repeat:
it always exited 0 even when every download failed; it printed full URLs (including
signed tokens in query strings) to the terminal and into error messages; it could not
merge separate video/audio streams because no system ffmpeg was installed; its audio
mode returned a 360p video because of a stale `player_client` extractor argument; and
without `--best` it dropped into an interactive picker that crashed on a closed stdin.

Every fix lives here: `ffmpeg_location` always points at the bundled ffmpeg (`core.
ffmpeg.ffmpeg_exe`), no `player_client` override is ever passed, a `logger` routes every
yt-dlp message through `redact_text` before it reaches a human or an exception message,
and format selection never falls back to interactive input.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from media_tools.core.events import Reporter
from media_tools.core.paths import BatchNameError, sanitize_batch
from media_tools.core.redact import redact_text, redact_url
from media_tools.core.runner import Outcome
from media_tools.tasks.common import UsageError

UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class Entry:
    url: str
    name: str | None


def load_list(path: Path) -> list[Entry]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise UsageError(f"list file not found: {path}") from error
    except json.JSONDecodeError as error:
        raise UsageError(f"list file is not valid JSON: {error}") from error

    if isinstance(data, dict):
        data = data.get("urls")
    if not isinstance(data, list):
        raise UsageError(
            'list file must be ["url", …], [{"url": …, "name": …}, …] or {"urls": [ … ]}'
        )

    entries: list[Entry] = []
    for raw in data:
        if isinstance(raw, str):
            entries.append(Entry(raw.strip(), None))
        elif isinstance(raw, dict) and isinstance(raw.get("url"), str):
            entries.append(Entry(raw["url"].strip(), raw.get("name")))
        else:
            raise UsageError(f"invalid entry in list file: {raw!r}")
    if not entries:
        raise UsageError(f"list file has no URLs: {path}")
    return entries


def _is_generic_title(title: str | None, url: str) -> bool:
    if not title:
        return True
    last = urlsplit(url).path.rstrip("/").split("/")[-1].lower()
    return title.lower() in {last, last.rsplit(".", 1)[0] if "." in last else last}


def derive_name(url: str, info: dict | None, explicit: str | None) -> str:
    if explicit:
        return explicit
    title = (info or {}).get("title")
    if not _is_generic_title(title, url):
        return title
    found = UUID_PATTERN.search(urlsplit(url).path)
    if found:
        return found.group(0)
    return hashlib.sha1(redact_url(url).encode("utf-8")).hexdigest()[:12]


def _safe_filename(name: str) -> str:
    """A name safe to write to disk. `derive_name` itself must stay unsanitised (its
    result is asserted verbatim in tests) — filesystem safety is applied here, at the
    one place the name actually reaches a path."""
    try:
        return sanitize_batch(name)
    except BatchNameError:
        return hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


class _YdlLogger:
    """Routes every yt-dlp debug/info/warning/error message through the reporter's
    human stream, redacted. Only public `Reporter` attributes are used here so this
    never depends on the reporter's own internal formatting helpers."""

    def __init__(self, reporter: Reporter) -> None:
        self._reporter = reporter

    def _emit(self, message: object) -> None:
        reporter = self._reporter
        if reporter.json_mode or reporter.quiet:
            return
        reporter.stderr.write(redact_text(str(message)) + "\n")
        reporter.stderr.flush()

    def debug(self, msg: object) -> None:
        self._emit(msg)

    def info(self, msg: object) -> None:
        self._emit(msg)

    def warning(self, msg: object) -> None:
        self._emit(msg)

    def error(self, msg: object) -> None:
        self._emit(msg)


def _select_format(type_: str, quality: str, format_id: str | None) -> tuple[str, list[str] | None]:
    """The format selector and (ascending) sort fields for one download.

    An explicit `--format` id always wins. Otherwise: audio prefers an audio-only
    stream, falling back to the best combined stream (`ba/b`); video prefers separate
    streams merged by ffmpeg, falling back to a combined one (`bv*+ba/b`). `--best`
    uses yt-dlp's own (descending) preference; the default asks yt-dlp to sort
    ascending by size/bitrate/resolution instead, so the same "best" selector lands on
    the SMALLEST available format rather than the discouraged `worst*` selectors.
    """
    if format_id:
        return format_id, None
    base = "ba/b" if type_ == "audio" else "bv*+ba/b"
    if quality == "best":
        return base, None
    return base, ["+size", "+br", "+res"]


def _has_audio_only_format(info: dict) -> bool:
    return any(
        f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
        for f in info.get("formats") or []
    )


def _progress_hook(reporter: Reporter, *, name: str, index: int, count: int):
    def _hook(data: dict) -> None:
        if data.get("status") != "downloading":
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        downloaded = data.get("downloaded_bytes") or 0
        percent = (downloaded / total * 100) if total else 0.0
        reporter.progress(
            stage="download",
            index=index,
            count=count,
            path=name,
            percent=percent,
            eta_s=data.get("eta"),
        )

    return _hook


def download_one(
    entry: Entry,
    *,
    output_dir: Path,
    type_: str,
    quality: str,
    format_id: str | None,
    ffmpeg: str,
    reporter: Reporter,
    force: bool = False,
    index: int = 1,
    count: int = 1,
) -> Outcome:
    """Download one URL. Never raises: any yt-dlp failure becomes a failed Outcome so
    the batch can continue with the next entry."""
    from yt_dlp import YoutubeDL

    logger = _YdlLogger(reporter)
    common_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "logger": logger,
        "noprogress": True,
        "ffmpeg_location": ffmpeg,
        # Deliberately no `extractor_args`/`player_client` override: the stale
        # ['android', 'web'] client list the old script passed is what made `-t audio`
        # come back as a 360p video. yt-dlp's own defaults are correct here.
    }

    try:
        with YoutubeDL(dict(common_opts)) as probe:
            info = probe.extract_info(entry.url, download=False)
    except Exception as error:
        return Outcome(
            status="failed",
            outputs=[],
            bytes_out=None,
            reason="engine_error",
            data={"error_type": type(error).__name__, "error_message": redact_text(str(error))},
        )

    if info is None:
        return Outcome(
            status="failed",
            outputs=[],
            bytes_out=None,
            reason="engine_error",
            data={"error": "no information extracted"},
        )

    name = _safe_filename(derive_name(entry.url, info, entry.name))
    output_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(output_dir / f"{name}.%(ext)s")

    format_selector, format_sort = _select_format(type_, quality, format_id)

    warnings: list[str] = []
    if type_ == "audio" and not _has_audio_only_format(info):
        warnings.append("no_audio_only_format")

    download_opts = {
        **common_opts,
        "format": format_selector,
        "outtmpl": outtmpl,
        "overwrites": True if force else None,
        "progress_hooks": [_progress_hook(reporter, name=name, index=index, count=count)],
    }
    if format_sort:
        download_opts["format_sort"] = format_sort
    if type_ == "video":
        download_opts["merge_output_format"] = "mp4"

    try:
        with YoutubeDL(download_opts) as ydl:
            result_info = ydl.extract_info(entry.url, download=True)
    except Exception as error:
        return Outcome(
            status="failed",
            outputs=[],
            bytes_out=None,
            reason="engine_error",
            data={"error_type": type(error).__name__, "error_message": redact_text(str(error))},
        )

    final_path = _resolve_output_path(ydl, result_info, outtmpl)
    bytes_out = final_path.stat().st_size if final_path and final_path.exists() else None
    return Outcome(
        status="done",
        outputs=[final_path] if final_path else [],
        bytes_out=bytes_out,
        warnings=warnings or None,
    )


def _resolve_output_path(ydl, result_info: dict, outtmpl: str) -> Path | None:
    downloads = result_info.get("requested_downloads") or []
    if downloads and downloads[0].get("filepath"):
        return Path(downloads[0]["filepath"])
    try:
        return Path(ydl.prepare_filename(result_info))
    except Exception:
        return None


def list_formats(url: str, *, ffmpeg: str, reporter: Reporter) -> list[dict]:
    """The formats available for `url`, simplified for display. Deliberately excludes
    each format's raw `url` field: on many sites that is a signed CDN link carrying the
    same kind of token this whole task exists to keep out of logs, and nothing about
    "which format to pick" needs it. Raises on extraction failure; the caller decides
    how to report that per-URL."""
    from yt_dlp import YoutubeDL

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "logger": _YdlLogger(reporter),
        "ffmpeg_location": ffmpeg,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = (info or {}).get("formats") or []
    listed = []
    for f in formats:
        resolution = f.get("resolution")
        if not resolution and f.get("width") and f.get("height"):
            resolution = f"{f['width']}x{f['height']}"
        listed.append(
            {
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": resolution,
                "fps": f.get("fps"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
                "note": f.get("format_note"),
            }
        )
    return listed
