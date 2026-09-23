import json
from pathlib import Path

from media_tools.tasks.ebook import names, normalize
from media_tools.tasks.ebook.metadata import BookFacts


def _facts(name, meta_title=None, meta_author=None):
    path = Path(f"/books/{name}")
    file_title, file_author = names.parse_filename(path)
    return BookFacts(
        path=path,
        size=10,
        fmt=path.suffix.lstrip("."),
        meta_title=meta_title,
        meta_author=meta_author,
        meta_language=None,
        meta_uuid=None,
        has_cover=False,
        file_title=file_title,
        file_author=file_author,
    )


def test_heuristic_prefers_the_filename_author_over_a_junk_embedded_one():
    verdict = normalize.heuristic(
        _facts(
            "Cidade de Deus - Paulo Lins.epub",
            meta_title="cidade de deus2",
            meta_author="Administrador",
        )
    )
    assert verdict.title == "Cidade de Deus"
    assert verdict.author == "Paulo Lins"
    # Not "pt": language.detect scores only markers exclusive to one language (see
    # tasks/ebook/language.py), and "Cidade de Deus" carries none — the same reason
    # "Memórias Póstumas de Brás Cubas" comes back None in test_ebook_language.py.
    # The offline heuristic has no LLM to lean on, so None is the honest answer here.
    assert verdict.language is None
    assert verdict.source == "heuristic"


def test_llm_verdicts_are_used_and_cached(tmp_path):
    facts = [_facts("paulo lins - cidade de deus.epub")]
    calls = []

    def fake_chat(messages, *, model, api_key, **kwargs):
        calls.append(messages)
        payload = {
            "books": [
                {
                    "i": 0,
                    "status": "ok",
                    "title": "Cidade de Deus",
                    "author": "Paulo Lins",
                    "language": "pt",
                }
            ]
        }
        from media_tools.integrations.openrouter import Usage

        return payload, Usage(10, 5)

    first, stats = normalize.classify(
        facts, model="m", api_key="k", cache_dir=tmp_path, chat=fake_chat
    )
    assert first[facts[0].path].title == "Cidade de Deus"
    assert first[facts[0].path].source == "llm"
    assert stats["llm_calls"] == 1 and stats["prompt_tokens"] == 10

    second, second_stats = normalize.classify(
        facts, model="m", api_key="k", cache_dir=tmp_path, chat=fake_chat
    )
    assert len(calls) == 1, "the second run must be served from the cache"
    assert second[facts[0].path].source == "cache"
    assert second_stats["llm_calls"] == 0 and second_stats["cache_hits"] == 1
    assert json.loads((tmp_path / "ebook-llm.json").read_text())


def test_the_cache_key_ignores_the_folder(tmp_path):
    def fake_chat(messages, *, model, api_key, **kwargs):
        from media_tools.integrations.openrouter import Usage

        return {
            "books": [{"i": 0, "status": "ok", "title": "T", "author": "A", "language": "en"}]
        }, Usage(1, 1)

    one = _facts("Book - Author.epub")
    moved = BookFacts(**{**one.__dict__, "path": Path("/elsewhere/Book - Author.epub")})
    normalize.classify([one], model="m", api_key="k", cache_dir=tmp_path, chat=fake_chat)
    result, _ = normalize.classify(
        [moved], model="m", api_key="k", cache_dir=tmp_path, chat=fake_chat
    )
    assert result[moved.path].source == "cache"


def test_a_failing_batch_falls_back_to_the_heuristic(tmp_path):
    from media_tools.integrations.openrouter import OpenRouterError

    def angry_chat(messages, **kwargs):
        raise OpenRouterError("502")

    facts = [_facts("Cidade de Deus - Paulo Lins.epub")]
    verdicts, stats = normalize.classify(
        facts, model="m", api_key="k", cache_dir=tmp_path, chat=angry_chat
    )
    assert stats["errors"] == 1
    assert verdicts[facts[0].path].source == "heuristic"
    assert verdicts[facts[0].path].title == "Cidade de Deus"


def test_statuses_other_than_ok_are_carried_through(tmp_path):
    def fake_chat(messages, **kwargs):
        from media_tools.integrations.openrouter import Usage

        return {
            "books": [{"i": 0, "status": "invalid", "title": "", "author": "", "language": ""}]
        }, Usage(1, 1)

    facts = [_facts("scan0001.pdf")]
    verdicts, _ = normalize.classify(
        facts, model="m", api_key="k", cache_dir=tmp_path, chat=fake_chat
    )
    assert verdicts[facts[0].path].status == "invalid"
