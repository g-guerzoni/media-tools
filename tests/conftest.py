"""Fixtures shared by every integration test: locate ffmpeg, and generate tiny clips
with it (via the `lavfi` synthetic sources) so tests never depend on fixture media
files or the network."""

from __future__ import annotations

import subprocess

import pytest

from media_tools.core.ffmpeg import ffmpeg_exe


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
def make_audio(tmp_path, ffmpeg_path):
    def _make(seconds: int = 2, name: str = "clip.m4a"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
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
                "aac",
                str(target),
            ],
            check=True,
            capture_output=True,
        )
        return target

    return _make
