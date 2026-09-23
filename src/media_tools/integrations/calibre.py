"""Calibre's command-line tools: locating them, reading metadata, converting books.

Every call runs with an isolated CALIBRE_CONFIG_DIRECTORY so a run is reproducible
and the user's own Calibre configuration is neither read nor written.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from media_tools.core.engine import Dependency

_EXTRA_DIRS = (
    Path("/Applications/calibre.app/Contents/MacOS"),
    Path.home() / "Applications/calibre.app/Contents/MacOS",
    Path("/opt/homebrew/bin"),
    Path("/opt/calibre"),
)
_INSTALL_HINT = (
    "install Calibre — macOS: brew install --cask calibre; "
    "Linux: sudo -v && wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh "
    "| sudo sh /dev/stdin"
)
_OPF_NS = {"dc": "http://purl.org/dc/elements/1.1/", "opf": "http://www.idpf.org/2007/opf"}

# `ebook-meta --to-opf` always normalises <dc:language> to ISO 639-2/T (a book tagged
# "en" or "pt" comes back "eng"/"por"), never the two-letter form the rest of this app
# groups books by. This maps the common ones back; a code that isn't in the table is
# returned unchanged — still a stable, if uncommon, bucket, rather than a crash.
_ISO_639_2_TO_1 = {
    "eng": "en",
    "por": "pt",
    "spa": "es",
    "fra": "fr",
    "deu": "de",
    "ita": "it",
    "nld": "nl",
    "swe": "sv",
    "nor": "no",
    "dan": "da",
    "fin": "fi",
    "pol": "pl",
    "ces": "cs",
    "slk": "sk",
    "hun": "hu",
    "ron": "ro",
    "bul": "bg",
    "ell": "el",
    "rus": "ru",
    "ukr": "uk",
    "tur": "tr",
    "ara": "ar",
    "heb": "he",
    "jpn": "ja",
    "zho": "zh",
    "kor": "ko",
    "vie": "vi",
    "tha": "th",
    "hin": "hi",
    "ind": "id",
    "cat": "ca",
    "eus": "eu",
    "glg": "gl",
    "isl": "is",
    "gle": "ga",
    "lit": "lt",
    "lav": "lv",
    "est": "et",
    "hrv": "hr",
    "srp": "sr",
    "slv": "sl",
}


class CalibreError(RuntimeError):
    """A Calibre command failed."""


def find_tool(name: str) -> str | None:
    on_path = shutil.which(name)
    if on_path:
        return on_path
    for folder in _EXTRA_DIRS:
        candidate = folder / name
        if candidate.exists():
            return str(candidate)
    return None


EBOOK_CONVERT = Dependency(
    name="ebook-convert", locate=lambda: find_tool("ebook-convert"), install_hint=_INSTALL_HINT
)
EBOOK_META = Dependency(
    name="ebook-meta", locate=lambda: find_tool("ebook-meta"), install_hint=_INSTALL_HINT
)
FETCH_COVER = Dependency(
    name="fetch-ebook-metadata",
    locate=lambda: find_tool("fetch-ebook-metadata"),
    install_hint=_INSTALL_HINT,
)
# Calibre's own interpreter, used to run `integrations/kindle_mtp.py` — the only way
# to reach `calibre.devices.mtp.driver`, which cannot be imported from outside it.
CALIBRE_DEBUG = Dependency(
    name="calibre-debug", locate=lambda: find_tool("calibre-debug"), install_hint=_INSTALL_HINT
)


def config_env(cache_dir: Path) -> dict[str, str]:
    config = Path(cache_dir) / "calibre-config"
    config.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "CALIBRE_CONFIG_DIRECTORY": str(config)}


@dataclass(frozen=True)
class BookMetadata:
    title: str | None
    author: str | None
    language: str | None
    uuid: str | None
    has_cover: bool


def _run(argv: list[str], *, cache_dir: Path, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=config_env(cache_dir)
        )
    except (subprocess.SubprocessError, OSError) as error:
        raise CalibreError(f"{argv[0]} failed: {error}") from error


def read_metadata(path: Path, *, cache_dir: Path, timeout: int = 120) -> BookMetadata:
    """Read title/author/language/uuid/has_cover via `ebook-meta --to-opf`.

    `--to-opf` takes a destination file, not stdout (`--to-opf` alone is a usage
    error), so this writes to a throwaway file under `cache_dir` and parses that.
    The uuid returned is whatever identifier Calibre's metadata layer reports for
    the file — it is not guaranteed stable across calls and is not what the ebook
    task uses to identify a book on a Kindle (see `tasks.ebook.opf.book_id`, which
    is derived from the source's own content hash instead).
    """
    tool = EBOOK_META.locate()
    if tool is None:
        raise CalibreError(f"ebook-meta not found. {_INSTALL_HINT}")
    empty = BookMetadata(None, None, None, None, False)
    cache_dir = Path(cache_dir)
    config_env(cache_dir)  # ensure cache_dir exists before writing the temp OPF into it
    handle, opf_name = tempfile.mkstemp(suffix=".opf", dir=cache_dir)
    os.close(handle)
    opf_path = Path(opf_name)
    try:
        proc = _run([tool, str(path), f"--to-opf={opf_path}"], cache_dir=cache_dir, timeout=timeout)
        if proc.returncode != 0 or opf_path.stat().st_size == 0:
            return empty
        try:
            root = ElementTree.fromstring(opf_path.read_text(encoding="utf-8"))
        except ElementTree.ParseError:
            return empty
    finally:
        opf_path.unlink(missing_ok=True)

    def text(tag: str) -> str | None:
        node = root.find(f".//dc:{tag}", _OPF_NS)
        value = (node.text or "").strip() if node is not None else ""
        return value or None

    uuid = None
    for node in root.findall(".//dc:identifier", _OPF_NS):
        value = (node.text or "").strip()
        if not value:
            continue
        scheme = node.get("{http://www.idpf.org/2007/opf}scheme", "").lower()
        if scheme == "uuid":
            uuid = value
            break
        if uuid is None:
            uuid = value
    has_cover = any(
        item.get("name") == "cover" for item in root.findall(".//opf:meta", _OPF_NS)
    ) or any(
        ref.get("type") == "cover" for ref in root.findall(".//opf:guide/opf:reference", _OPF_NS)
    )
    language = text("language")
    if language:
        language = _ISO_639_2_TO_1.get(language.lower(), language.lower())
    return BookMetadata(
        title=text("title"),
        author=text("creator"),
        language=language,
        uuid=uuid,
        has_cover=has_cover,
    )


def convert(
    src: Path,
    dst: Path,
    *,
    opf: Path | None,
    cover: Path | None,
    cache_dir: Path,
    timeout: int = 900,
) -> None:
    tool = EBOOK_CONVERT.locate()
    if tool is None:
        raise CalibreError(f"ebook-convert not found. {_INSTALL_HINT}")
    argv = [tool, str(src), str(dst)]
    if opf is not None:
        argv += ["--from-opf", str(opf)]
    if cover is not None:
        argv += ["--cover", str(cover)]
    proc = _run(argv, cache_dir=cache_dir, timeout=timeout)
    if proc.returncode != 0 or not dst.exists():
        tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-10:])
        raise CalibreError(tail or f"ebook-convert exited {proc.returncode}")


def update_metadata(
    path: Path,
    *,
    title: str,
    author: str | None,
    language: str | None,
    cache_dir: Path,
    timeout: int = 120,
) -> None:
    """Rewrite a book's embedded title/author/language in place via `ebook-meta`
    (RB20). `library.reconcile` renames a file whose stable book id matches an
    already-converted copy instead of reconverting it, on the (until now false)
    assumption that renaming alone is enough — but the file's *embedded* metadata
    still carries whatever title/author/language it was converted with under its
    OLD name. Calling this on the renamed file is what makes the rename honest:
    the file genuinely matches the plan afterward, instead of merely sitting at
    the right path with stale metadata that `_verify_output` (rightly) rejects."""
    tool = EBOOK_META.locate()
    if tool is None:
        raise CalibreError(f"ebook-meta not found. {_INSTALL_HINT}")
    argv = [tool, str(path), f"--title={title}"]
    if author:
        argv.append(f"--authors={author}")
    if language:
        argv.append(f"--language={language}")
    proc = _run(argv, cache_dir=cache_dir, timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-10:])
        raise CalibreError(tail or f"ebook-meta exited {proc.returncode}")


def extract_cover(src: Path, dest: Path, *, cache_dir: Path, timeout: int = 120) -> bool:
    tool = EBOOK_META.locate()
    if tool is None:
        return False
    dest.unlink(missing_ok=True)
    try:
        _run([tool, str(src), "--get-cover", str(dest)], cache_dir=cache_dir, timeout=timeout)
    except CalibreError:
        return False
    return dest.exists() and dest.stat().st_size > 1000


def fetch_cover(
    title: str, author: str | None, dest: Path, *, cache_dir: Path, timeout: int = 90
) -> bool:
    """Look up a cover online by title/author. Network — callers must gate this
    themselves (e.g. behind the `network` test marker, or a user-facing --offline flag)."""
    tool = FETCH_COVER.locate()
    if tool is None:
        return False
    argv = [tool, "--title", title, "--cover", str(dest)]
    if author:
        argv += ["--authors", author]
    try:
        _run(argv, cache_dir=cache_dir, timeout=timeout)
    except CalibreError:
        return False
    return dest.exists() and dest.stat().st_size > 1000
