from pathlib import Path

import pytest

from media_tools.core.paths import (
    BatchNameError,
    batch_hash,
    mirror_output,
    output_root,
    sanitize_batch,
    temp_path,
    truncate_name,
)


def test_output_root_prefers_cli_then_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_TOOLS_OUT", str(tmp_path / "from-env"))
    assert output_root(tmp_path / "from-cli") == tmp_path / "from-cli"
    assert output_root(None) == tmp_path / "from-env"


def test_output_root_falls_back_to_cwd_media(tmp_path, monkeypatch):
    monkeypatch.delenv("MEDIA_TOOLS_OUT", raising=False)
    monkeypatch.setattr("media_tools.core.paths._checkout_root", lambda: None)
    monkeypatch.chdir(tmp_path)
    assert output_root(None) == tmp_path / "media"


def test_output_root_uses_checkout_root_when_detected(tmp_path, monkeypatch):
    fake_repo = tmp_path / "repo"
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    monkeypatch.delenv("MEDIA_TOOLS_OUT", raising=False)
    monkeypatch.setattr("media_tools.core.paths._checkout_root", lambda: fake_repo)
    monkeypatch.chdir(other_dir)
    assert output_root(None) == fake_repo / "media"


def test_sanitize_batch():
    assert sanitize_batch("Aula 01") == "Aula-01"
    assert sanitize_batch("  many   spaces  ") == "many-spaces"
    assert sanitize_batch("Ciência/2026") == "Ciência2026"
    assert len(sanitize_batch("x" * 200)) == 80


@pytest.mark.parametrize("name", ["", "   ", "///", ".cache", "_kindle", ".hidden", "_x"])
def test_sanitize_batch_rejects_empty_and_reserved(name):
    with pytest.raises(BatchNameError):
        sanitize_batch(name)


def test_batch_hash_is_stable_and_option_sensitive(tmp_path):
    a = batch_hash(
        task="compress",
        options={"crf": 28},
        selection={"recursive": False},
        inputs=[tmp_path / "in"],
    )
    b = batch_hash(
        task="compress",
        options={"crf": 28},
        selection={"recursive": False},
        inputs=[tmp_path / "in"],
    )
    c = batch_hash(
        task="compress",
        options={"crf": 23},
        selection={"recursive": False},
        inputs=[tmp_path / "in"],
    )
    d = batch_hash(
        task="compress",
        options={"crf": 28},
        selection={"recursive": True},
        inputs=[tmp_path / "in"],
    )
    assert a == b
    assert len(a) == 8
    assert a != c and a != d


def test_batch_hash_ignores_key_order():
    one = batch_hash(task="t", options={"a": 1, "b": 2}, selection={}, inputs=[])
    two = batch_hash(task="t", options={"b": 2, "a": 1}, selection={}, inputs=[])
    assert one == two


def test_batch_hash_canonical_serialization():
    # Pins canonical serialisation; change only deliberately.
    digest = batch_hash(
        task="compress",
        options={"crf": 28},
        selection={"recursive": False},
        inputs=[Path("/tmp/in")],
    )
    assert digest == "c9351634"


def test_mirror_output_keeps_subfolders(tmp_path):
    src = tmp_path / "in" / "sub" / "clip.mov"
    out = mirror_output(src, tmp_path / "in", tmp_path / "batch", "clip.mp4")
    assert out == tmp_path / "batch" / "sub" / "clip.mp4"


def test_mirror_output_flattens_named_files(tmp_path):
    src = tmp_path / "elsewhere" / "clip.mov"
    out = mirror_output(src, None, tmp_path / "batch", "clip.mp4")
    assert out == tmp_path / "batch" / "clip.mp4"


def test_temp_path_keeps_extension_before_partial(tmp_path):
    assert temp_path(tmp_path / "a.mp4").name == ".a.mp4.partial"


def test_truncate_name_preserves_extension_and_marks_hash():
    out = truncate_name("x" * 300 + ".azw3", 60)
    assert out.endswith(".azw3")
    assert len(out) <= 60
    assert truncate_name("short.azw3", 60) == "short.azw3"
