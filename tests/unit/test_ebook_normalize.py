import json
from pathlib import Path

import pytest

from media_tools.tasks.ebook import names, normalize
from media_tools.tasks.ebook.metadata import BookFacts


def _facts(name, meta_title=None, meta_author=None, meta_language=None):
    path = Path(f"/books/{name}")
    file_title, file_author = names.parse_filename(path)
    return BookFacts(
        path=path,
        size=10,
        fmt=path.suffix.lstrip("."),
        meta_title=meta_title,
        meta_author=meta_author,
        meta_language=meta_language,
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


def test_heuristic_prefers_the_filename_title_over_a_different_usable_embedded_one():
    # Minor finding: the module docstring and the LLM system prompt both say the
    # filename beats the embedded metadata; the code must actually agree — a
    # usable (non-junk, capitalised) embedded title must not override a filename
    # title that is not itself junk.
    verdict = normalize.heuristic(
        _facts("Cidade de Deus - Paulo Lins.epub", meta_title="A Completely Different Title")
    )
    assert verdict.title == "Cidade de Deus"


def test_a_junk_filename_title_falls_back_to_a_usable_embedded_title():
    # The filename only loses when its OWN title is itself junk (a temp name, a
    # scan artifact) — this is the one case the filename genuinely has nothing to
    # offer, matching the `examples/ebook-list.json` "tmp1603" worked example.
    verdict = normalize.heuristic(_facts("tmp1603.mobi", meta_title="The Blade Itself"))
    assert verdict.title == "The Blade Itself"


# -- RB22: language precedence is --list > LLM > embedded tag > title heuristic > unknown


def test_die_trying_tagged_en_lands_in_en_not_de_via_the_title_heuristic():
    # The reviewer's exact reproduction: "Die Trying" (Lee Child, English) tagged
    # "en" must not be overridden by the title heuristic's "de" guess ("die" is an
    # exclusive German marker) — the tag must be checked BEFORE the heuristic runs.
    facts = _facts("Die Trying - Lee Child.epub", meta_language="en")
    verdict = normalize.heuristic(facts)
    assert verdict.language == "en"
    assert verdict.language_origin == "embedded_tag"


def test_a_book_with_no_tag_still_uses_the_title_heuristic():
    facts = _facts("A Igreja do Diabo - Some Author.epub")
    verdict = normalize.heuristic(facts)
    assert verdict.language == "pt"
    assert verdict.language_origin == "title_heuristic"


def test_a_book_with_no_tag_and_no_decisive_title_is_unknown():
    facts = _facts("Solaris - Stanislaw Lem.epub")
    verdict = normalize.heuristic(facts)
    assert verdict.language is None
    assert verdict.language_origin == "unknown"


@pytest.mark.parametrize("code", ["und", "mul", "zxx"])
def test_placeholder_language_tags_are_rejected_and_the_heuristic_still_runs(code):
    facts = _facts("Die Trying - Lee Child.epub", meta_language=code)
    verdict = normalize.heuristic(facts)
    # the tag is ignored, so the (documented) title-heuristic blind spot applies.
    assert verdict.language == "de"
    assert verdict.language_origin == "title_heuristic"


def test_the_llm_language_beats_a_present_embedded_tag(tmp_path):
    # "Do not hoist the tag above the LLM": when the model DOES answer, its
    # language wins outright over the embedded tag, even though the tag alone
    # would otherwise be trusted ahead of the title heuristic.
    facts = _facts("Die Trying - Lee Child.epub", meta_language="en")

    def fake_chat(messages, *, model, api_key, **kwargs):
        from media_tools.integrations.openrouter import Usage

        return {
            "books": [
                {
                    "i": 0,
                    "status": "ok",
                    "title": "Die Trying",
                    "author": "Lee Child",
                    "language": "de",
                }
            ]
        }, Usage(1, 1)

    verdicts, _ = normalize.classify(
        [facts], model="m", api_key="k", cache_dir=tmp_path, chat=fake_chat
    )
    verdict = verdicts[facts.path]
    assert verdict.language == "de"
    assert verdict.language_origin == "llm"


def test_classify_writes_the_cache_through_fsync_replace(tmp_path, monkeypatch):
    # I7: the shared LLM cache is written from outside any batch's lock — a
    # concurrent run or a Ctrl+C mid-write must not truncate it.
    calls = []

    def spy(temp, target):
        calls.append((temp, target))
        temp.replace(target)

    monkeypatch.setattr(normalize, "fsync_replace", spy)

    def fake_chat(messages, *, model, api_key, **kwargs):
        from media_tools.integrations.openrouter import Usage

        return {
            "books": [{"i": 0, "status": "ok", "title": "T", "author": "A", "language": "en"}]
        }, Usage(1, 1)

    facts = [_facts("Book - Author.epub")]
    normalize.classify(facts, model="m", api_key="k", cache_dir=tmp_path, chat=fake_chat)

    assert calls, "the cache write must go through fsync_replace, not a bare write_text"
    temp, target = calls[0]
    assert target == tmp_path / normalize.CACHE_FILENAME
    assert temp.name.startswith(".") and temp.name.endswith(".partial")


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


def test_a_malformed_reply_shape_falls_back_to_the_heuristic(tmp_path):
    # Valid JSON, wrong shape: a bare list instead of the expected object. This must
    # be treated exactly like a failed request, not crash the whole classify() call.
    def bare_list_chat(messages, **kwargs):
        from media_tools.integrations.openrouter import Usage

        return [1, 2, 3], Usage(1, 1)

    facts = [_facts("Cidade de Deus - Paulo Lins.epub")]
    verdicts, stats = normalize.classify(
        facts, model="m", api_key="k", cache_dir=tmp_path, chat=bare_list_chat
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
