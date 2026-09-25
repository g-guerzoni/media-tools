"""Tasks a deployment has switched off, and why a task is refused before it runs.

The prod container disables `download` (it fetches arbitrary URLs and runs their
JavaScript) and `ebook-kindle` (there is no USB in a container). Two sources feed the
disabled set, and it is their UNION:

- a file baked into the prod image at `BAKED_PATH`, one task id per line, on a
  read-only root filesystem and inside a digest-pinned image;
- the `MEDIA_TOOLS_DISABLED_TASKS` environment variable, comma-separated.

The environment can widen the set and never narrow it. On the prod host the compose
file is writable by a non-root deploy principal, so a control that an edit to that
file could remove would only be as strong as the file. With the union rule, the image
defends itself whatever its deployment says.

A baked file that exists but cannot be read fails CLOSED: no task can prove it is
allowed, so every task that does work is refused (`doctor`, `formats` and `status`,
which only report, still run so the problem can be seen).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

BAKED_PATH = Path("/usr/local/share/media-tools/disabled-tasks")
ENV_VAR = "MEDIA_TOOLS_DISABLED_TASKS"

# Every id a deployment can disable: each top-level task that does work, plus the
# Kindle subsystem, which lives under `ebook` but is a separate capability.
KNOWN_IDS = frozenset({"compress", "convert", "split", "download", "ebook", "ebook-kindle"})

# Reports about the tool itself. They are never disabled, so a deployment can always
# be asked what it refuses and why.
ALWAYS_ALLOWED = frozenset({"doctor", "formats", "status"})


@dataclass(frozen=True)
class DisabledSet:
    baked: frozenset[str]
    env: frozenset[str]
    unknown: frozenset[str]
    # The baked file exists but could not be read: fail closed.
    unreadable: str | None = None

    @property
    def ids(self) -> frozenset[str]:
        return self.baked | self.env


def _parse(text: str, sep: str) -> set[str]:
    return {part.strip() for part in text.split(sep) if part.strip()}


def load(env: Mapping[str, str] | None = None, path: Path | None = None) -> DisabledSet:
    environ = os.environ if env is None else env
    path = BAKED_PATH if path is None else path
    baked: set[str] = set()
    unreadable = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        baked = {line.strip() for line in lines if line.strip() and not line.startswith("#")}
    except FileNotFoundError:
        pass
    except OSError as error:
        unreadable = f"{path}: {error.strerror or error}"
    from_env = _parse(environ.get(ENV_VAR, ""), ",")
    everything = baked | from_env
    return DisabledSet(
        baked=frozenset(baked & KNOWN_IDS),
        env=frozenset(from_env & KNOWN_IDS),
        unknown=frozenset(everything - KNOWN_IDS),
        unreadable=unreadable,
    )


def task_id(args) -> str | None:
    """The id a parsed command line is checked against."""
    task = getattr(args, "task", None)
    if task == "ebook" and getattr(args, "ebook_command", None) == "kindle":
        return "ebook-kindle"
    return task


def refusal(args, disabled: DisabledSet) -> str | None:
    """Why this command may not run here, or None when it may."""
    ident = task_id(args)
    if ident is None or ident in ALWAYS_ALLOWED:
        return None
    if disabled.unreadable:
        return (
            f"the disabled-tasks file could not be read ({disabled.unreadable}), so no task "
            "can be shown to be allowed in this deployment"
        )
    # Disabling `ebook` also disables the Kindle subsystem beneath it.
    blocked = ident in disabled.ids or (ident == "ebook-kindle" and "ebook" in disabled.ids)
    if not blocked:
        return None
    by_image = ident in disabled.baked or (ident == "ebook-kindle" and "ebook" in disabled.baked)
    source = "this image" if by_image else ENV_VAR
    return f"`{ident}` is disabled in this deployment (by {source})"
