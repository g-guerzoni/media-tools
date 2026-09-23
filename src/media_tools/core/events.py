"""The two output streams: JSON Lines on stdout for agents, text on stderr for people."""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from media_tools.core.redact import redact, redact_text
from media_tools.core.sizes import format_size

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_DEPENDENCY = 3
EXIT_INTERRUPTED = 130

SCHEMA_VERSION = 1

ITEM_STATUSES = frozenset({"done", "skipped", "failed", "pending"})

REASONS = frozenset(
    {
        "exists",
        "no_gain",
        "already_target_format",
        "unsupported_input",
        "output_collision",
        "output_equals_input",
        "source_missing",
        "under_limit",
        "keyframe_interval_exceeds_max_size",
        "size_limit_unreachable",
        "engine_error",
        "dependency_missing",
        "device_rejected",
        "no_audio_only_format",
        "llm_unavailable",
        "no_cover",
    }
)

ERROR_CODES = frozenset(
    {
        "usage",
        "no_input_matched",
        "batch_in_use",
        "batch_task_mismatch",
        "dependency_missing",
        "config_missing",
        "device_not_found",
        "device_busy",
        "device_write_protected",
        "backup_failed",
        "interrupted",
        "output_not_writable",
        "internal_error",
        "extraction_failed",
    }
)

WARNING_CODES = frozenset(
    {
        "no_gain",
        "no_audio_only_format",
        "cover_not_embedded",
        "book_id_missing",
        "book_id_unreadable",
        "extension_filter_bypassed",
        "device_rejected_thumbnail",
        "sidecar_not_removed",
        "hash_from_previous",
        "name_collision_suffixed",
        "leftover_book",
    }
)

_MARKS = {"done": "✓", "skipped": "-", "failed": "✗", "pending": "·"}


