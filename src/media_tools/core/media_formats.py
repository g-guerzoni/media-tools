"""Extension sets shared by every task that engines classify files with.

Kept here, in `core`, rather than inside a single task package, because more than one
task package needs each set (video compression and conversion both need
`VIDEO_EXTENSIONS`; audio tasks need `AUDIO_EXTENSIONS`) — a task package importing
from a sibling task package would be a layering violation.
"""

from __future__ import annotations

VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".mov",
        ".mkv",
        ".webm",
        ".avi",
        ".m4v",
        ".flv",
        ".wmv",
        ".ts",
        ".mpg",
        ".mpeg",
    }
)

AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp3",
        ".m4a",
        ".aac",
        ".webm",
        ".opus",
        ".ogg",
        ".wav",
        ".flac",
    }
)
