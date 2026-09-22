from media_tools.core.media_formats import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS


def test_video_extensions_are_dot_prefixed_lowercase():
    assert {
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
    } == VIDEO_EXTENSIONS
    assert isinstance(VIDEO_EXTENSIONS, frozenset)


def test_audio_extensions_are_dot_prefixed_lowercase():
    assert {
        ".mp3",
        ".m4a",
        ".aac",
        ".webm",
        ".opus",
        ".ogg",
        ".wav",
        ".flac",
    } == AUDIO_EXTENSIONS
    assert isinstance(AUDIO_EXTENSIONS, frozenset)


def test_extensions_do_not_share_identity_but_may_overlap():
    # .webm is a legitimate container for both audio-only and video content.
    assert ".webm" in VIDEO_EXTENSIONS
    assert ".webm" in AUDIO_EXTENSIONS
