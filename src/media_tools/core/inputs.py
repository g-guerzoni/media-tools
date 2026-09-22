"""Turn command-line paths into the list of files a task will process."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from media_tools.core.paths import RESERVED_ROOT_ENTRIES


class InputError(ValueError):
    """Raised when the given inputs cannot be used."""


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
) -> list[Source]:
    sources: list[Source] = []
    for given in paths:
        given = Path(given)
        if not given.exists():
            raise InputError(f"input not found: {given}")
        if given.is_file():
            if given.suffix.lower() not in accepted:
                raise InputError(
                    f"unsupported input: {given.name} (supported: {', '.join(sorted(accepted))})"
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

    if include:
        needle = include.lower()
        sources = [s for s in sources if needle in str(s.path).lower()]
    if exclude:
        needle = exclude.lower()
        sources = [s for s in sources if needle not in str(s.path).lower()]
    if limit is not None:
        sources = sources[:limit]
    return sources
