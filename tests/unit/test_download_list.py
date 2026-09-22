import json

import pytest

from media_tools.tasks.common import UsageError
from media_tools.tasks.download.ytdlp import Entry, derive_name, load_list


def _write(tmp_path, payload):
    path = tmp_path / "list.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_plain_array(tmp_path):
    path = _write(tmp_path, ["https://a/1", "https://a/2"])
    assert load_list(path) == [Entry("https://a/1", None), Entry("https://a/2", None)]


def test_objects_with_names(tmp_path):
    path = _write(tmp_path, [{"url": "https://a/1", "name": "lesson-01"}, "https://a/2"])
    assert load_list(path) == [Entry("https://a/1", "lesson-01"), Entry("https://a/2", None)]


def test_urls_key(tmp_path):
    path = _write(tmp_path, {"urls": ["https://a/1"]})
    assert load_list(path) == [Entry("https://a/1", None)]


def test_invalid_shape_raises(tmp_path):
    path = _write(tmp_path, {"videos": ["https://a/1"]})
    with pytest.raises(UsageError):
        load_list(path)


def test_derive_name_prefers_explicit_then_title_then_url():
    assert derive_name("https://h/v", {"title": "Real"}, "chosen") == "chosen"
    assert derive_name("https://h/v", {"title": "Real Title"}, None) == "Real Title"
    uuid_url = "https://h/vod/7b7ecd01-2abd-df32-430a-1394bcb6d53a/playlist.m3u8?sjwt=x"
    assert (
        derive_name(uuid_url, {"title": "playlist"}, None) == "7b7ecd01-2abd-df32-430a-1394bcb6d53a"
    )
    plain = derive_name("https://h/stream/file.m3u8?token=abc", {"title": "file"}, None)
    assert len(plain) == 12 and plain.isalnum()
