"""Split audio/video into parts that never exceed a size limit, without re-encoding.

The old "trim-audio" script this replaces had two bugs this module must not repeat:
given a file only slightly larger than the target it cut the end off instead of
splitting (losing content), and its parts routinely overshot the requested size by up
to 10%. Both failure modes are guarded against explicitly below.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from media_tools.core.ffmpeg import FFMPEG, probe, run_ffmpeg
from media_tools.core.media_formats import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from media_tools.core.paths import fsync_replace, temp_path
from media_tools.core.runner import Context, Item, Outcome
from media_tools.core.sizes import parse_size

MARGIN_RATIO = 0.02
MAX_ATTEMPTS = 3
MIN_PART_SECONDS = 0.05

# ffmpeg picks a muxer from the OUTPUT filename's extension, but a part is written to a
# `.partial` temp name first (so a crash leaves it for the runner's shared cleanup to
# find), which hides the real extension from that auto-detection. Each part is muxed
# with `-c copy` in whatever container the source uses, so the muxer must be named
# explicitly instead — this table is ffmpeg's own extension-to-muxer mapping, read off
# the bundled binary for every extension `MediaSplitEngine.inputs` accepts.
_MUXER_FOR_SUFFIX = {
    ".mp4": "mp4",
    ".mov": "mov",
    ".mkv": "matroska",
    ".webm": "webm",
    ".avi": "avi",
    ".m4v": "ipod",
    ".flv": "flv",
    ".wmv": "asf",
    ".ts": "mpegts",
    ".mpg": "mpeg",
    ".mpeg": "mpeg",
    ".mp3": "mp3",
    ".m4a": "ipod",
    ".aac": "adts",
    ".opus": "opus",
    ".ogg": "ogg",
    ".wav": "wav",
    ".flac": "flac",
}


class MediaSplitEngine:
    name = "media"
    inputs = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
    outputs = frozenset({"parts"})
    dependencies = (FFMPEG,)

    def add_arguments(self, group) -> None:
        group.add_argument(
            "--max-size",
            required=True,
            dest="max_size",
            help="Maximum size per part: 25MB (decimal) or 25MiB (binary).",
        )

    def hash_options(self, args) -> dict:
        return {"max_size": args.max_size}

    def output_names(self, src: Path, args) -> list[str]:
        # The runner keys its skip/collision/re-run logic off this name, so it must be
        # the name `process` actually writes: a file already at or under the limit is
        # placed unchanged (its own name), not renamed to a "part01" that never exists.
        limit = parse_size(args.max_size)
        if src.stat().st_size <= limit:
            return [src.name]
        return [f"{src.stem}.part01{src.suffix}"]

    def process(self, item: Item, ctx: Context) -> Outcome:
        limit = ctx.deps["max_bytes"]
        ffmpeg = ctx.deps["ffmpeg"]
        source = item.source
        batch_dir = item.outputs[0].parent
        size_in = source.stat().st_size

        # Probe before trusting the byte size: a corrupt or unreadable file must fail
        # its own item even when it happens to be small enough to look "under the
        # limit" — placing it unchanged without validating it would silently pass
        # garbage through, which is worse than the old script's own bugs.
        total = probe(source).duration_s
        if not total:
            return Outcome(
                status="failed",
                outputs=[],
                bytes_out=None,
                reason="engine_error",
                data={"error": "could not read duration"},
            )

        if size_in <= limit:
            return self._place_unchanged(source, batch_dir)

        return self._split(source, batch_dir, total, limit, ffmpeg, item, ctx)

    @staticmethod
    def _place_unchanged(source: Path, batch_dir: Path) -> Outcome:
        placed = batch_dir / source.name
        how = "link"
        try:
            os.link(source, placed)
        except OSError:
            how = "copy"
            # os.link raises EXDEV whenever -o points at a different filesystem (an
            # external drive, a network share — exactly where a large split's output
            # is likely to go), making this the realistic path, not a rare fallback.
            # Unlike the hard link, a copy is not atomic: stage it through the same
            # `.partial` temp name and `fsync_replace()` every other write in this
            # codebase uses, so an interruption mid-copy never leaves a truncated file
            # sitting at the real output name (which a re-run would then see as
            # "already done" and skip forever, silently passing corruption through).
            temp = temp_path(placed)
            try:
                shutil.copyfile(source, temp)
            except Exception:
                temp.unlink(missing_ok=True)
                raise
            fsync_replace(temp, placed)
        return Outcome(
            status="done",
            outputs=[placed],
            bytes_out=placed.stat().st_size,
            reason="under_limit",
            data={"parts": 1, "placed": how},
        )

    def _split(
        self,
        source: Path,
        batch_dir: Path,
        total: float,
        limit: int,
        ffmpeg: str,
        item: Item,
        ctx: Context,
    ) -> Outcome:
        parts: list[Path] = []
        durations: list[float] = []
        start = 0.0
        index = 0
        muxer = _MUXER_FOR_SUFFIX[source.suffix.lower()]

        while start < total - MIN_PART_SECONDS:
            index += 1
            temp = batch_dir / f".{source.stem}.part{index:02d}{source.suffix}.partial"
            budget = int(limit * (1 - MARGIN_RATIO))
            duration = 0.0

            for _attempt in range(MAX_ATTEMPTS):
                temp.unlink(missing_ok=True)
                code, stderr = run_ffmpeg(
                    [
                        ffmpeg,
                        "-y",
                        "-ss",
                        f"{start:.3f}",
                        "-i",
                        str(source),
                        "-map",
                        "0",
                        "-c",
                        "copy",
                        "-fs",
                        str(budget),
                        "-avoid_negative_ts",
                        "make_zero",
                        "-f",
                        muxer,
                        str(temp),
                    ]
                )
                if code != 0 or not temp.exists():
                    self._discard(temp, parts)
                    return Outcome(
                        status="failed",
                        outputs=[],
                        bytes_out=None,
                        reason="engine_error",
                        data={"stderr": stderr},
                    )
                if temp.stat().st_size <= limit:
                    duration = probe(temp).duration_s or 0.0
                    break
                budget = int(budget * 0.95)
            else:
                self._discard(temp, parts)
                return Outcome(
                    status="failed", outputs=[], bytes_out=None, reason="size_limit_unreachable"
                )

            if duration <= MIN_PART_SECONDS:
                self._discard(temp, parts)
                return Outcome(
                    status="failed",
                    outputs=[],
                    bytes_out=None,
                    reason="keyframe_interval_exceeds_max_size",
                )

            parts.append(temp)
            durations.append(duration)
            # Advance conservatively rather than by the full measured duration: a part
            # cut with `-c copy` can leave one stream (e.g. video) shorter than another
            # (e.g. audio) within the same file, and the only duration this codebase can
            # read back (`probe`, container-level) does not distinguish per-stream
            # numbers — ffprobe isn't bundled. Starting the next part slightly earlier
            # guarantees overlap instead of ever skipping content.
            advance = duration * (1 - MARGIN_RATIO)
            start += advance
            ctx.reporter.progress(
                stage="split",
                index=item.id,
                count=ctx.total_items,
                path=source.name,
                percent=min(100.0, start / total * 100),
            )

        width = max(2, len(str(len(parts))))
        finals: list[Path] = []
        for number, temp in enumerate(parts, start=1):
            final = batch_dir / f"{source.stem}.part{number:0{width}d}{source.suffix}"
            fsync_replace(temp, final)
            finals.append(final)

        covered = sum(durations)
        return Outcome(
            status="done",
            outputs=finals,
            bytes_out=sum(p.stat().st_size for p in finals),
            data={
                "parts": len(finals),
                "source_duration_s": round(total, 2),
                "parts_duration_s": round(covered, 2),
                "overlap_s": round(max(0.0, covered - total), 2),
            },
        )

    @staticmethod
    def _discard(temp: Path, parts: list[Path]) -> None:
        temp.unlink(missing_ok=True)
        for part in parts:
            part.unlink(missing_ok=True)
