"""Fixtures shared by every integration test: locate ffmpeg, and generate tiny clips
with it (via the `lavfi` synthetic sources) so tests never depend on fixture media
files or the network."""

from __future__ import annotations

import subprocess

import pytest

from media_tools.core.ffmpeg import ffmpeg_exe
from media_tools.integrations import calibre

# Shared by every test module gated on a real Calibre install (metadata/convert tests
# today; the future convert-engine and build-pipeline tests per the ebook plan), so the
# skip condition and its reason are defined once instead of duplicated per file.
requires_calibre = pytest.mark.skipif(
    calibre.find_tool("ebook-convert") is None, reason="Calibre is not installed"
)


@pytest.fixture(scope="session")
def ffmpeg_path() -> str:
    return ffmpeg_exe()


@pytest.fixture
def make_video(tmp_path, ffmpeg_path):
    def _make(
        seconds: int = 2,
        name: str = "clip.mp4",
        size: str = "320x240",
        gop: int | None = None,
    ):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        args = [
            ffmpeg_path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={size}:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            str(seconds),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
        ]
        if gop is not None:
            # An explicit keyframe interval, not the encoder's own default: a test that
            # needs actual cut points (e.g. `split`) must not depend on how far apart an
            # ffmpeg build happens to place keyframes by default — that differs enough
            # between builds/platforms to change which `--max-size` values are reachable
            # at all (see tests/integration/test_split.py).
            args += ["-g", str(gop)]
        args += ["-c:a", "aac", "-shortest", str(target)]
        subprocess.run(args, check=True, capture_output=True)
        return target

    return _make


@pytest.fixture
def make_epub(tmp_path):
    """Build a real EPUB with Calibre so metadata tests have something honest to read."""

    def _make(title="Test Book", author="Test Author", language="en", name=None):
        source = tmp_path / f"{name or title}.html"
        source.write_text(
            f"<html><head><title>{title}</title></head>"
            f"<body><h1>{title}</h1><p>Body text.</p></body></html>",
            encoding="utf-8",
        )
        target = source.with_suffix(".epub")
        subprocess.run(
            [
                calibre.find_tool("ebook-convert"),
                str(source),
                str(target),
                "--title",
                title,
                "--authors",
                author,
                "--language",
                language,
            ],
            check=True,
            capture_output=True,
            env={**calibre.config_env(tmp_path / "cache"), "PATH": "/usr/bin:/bin"},
        )
        return target

    return _make


# Every container only accepts certain audio codecs (an .mp3 muxer rejects AAC, for
# example), so the encoder must match the fixture's own target extension rather than
# defaulting to AAC for every name.
_AUDIO_CODEC_FOR_SUFFIX = {".mp3": "libmp3lame", ".flac": "flac", ".wav": "pcm_s16le"}


@pytest.fixture
def make_audio(tmp_path, ffmpeg_path):
    def _make(seconds: int = 2, name: str = "clip.m4a"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        codec = _AUDIO_CODEC_FOR_SUFFIX.get(target.suffix.lower(), "aac")
        subprocess.run(
            [
                ffmpeg_path,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440",
                "-t",
                str(seconds),
                "-c:a",
                codec,
                str(target),
            ],
            check=True,
            capture_output=True,
        )
        return target

    return _make


@pytest.fixture
def fake_kindle(tmp_path):
    """A directory shaped like a mass-storage Kindle, with two books already on it."""
    from media_tools.tasks.ebook.kindle.detect import Device

    root = tmp_path / "Kindle"
    (root / "documents" / "en").mkdir(parents=True)
    (root / "documents" / "pt").mkdir(parents=True)
    (root / "system" / "thumbnails").mkdir(parents=True)
    (root / "system" / "wifi").mkdir(parents=True)
    (root / "audible").mkdir()
    (root / "documents" / "en" / "A Book - An Author.azw3").write_bytes(b"english book")
    (root / "documents" / "en" / "A Book - An Author.sdr").mkdir()
    (root / "documents" / "pt" / "Um Livro - Um Autor.azw3").write_bytes(b"livro")
    (root / "system" / "thumbnails" / "cover.jpg").write_bytes(b"thumb")
    (root / "system" / "wifi" / "wifi.cfg").write_bytes(b"wifi config")
    (root / "My Clippings.txt").write_text("clippings", encoding="utf-8")
    return Device(serial="G000TESTSERIAL", product_id=0x0004, mode="mass_storage", mount=root)
