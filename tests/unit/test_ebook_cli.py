import json
import subprocess
import sys


def _cli(*args, **kwargs):
    return subprocess.run(
        [sys.executable, "-m", "media_tools", *args], capture_output=True, text=True, **kwargs
    )


def test_build_without_sources_exits_2():
    assert _cli("ebook", "build", "--json").returncode == 2


def test_missing_key_without_no_llm_exits_3(tmp_path, monkeypatch):
    books = tmp_path / "books"
    books.mkdir()
    (books / "A Book - An Author.epub").write_bytes(b"x")
    env = {"PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "media_tools",
            "ebook",
            "build",
            str(books),
            "-o",
            str(tmp_path / "media"),
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 3
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(e.get("code") == "config_missing" for e in events)
    assert any("--no-llm" in (e.get("hint") or "") for e in events)


def test_no_llm_needs_no_key(tmp_path):
    books = tmp_path / "books"
    books.mkdir()
    (books / "A Book - An Author.epub").write_bytes(b"not a real epub")
    result = _cli(
        "ebook",
        "build",
        str(books),
        "--no-llm",
        "--dry-run",
        "-o",
        str(tmp_path / "media"),
        "--json",
    )
    assert result.returncode in (0, 1)
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert events[-1]["type"] == "result"
    assert not (tmp_path / "media").exists()


def test_list_file_shapes_are_accepted(tmp_path):
    from media_tools.tasks.ebook.build import load_list

    listing = tmp_path / "list.json"
    listing.write_text(
        json.dumps(
            [
                "/books/one.epub",
                {"path": "/books/two.epub", "title": "Two", "author": "A", "language": "en"},
            ]
        )
    )
    entries = load_list(listing)
    assert entries[0].path.name == "one.epub" and entries[0].title is None
    assert entries[1].title == "Two" and entries[1].language == "en"