class Reporter:
    """Emits progress. `json_mode` sends events to stdout and silences the human stream."""

    def __init__(self, *, json_mode: bool, quiet: bool, stdout=None, stderr=None) -> None:
        self.json_mode = json_mode
        self.quiet = quiet
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        self._started = time.monotonic()

    # -- plumbing ---------------------------------------------------------
    def _emit(self, type_: str, **fields: Any) -> None:
        if not self.json_mode:
            return
        payload = {"v": SCHEMA_VERSION, "type": type_, **redact(fields)}
        self.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.stdout.flush()

    def _say(self, text: str) -> None:
        if self.json_mode or self.quiet:
            return
        self.stderr.write(redact_text(text) + "\n")
        self.stderr.flush()

    @staticmethod
    def _check(value: str | None, allowed: frozenset[str], label: str) -> str | None:
        if value is not None and value not in allowed:
            raise KeyError(f"unknown {label}: {value!r}")
        return value

    # -- events -----------------------------------------------------------
    def start(self, *, tool, batch, output_dir, stages, items, options) -> None:
        self._emit(
            "start",
            tool=tool,
            batch=batch,
            output_dir=str(output_dir),
            stages=list(stages),
            items=items,
            options=options,
        )
        summary = " · ".join(f"{k}={v}" for k, v in options.items()) or "defaults"
        self._say(f"{tool} · batch {batch} · {items} item(s) · {summary} · → {output_dir}")

    def stage(self, *, stage, index, count) -> None:
        self._emit("stage", stage=stage, index=index, count=count)
        self._say(f"[{index}/{count} {stage}]")

    def progress(self, *, stage, index, count, path, percent, eta_s=None) -> None:
        self._emit(
            "progress",
            stage=stage,
            item={"index": index, "count": count, "path": str(path)},
            percent=round(percent, 1),
            eta_s=eta_s,
        )
        self._say(f"  ({index}/{count}) {path} {percent:.0f}%")

    def item(
        self,
        *,
        id,
        status,
        input,
        outputs,
        bytes_in,
        bytes_out=None,
        reason=None,
        detail=None,
        warnings=None,
    ) -> None:
        """One finished item. `detail` is free text that NARROWS `reason`, for the
        cases where the closed registry has one code covering several distinct causes
        (`ebook kindle add`'s `engine_error`: out of space, a short write, a refused
        write, a failed verification). It is emitted on every `item`, `null` for every
        task that has nothing to add, so an agent parsing this event always gets a
        missing VALUE rather than a missing KEY — and a producer that uses it must
        start it with a stable, documented prefix, because free English prose is
        exactly what a machine-readable `reason` exists to avoid."""
        self._check(status, ITEM_STATUSES, "item status")
        self._check(reason, REASONS, "reason")
        for code in warnings or []:
            self._check(code, WARNING_CODES, "warning code")
        self._emit(
            "item",
            id=id,
            status=status,
            input=str(input),
            outputs=[str(o) for o in outputs],
            bytes_in=bytes_in,
            bytes_out=bytes_out,
            reason=reason,
            detail=detail,
            warnings=list(warnings or []),
        )
        size = f" {format_size(bytes_in)}→{format_size(bytes_out)}" if bytes_out else ""
        # `detail` when there is one: "engine_error" tells a human nothing, while
        # "out_of_space: ..." tells them what to do about it.
        note = f" ({detail or reason})" if (detail or reason) else ""
        self._say(f"  {_MARKS[status]} {redact(str(input))}{size}{note}")

    def warning(self, *, code, message) -> None:
        self._check(code, WARNING_CODES, "warning code")
        self._emit("warning", code=code, message=message)
        self._say(f"  ! {message}")

    def error(self, *, code, message, hint=None, retryable=False) -> None:
        self._check(code, ERROR_CODES, "error code")
        self._emit("error", code=code, message=message, hint=hint, retryable=retryable)
        redacted_msg = redact_text(message)
        redacted_hint = redact_text(hint) if hint else None
        text = f"error: {redacted_msg}" + (f"\n  hint: {redacted_hint}" if redacted_hint else "")
        self.stderr.write(text + "\n")
        self.stderr.flush()

    def result(
        self,
        *,
        ok,
        exit_code,
        counts,
        failed,
        pending,
        outputs,
        run_file,
        elapsed_s=None,
        data=None,
    ) -> None:
        elapsed = time.monotonic() - self._started if elapsed_s is None else elapsed_s
        fields = dict(
            ok=ok,
            exit_code=exit_code,
            counts=counts,
            failed=failed,
            pending=pending,
            outputs=[str(o) for o in outputs],
            run_file=str(run_file) if run_file else None,
            elapsed_s=round(elapsed, 2),
        )
        # `data` is opt-in and left out entirely for every task that never passes it, so
        # the `result` event's shape for compress/convert/split/download is unchanged.
        # The ebook task uses it to carry the LLM cost summary (requests, cache hits,
        # tokens, heuristic fallbacks) — the only warning before a large bill (spec 8.3).
        if data is not None:
            fields["data"] = data
        self._emit("result", **fields)
        parts = " · ".join(f"{k} {v}" for k, v in counts.items() if v)
        llm = data.get("llm") if data else None
        llm_part = ""
        # Only worth a line when the LLM was actually used this run — a --no-llm run's
        # all-zero summary would otherwise print a misleading "llm 0 req" every time.
        # `heuristic_fallback_batches` is included here (not just requests/cache_hits)
        # so a total outage — every request failing and silently falling back to the
        # heuristic, `requests` and `cache_hits` both 0 — still prints a line instead
        # of hiding the exact case this summary exists to surface.
        if llm and (
            llm.get("requests") or llm.get("cache_hits") or llm.get("heuristic_fallback_batches")
        ):
            llm_part = (
                f" · llm {llm.get('requests', 0)} req"
                f" · {llm.get('cache_hits', 0)} cached"
                f" · {llm.get('prompt_tokens', 0)}+{llm.get('completion_tokens', 0)} tok"
            )
            fallback = llm.get("heuristic_fallback_batches", 0)
            if fallback:
                llm_part += f" · {fallback} batch(es) fell back to heuristic"
        # A `result` with every count at 0 and no `run_file` never owned any work — most
        # commonly a `UsageError` from before a task's own `start` (a bad flag, no input
        # given, ...), but also a batch conflict or a pre-batch Ctrl+C. There is nothing
        # to summarise there beyond what the `error` line already said, and printing one
        # anyway reads as a content-free "✗  · 0.0s" (note the double space where the
        # counts would go) on every such failure of every task.
        if any(counts.values()) or run_file is not None:
            self._say(f"{'✓' if ok else '✗'} {parts} · {elapsed:.1f}s{llm_part}")
