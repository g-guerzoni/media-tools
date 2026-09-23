from pathlib import Path

from media_tools.tasks.ebook import dedup
from media_tools.tasks.ebook.normalize import Verdict


def _entries(rows):
    return {Path(p): Verdict("ok", t, a, lang, "heuristic") for p, t, a, lang in rows}


def _formats(entries):
    return {p: p.suffix.lstrip(".") for p in entries}


def test_the_same_book_in_two_formats_becomes_one_group_and_mobi_wins():
    entries = _entries(
        [
            ("/b/Dom Casmurro - Machado de Assis.epub", "Dom Casmurro", "Machado de Assis", "pt"),
            ("/b/Dom Casmurro - Machado de Assis.mobi", "Dom Casmurro", "Machado de Assis", "pt"),
        ]
    )
    groups = dedup.group(entries, formats=_formats(entries))
    assert len(groups) == 1
    assert groups[0].winner.suffix == ".mobi"
    assert len(groups[0].members) == 2


def test_accents_and_punctuation_do_not_split_a_group():
    entries = _entries(
        [
            (
                "/b/Memorias Postumas - Machado de Assis.epub",
                "Memorias Postumas",
                "Machado de Assis",
                "pt",
            ),
            (
                "/b/Memórias Póstumas - Machado de Assis.mobi",
                "Memórias Póstumas!",
                "Machado de Assis",
                "pt",
            ),
        ]
    )
    assert len(dedup.group(entries, formats=_formats(entries))) == 1


def test_a_translation_is_never_merged_even_by_the_llm():
    entries = _entries(
        [
            ("/b/The Alchemist - Paulo Coelho.epub", "The Alchemist", "Paulo Coelho", "en"),
            ("/b/O Alquimista - Paulo Coelho.epub", "O Alquimista", "Paulo Coelho", "pt"),
        ]
    )
    groups = dedup.group(entries, formats=_formats(entries))
    assert len(groups) == 2

    def greedy_chat(messages, **kwargs):
        from media_tools.integrations.openrouter import Usage

        return {"clusters": [[0, 1]]}, Usage(1, 1)

    refined, _ = dedup.refine(
        groups,
        entries,
        _formats(entries),
        model="m",
        api_key="k",
        preference=dedup.DEFAULT_PREFERENCE,
        chat=greedy_chat,
    )
    assert len(refined) == 2, "different languages must never share a bucket"


def test_series_volumes_stay_apart_in_the_exact_pass():
    entries = _entries(
        [
            ("/b/Goosebumps 2 - R. L. Stine.epub", "Goosebumps 2", "R. L. Stine", "en"),
            ("/b/Goosebumps 3 - R. L. Stine.epub", "Goosebumps 3", "R. L. Stine", "en"),
        ]
    )
    assert len(dedup.group(entries, formats=_formats(entries))) == 2


def test_the_llm_merges_a_near_duplicate_within_one_language():
    entries = _entries(
        [
            ("/b/Cidade de Deus - Paulo Lins.epub", "Cidade de Deus", "Paulo Lins", "pt"),
            ("/b/Cidade de Deus.pdf", "Cidade de Deus", None, "pt"),
        ]
    )
    groups = dedup.group(entries, formats=_formats(entries))
    assert len(groups) == 2

    def chat(messages, **kwargs):
        from media_tools.integrations.openrouter import Usage

        return {"clusters": [[0, 1]]}, Usage(1, 1)

    refined, stats = dedup.refine(
        groups,
        entries,
        _formats(entries),
        model="m",
        api_key="k",
        preference=dedup.DEFAULT_PREFERENCE,
        chat=chat,
    )
    assert len(refined) == 1
    assert refined[0].winner.suffix == ".epub"
    assert stats["llm_calls"] == 1


def test_a_bucket_with_one_entry_costs_no_call():
    entries = _entries([("/b/Solaris - Lem.epub", "Solaris", "Lem", "pl")])
    groups = dedup.group(entries, formats=_formats(entries))
    calls = []

    def chat(messages, **kwargs):
        calls.append(1)
        from media_tools.integrations.openrouter import Usage

        return {"clusters": [[0]]}, Usage(1, 1)

    dedup.refine(
        groups,
        entries,
        _formats(entries),
        model="m",
        api_key="k",
        preference=dedup.DEFAULT_PREFERENCE,
        chat=chat,
    )
    assert calls == []


def test_a_leftover_chunk_of_one_after_splitting_a_big_bucket_costs_no_call():
    # 61 entries sharing a language and a blocking key, one over max_bucket=60:
    # the bucket must be split into a 60-chunk and a 1-chunk, and that trailing
    # lone entry can never cluster with itself, so it must not be sent at all.
    rows = [(f"/b/Solaris - Lem{i}.epub", "Solaris", f"Lem{i}", "pl") for i in range(61)]
    entries = _entries(rows)
    groups = dedup.group(entries, formats=_formats(entries))
    assert len(groups) == 61

    chunk_sizes = []

    def chat(messages, **kwargs):
        from media_tools.integrations.openrouter import Usage

        lines = messages[-1]["content"].splitlines()
        chunk_sizes.append(len(lines))
        return {"clusters": [[i] for i in range(len(lines))]}, Usage(1, 1)

    _refined, stats = dedup.refine(
        groups,
        entries,
        _formats(entries),
        model="m",
        api_key="k",
        preference=dedup.DEFAULT_PREFERENCE,
        chat=chat,
        max_bucket=60,
    )
    assert chunk_sizes == [60], "the lone 61st entry must not cost a request"
    assert stats["llm_calls"] == 1
