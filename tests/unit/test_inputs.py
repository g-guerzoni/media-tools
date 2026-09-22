from pathlib import Path

import pytest

from media_tools.core.inputs import InputError, Source, expand_inputs, parse_extensions

ACCEPTED = {".mp4", ".mkv"}


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def test_parse_extensions_normalises():
    assert parse_extensions("MP4, .mkv ,mov") == {".mp4", ".mkv", ".mov"}
    assert parse_extensions(None) == set()


def test_folder_scan_respects_extensions_and_recursion(tmp_path):
    _touch(tmp_path / "in" / "a.mp4")
    _touch(tmp_path / "in" / "b.txt")
    _touch(tmp_path / "in" / "sub" / "c.mkv")

    flat = expand_inputs(
        [tmp_path / "in"],
        recursive=False,
        extensions=ACCEPTED,
        accepted=ACCEPTED,
        output_root=tmp_path / "media",
    )
    assert [s.path.name for s in flat] == ["a.mp4"]
    assert flat[0].root == tmp_path / "in"

    deep = expand_inputs(
        [tmp_path / "in"],
        recursive=True,
        extensions=ACCEPTED,
        accepted=ACCEPTED,
        output_root=tmp_path / "media",
    )
    assert [s.path.name for s in deep] == ["a.mp4", "c.mkv"]


def test_named_file_is_accepted_even_outside_extension_filter(tmp_path):
    named = _touch(tmp_path / "clip.mkv")
    out = expand_inputs(
        [named],
        recursive=False,
        extensions={".mp4"},
        accepted=ACCEPTED,
        output_root=tmp_path / "media",
    )
    assert out == [Source(path=named, root=None)]


def test_named_file_with_unsupported_extension_raises(tmp_path):
    named = _touch(tmp_path / "notes.txt")
    with pytest.raises(InputError):
        expand_inputs(
            [named],
            recursive=False,
            extensions=None,
            accepted=ACCEPTED,
            output_root=tmp_path / "media",
        )


def test_output_root_is_skipped_unless_named(tmp_path):
    media = tmp_path / "media"
    _touch(media / "old-batch" / "a.mp4")
    _touch(media / ".cache" / "b.mp4")
    _touch(tmp_path / "in" / "keep.mp4")

    scan = expand_inputs(
        [tmp_path],
        recursive=True,
        extensions=ACCEPTED,
        accepted=ACCEPTED,
        output_root=media,
    )
    assert [s.path.name for s in scan] == ["keep.mp4"]

    named = expand_inputs(
        [media / "old-batch"],
        recursive=True,
        extensions=ACCEPTED,
        accepted=ACCEPTED,
        output_root=media,
    )
    assert [s.path.name for s in named] == ["a.mp4"]


def test_symlinked_directories_are_not_followed(tmp_path):
    _touch(tmp_path / "real" / "a.mp4")
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    out = expand_inputs(
        [tmp_path / "in"],
        recursive=True,
        extensions=ACCEPTED,
        accepted=ACCEPTED,
        output_root=tmp_path / "media",
    )
    assert out == []


def test_include_exclude_and_limit(tmp_path):
    for name in ("alpha.mp4", "beta.mp4", "gamma.mp4"):
        _touch(tmp_path / "in" / name)
    out = expand_inputs(
        [tmp_path / "in"],
        recursive=False,
        extensions=ACCEPTED,
        accepted=ACCEPTED,
        output_root=tmp_path / "media",
        include="a",
        exclude="gamma",
        limit=1,
    )
    assert [s.path.name for s in out] == ["alpha.mp4"]


def test_missing_path_raises(tmp_path):
    with pytest.raises(InputError):
        expand_inputs(
            [tmp_path / "nope"],
            recursive=False,
            extensions=None,
            accepted=ACCEPTED,
            output_root=tmp_path / "media",
        )
