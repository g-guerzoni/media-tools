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


def test_a_malformed_reply_shape_is_treated_as_a_failed_request():
    # Valid JSON, wrong shape: a bare list instead of the expected object. This must
    # be treated exactly like a failed request — count it, and leave the groups
    # unmerged rather than crash refine() entirely.
    entries = _entries(
        [
            ("/b/Cidade de Deus - Paulo Lins.epub", "Cidade de Deus", "Paulo Lins", "pt"),
            ("/b/Cidade de Deus.pdf", "Cidade de Deus", None, "pt"),
        ]
    )
    groups = dedup.group(entries, formats=_formats(entries))

    def bare_list_chat(messages, **kwargs):
        from media_tools.integrations.openrouter import Usage

        return [1, 2, 3], Usage(1, 1)

    refined, stats = dedup.refine(
        groups,
        entries,
        _formats(entries),
        model="m",
        api_key="k",
        preference=dedup.DEFAULT_PREFERENCE,
        chat=bare_list_chat,
    )
    assert len(refined) == 2
    assert stats["errors"] == 1


def test_repeated_indices_across_clusters_are_dropped_from_all_of_them():
    entries = _entries(
        [
            ("/b/Solaris A - Lem.epub", "Solaris", "Lem A", "pl"),
            ("/b/Solaris B - Lem.epub", "Solaris", "Lem B", "pl"),
            ("/b/Solaris C - Lem.epub", "Solaris", "Lem C", "pl"),
        ]
    )
    groups = dedup.group(entries, formats=_formats(entries))
    assert len(groups) == 3

    def chat(messages, **kwargs):
        from media_tools.integrations.openrouter import Usage

        # Index 1 is claimed by both clusters: ambiguous, must be trusted by neither.
        return {"clusters": [[0, 1], [1, 2]]}, Usage(1, 1)

    refined, _ = dedup.refine(
        groups,
        entries,
        _formats(entries),
        model="m",
        api_key="k",
        preference=dedup.DEFAULT_PREFERENCE,
        chat=chat,
    )
    assert len(refined) == 3, "an index claimed by two clusters must be dropped from both"
    book_b = Path("/b/Solaris B - Lem.epub")
    for grp in refined:
        assert len(grp.members) == 1
        if book_b in grp.members:
            assert grp.members == [book_b], "book 1 must end up alone"


def test_merge_cluster_refuses_to_merge_across_languages_even_when_asked():
    # A second line of defense, independent of bucketing: forcing a mixed-language
    # chunk straight into the merge step must still refuse to merge it.
    en_group = dedup.Group(members=[Path("/b/en.epub")], winner=Path("/b/en.epub"), language="en")
    pt_group = dedup.Group(members=[Path("/b/pt.epub")], winner=Path("/b/pt.epub"), language="pt")
    formats = {Path("/b/en.epub"): "epub", Path("/b/pt.epub"): "epub"}

    merged = dedup._merge_cluster([0, 1], [en_group, pt_group], formats, dedup.DEFAULT_PREFERENCE)

    assert merged is None, "a cluster spanning two languages must never be merged"


def test_two_different_invalid_books_do_not_become_duplicates_of_each_other():
    entries = {
        Path("/b/scan0001.pdf"): Verdict("invalid", "", None, None, "heuristic"),
        Path("/b/scan0002.pdf"): Verdict("invalid", "", None, None, "heuristic"),
    }
    groups = dedup.group(entries, formats=_formats(entries))
    assert len(groups) == 2


def test_refine_never_calls_the_model_for_books_that_did_not_qualify_for_exact_grouping():
    # The exact scenario from the finding: two different blank/invalid books both
    # land in the (None, "") bucket unless refine() excludes them the same way
    # group() does — otherwise the model is asked to compare "scan0001" against
    # "scan0002" and has every reason to hallucinate a match.
    entries = {
        Path("/b/scan0001.pdf"): Verdict("invalid", "", None, None, "heuristic"),
        Path("/b/scan0002.pdf"): Verdict("invalid", "", None, None, "heuristic"),
    }
    groups = dedup.group(entries, formats=_formats(entries))
    assert len(groups) == 2

    calls = []

    def chat(messages, **kwargs):
        calls.append(1)
        from media_tools.integrations.openrouter import Usage

        return {"clusters": [[0, 1]]}, Usage(1, 1)

    refined, _stats = dedup.refine(
        groups,
        entries,
        _formats(entries),
        model="m",
        api_key="k",
        preference=dedup.DEFAULT_PREFERENCE,
        chat=chat,
    )
    assert len(refined) == 2
    assert calls == [], "an ineligible pair must never even be sent to the model"


def test_the_exclusion_does_not_swallow_a_genuine_duplicate():
    # Guard against the fix above being too broad: a real same-language duplicate
    # (both "ok", both a usable title) must still merge through refine() as before.
    entries = _entries(
        [
            ("/b/Solaris - Lem.epub", "Solaris", "Lem", "pl"),
            ("/b/Solaris.pdf", "Solaris", None, "pl"),
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
    assert stats["llm_calls"] == 1
