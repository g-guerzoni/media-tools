"""Turn command-line paths into the list of files a task will process."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from media_tools.core.paths import RESERVED_ROOT_ENTRIES


class InputError(ValueError):
    """Raised when the given inputs cannot be used."""


# Set by `media-tools serve` for every job it runs: the caller's own input directory.
# Every source must resolve, symlinks followed, inside it. Checked here, where the
# sources are decided, rather than only when the API accepts the request, so that a
# symlink planted after submission, or a folder scan, still cannot reach out of it.
INPUT_ROOT_ENV = "MEDIA_TOOLS_INPUT_ROOT"


def _confine(sources: list[Source], root: Path) -> None:
    for source in sources:
        if not _is_inside(source.path, root):
            raise InputError(f"input resolves outside the permitted input directory: {source.path}")


@dataclass(frozen=True)
class Source:
    path: Path
    root: Path | None  # the folder argument this file came from, for mirroring


def parse_extensions(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {"." + part.strip().lstrip(".").lower() for part in raw.split(",") if part.strip()}


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True


def _walk(folder: Path) -> list[Path]:
    """Files under folder, never following symlinked directories."""
    found: list[Path] = []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_symlink() and entry.is_dir():
            continue
        if entry.is_dir():
            found.extend(_walk(entry))
        elif entry.is_file():
            found.append(entry)
    return found


def expand_inputs(
    paths: list[Path],
    *,
    recursive: bool,
    extensions: set[str] | None,
    accepted: set[str],
    output_root: Path,
    include: str | None = None,
    exclude: str | None = None,
    limit: int | None = None,
    warn: Callable[[str, str], None] | None = None,
) -> list[Source]:
    sources: list[Source] = []
    for given in paths:
        given = Path(given)
        if not given.exists():
            raise InputError(f"input not found: {given}")
        if given.is_file():
            if any(part in RESERVED_ROOT_ENTRIES for part in given.parts):
                raise InputError(
                    f"cannot use files from internal directories: {given} "
                    f"({', '.join(RESERVED_ROOT_ENTRIES)} are reserved)"
                )
            if given.suffix.lower() not in accepted:
                raise InputError(
                    f"unsupported input: {given.name} (supported: {', '.join(sorted(accepted))})"
                )
            # Spec 6.1/R30: -e/--extensions filters FOLDER scans only. An explicitly
            # named file that an engine accepts is still processed even when it does not
            # match an explicitly given -e — just with a warning, since naming a file
            # directly is a stronger signal of intent than a folder scan's filter.
            if (
                extensions is not None
                and given.suffix.lower() not in extensions
                and warn is not None
            ):
                warn(
                    "extension_filter_bypassed",
                    f"{given} does not match -e/--extensions "
                    f"({', '.join(sorted(extensions))}); processing it anyway because "
                    f"it was named explicitly",
                )
            sources.append(Source(path=given, root=None))
            continue

        named_output_area = _is_inside(given, output_root)
        wanted = extensions or accepted
        candidates = (
            _walk(given)
            if recursive
            else [p for p in sorted(given.iterdir(), key=lambda p: p.name.lower()) if p.is_file()]
        )
        for candidate in candidates:
            if candidate.suffix.lower() not in wanted or candidate.suffix.lower() not in accepted:
                continue
            if not named_output_area and _is_inside(candidate, output_root):
                continue
            if any(part in RESERVED_ROOT_ENTRIES for part in candidate.parts):
                continue
            sources.append(Source(path=candidate, root=given))

    confine_to = os.environ.get(INPUT_ROOT_ENV)
    if confine_to:
        _confine(sources, Path(confine_to))
    sources.sort(key=lambda s: str(s.path).lower())
    if include:
        needle = include.lower()
        sources = [s for s in sources if needle in str(s.path).lower()]
    if exclude:
        needle = exclude.lower()
        sources = [s for s in sources if needle not in str(s.path).lower()]
    if limit is not None:
        sources = sources[:limit]
    return sources
