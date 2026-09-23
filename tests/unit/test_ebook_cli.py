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


def test_interrupt_during_planning_still_emits_start_and_a_130_result(
    tmp_path, monkeypatch, capsys
):
    """`_build_plan` (metadata reads, the LLM passes, cover resolution) runs before
    `RunState.open()` and used to run before `reporter.start()` too — an interrupt
    there produced no stdout at all: no `start`, no `result`. `read_all` is
    monkeypatched to raise immediately so this test does not depend on Calibre being
    installed or on timing a real Ctrl+C against a real metadata read."""
    from media_tools.cli import build_parser
    from media_tools.tasks.ebook import build
    from media_tools.tasks.ebook import metadata as metadata_stage

    books = tmp_path / "books"
    books.mkdir()
    (books / "A Book - An Author.epub").write_bytes(b"x")

    def _raise(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(metadata_stage, "read_all", _raise)

    args = build_parser().parse_args(
        ["ebook", "build", str(books), "--dry-run", "-o", str(tmp_path / "media"), "--json"]
    )
    exit_code = build.run(args)
    assert exit_code == 130

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert events[0]["type"] == "start"
    assert events[-1]["type"] == "result"
    assert events[-1]["exit_code"] == 130
    assert not (tmp_path / "media").exists()
