"""Convert one ebook to another format with Calibre."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from media_tools.core.paths import fsync_replace
from media_tools.core.runner import Context, Item, Outcome
from media_tools.integrations import calibre

EBOOK_INPUTS = frozenset({".epub", ".mobi", ".azw", ".azw3", ".prc", ".pdf"})
EBOOK_OUTPUTS = frozenset({"azw3", "epub", "mobi", "pdf"})


class EbookEngine:
    name = "ebook"
    inputs = EBOOK_INPUTS
    outputs = EBOOK_OUTPUTS
    dependencies = (calibre.EBOOK_CONVERT,)

    def add_arguments(self, group) -> None:
        """No options of its own: `--to` already selects the target format."""

    def hash_options(self, args) -> dict:
        return {"to": args.to}

    def output_names(self, src: Path, args) -> list[str]:
        return [f"{src.stem}.{args.to}"]

    def process(self, item: Item, ctx: Context) -> Outcome:
        target = item.outputs[0]
        if item.source.suffix.lower().lstrip(".") == target.suffix.lower().lstrip("."):
            return Outcome(
                status="skipped", outputs=[], bytes_out=None, reason="already_target_format"
            )

        # Unlike ffmpeg, `ebook-convert` has no "-f <format>" equivalent: it infers the
        # output format solely from the destination filename's own extension, so a temp
        # name ending in this project's usual ".partial" marker (core.paths.temp_path)
        # makes it fail with "No plugin to handle output format: partial". Instead, write
        # to a uniquely-named scratch file with the real extension under `cache_dir` —
        # the same mkstemp-and-clean-up-in-a-try pattern `calibre.read_metadata` already
        # uses for its own throwaway OPF file — then fsync+rename it into place, which is
        # still exactly the atomic "temp + fsync + rename" guarantee `fsync_replace` exists
        # for; it is simply not this project's `.partial` file, since it never sits under
        # the batch dir where a stale one would need sweeping.
        cache_dir = Path(ctx.deps["cache_dir"])
        calibre.config_env(cache_dir)  # ensure cache_dir exists before mkstemp writes into it
        handle, temp_name = tempfile.mkstemp(suffix=target.suffix, dir=cache_dir)
        os.close(handle)
        temp = Path(temp_name)
        try:
            calibre.convert(item.source, temp, opf=None, cover=None, cache_dir=cache_dir)
        except calibre.CalibreError as error:
            temp.unlink(missing_ok=True)
            return Outcome(
                status="failed",
                outputs=[],
                bytes_out=None,
                reason="engine_error",
                data={"error": str(error)},
            )
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        fsync_replace(temp, target)
        return Outcome(status="done", outputs=[target], bytes_out=target.stat().st_size)
