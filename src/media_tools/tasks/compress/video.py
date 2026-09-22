"""Compress video with ffmpeg (libx264 / libx265)."""

from __future__ import annotations

from pathlib import Path

from media_tools.core.ffmpeg import FFMPEG, probe, run_ffmpeg
from media_tools.core.media_formats import VIDEO_EXTENSIONS
from media_tools.core.paths import temp_path
from media_tools.core.runner import Context, Item, Outcome

PRESETS = {
    "high": ("h264", 23),
    "medium": ("h264", 28),
    "small": ("h265", 28),
    "tiny": ("h265", 32),
}
CODEC_LIBRARY = {"h264": "libx264", "h265": "libx265"}


class VideoEngine:
    name = "video"
    inputs = VIDEO_EXTENSIONS
    outputs = frozenset({"mp4"})
    dependencies = (FFMPEG,)

    def add_arguments(self, group) -> None:
        group.add_argument(
            "-p",
            "--preset",
            default="medium",
            choices=sorted(PRESETS),
            help="Quality/size preset (default: medium).",
        )
        group.add_argument(
            "--codec",
            default=None,
            choices=sorted(CODEC_LIBRARY),
            help="Override the preset's codec.",
        )
        group.add_argument(
            "--crf",
            type=int,
            default=None,
            help="Override the preset's CRF (0-51; lower is better quality).",
        )
        group.add_argument("--audio-bitrate", default="96k", help="AAC bitrate (default: 96k).")
        group.add_argument("--mono", action="store_true", help="Downmix audio to mono.")

    def hash_options(self, args) -> dict:
        codec, crf = PRESETS[args.preset]
        return {
            "codec": args.codec or codec,
            "crf": args.crf if args.crf is not None else crf,
            "audio_bitrate": args.audio_bitrate,
            "mono": bool(args.mono),
        }

    def output_names(self, src: Path, args) -> list[str]:
        return [f"{src.stem}.mp4"]

    def process(self, item: Item, ctx: Context) -> Outcome:
        options = ctx.deps["options"]
        target = item.outputs[0]
        temp = temp_path(target)
        duration = probe(item.source).duration_s

        argv = [
            ctx.deps["ffmpeg"],
            "-y",
            "-i",
            str(item.source),
            "-c:v",
            CODEC_LIBRARY[options["codec"]],
            "-crf",
            str(options["crf"]),
            "-preset",
            "slow",
            "-c:a",
            "aac",
            "-b:a",
            options["audio_bitrate"],
        ]
        if options["mono"]:
            argv += ["-ac", "1"]
        argv += ["-movflags", "+faststart", "-f", "mp4", str(temp)]

        def report(percent: float) -> None:
            ctx.reporter.progress(
                stage="encode",
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

        temp.replace(target)
        size_in = item.source.stat().st_size
        size_out = target.stat().st_size
        warnings = ["no_gain"] if size_out >= size_in else []
        return Outcome(status="done", outputs=[target], bytes_out=size_out, warnings=warnings)
