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
        "backup_failed",
        "interrupted",
        "output_not_writable",
        "internal_error",
    }
)

WARNING_CODES = frozenset(
    {
        "no_gain",
        "no_audio_only_format",
        "cover_not_embedded",
        "book_id_missing",
        "extension_filter_bypassed",
        "device_rejected_thumbnail",
        "hash_from_previous",
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
        self, *, id, status, input, outputs, bytes_in, bytes_out=None, reason=None, warnings=None
    ) -> None:
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
            warnings=list(warnings or []),
        )
        size = f" {format_size(bytes_in)}→{format_size(bytes_out)}" if bytes_out else ""
        note = f" ({reason})" if reason else ""
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
        self, *, ok, exit_code, counts, failed, pending, outputs, run_file, elapsed_s=None
    ) -> None:
        elapsed = time.monotonic() - self._started if elapsed_s is None else elapsed_s
        self._emit(
            "result",
            ok=ok,
            exit_code=exit_code,
            counts=counts,
            failed=failed,
            pending=pending,
            outputs=[str(o) for o in outputs],
            run_file=str(run_file) if run_file else None,
            elapsed_s=round(elapsed, 2),
        )
        parts = " · ".join(f"{k} {v}" for k, v in counts.items() if v)
        self._say(f"{'✓' if ok else '✗'} {parts} · {elapsed:.1f}s")
