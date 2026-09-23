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
    def _make(seconds: int = 2, name: str = "clip.mp4", size: str = "320x240"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
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
                "-c:a",
                "aac",
                "-shortest",
                str(target),
            ],
            check=True,
            capture_output=True,
        )
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
