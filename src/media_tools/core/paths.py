"""Where output goes: the output root, batch folders, mirrored paths and temp names."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path

RESERVED_ROOT_ENTRIES = (".cache", "_kindle")
MAX_BATCH_NAME = 80
_ILLEGAL = re.compile(r"[^\w.\-]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_REPEATS = re.compile(r"-{2,}")


class BatchNameError(ValueError):
    """Raised when a batch name cannot be used."""


def _checkout_root() -> Path | None:
    """The repo root when running from a clone (editable install), else None."""
    repo = Path(__file__).resolve().parents[3]
    pyproject = repo / "pyproject.toml"
    if (
        (repo / "src" / "media_tools").is_dir()
        and pyproject.is_file()
        and 'name = "media-tools"' in pyproject.read_text(encoding="utf-8")
    ):
        return repo
    return None


def output_root(cli_value: Path | None, env: Mapping[str, str] | None = None) -> Path:
    if cli_value is not None:
        return Path(cli_value)
    environ = os.environ if env is None else env
    from_env = environ.get("MEDIA_TOOLS_OUT")
    if from_env:
        return Path(from_env)
    repo = _checkout_root()
    if repo is not None:
        try:
            Path.cwd().relative_to(repo)
            return repo / "media"
        except ValueError:
            pass
    return Path.cwd() / "media"


def sanitize_batch(name: str) -> str:
    text = unicodedata.normalize("NFC", name or "").strip()
    text = _WHITESPACE.sub("-", text)
    text = _ILLEGAL.sub("", text)
    text = _REPEATS.sub("-", text).strip("-")[:MAX_BATCH_NAME]
    if not text:
        raise BatchNameError(f"batch name is empty after sanitising: {name!r}")
    if text.startswith((".", "_")):
        raise BatchNameError(f"batch names cannot start with '.' or '_' (reserved): {text!r}")
    return text


def batch_hash(*, task: str, options: dict, selection: dict, inputs: list[Path]) -> str:
    payload = {
        "hash_v": 1,
        "task": task,
        "options": options,
        "selection": selection,
        "inputs": sorted(str(Path(p).resolve()) for p in inputs),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


def mirror_output(src: Path, input_root: Path | None, batch_dir: Path, new_name: str) -> Path:
    if input_root is None:
        return batch_dir / new_name
    try:
        relative = src.parent.relative_to(input_root)
    except ValueError:
        relative = Path()
    return batch_dir / relative / new_name


def temp_path(final: Path) -> Path:
    return final.parent / f".{final.name}.partial"


def truncate_name(name: str, limit: int) -> str:
    if len(name) <= limit:
        return name
    suffix = "".join(Path(name).suffixes[-1:])
    marker = "~" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:4]
    keep = max(1, limit - len(marker) - len(suffix))
    return f"{name[:keep]}{marker}{suffix}"
