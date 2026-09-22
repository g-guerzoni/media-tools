"""Convert video or audio to MP3 with ffmpeg (libmp3lame)."""

from __future__ import annotations

from pathlib import Path

from media_tools.core.ffmpeg import FFMPEG, probe, run_ffmpeg
from media_tools.core.media_formats import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from media_tools.core.paths import fsync_replace, temp_path
from media_tools.core.runner import Context, Item, Outcome

QUALITY = {"low": "96k", "medium": "192k", "high": "320k"}


class AudioEngine:
    name = "audio"
    inputs = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
    outputs = frozenset({"mp3"})
    dependencies = (FFMPEG,)

    def add_arguments(self, group) -> None:
        group.add_argument(
            "--quality",
            default="high",
            choices=sorted(QUALITY),
            help="MP3 bitrate preset (default: high = 320k).",
        )

    def hash_options(self, args) -> dict:
        return {"to": args.to, "bitrate": QUALITY[args.quality]}

    def output_names(self, src: Path, args) -> list[str]:
        return [f"{src.stem}.mp3"]

    def process(self, item: Item, ctx: Context) -> Outcome:
        target = item.outputs[0]
        temp = temp_path(target)
        duration = probe(item.source).duration_s
        argv = [
            ctx.deps["ffmpeg"],
            "-y",
            "-i",
            str(item.source),
            "-vn",
            "-c:a",
            "libmp3lame",
            "-b:a",
            ctx.deps["options"]["bitrate"],
            "-f",
            "mp3",
            str(temp),
        ]

        def report(percent: float) -> None:
            ctx.reporter.progress(
                stage="convert",
                index=item.id,
                count=ctx.total_items,
                path=item.source.name,
                percent=percent,
            )

        code, stderr = run_ffmpeg(argv, total_s=duration, on_progress=report)
        if code != 0:
            temp.unlink(missing_ok=True)
            return Outcome(
                status="failed",
                outputs=[],
                bytes_out=None,
                reason="engine_error",
                data={"stderr": stderr},
            )
        fsync_replace(temp, target)
        return Outcome(status="done", outputs=[target], bytes_out=target.stat().st_size)
