"""Drop duplicate books before conversion.

The old script converted every format of a book it found, then discarded the
duplicates it had just spent time producing. This stage groups the same book's
formats together first, so only the winning format is ever converted.

Two passes:

- `group` — exact pass. Books with the same language and the same normalised
  title+author collapse into one `Group`, no LLM involved.
- `refine` — LLM pass. Exact groups are bucketed by `(language, blocking_key)` and,
  within each bucket with more than one candidate, the model is asked which ones are
  really the same book (a typo, a subtitle, a missing author). Buckets are always
  split by language first, so a translation is never even shown to the model
  alongside the original — that pairing must never be merged, no matter what the
  model would say if asked.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from media_tools.integrations import openrouter
from media_tools.tasks.ebook.normalize import Verdict

DEFAULT_PREFERENCE = ("mobi", "azw", "prc", "epub", "pdf")

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")

# Leading articles only, across the languages this library holds — dropped so
# "The Alchemist" and "El Alquimista" block on their real first word, not "the"/"el".
_ARTICLES = frozenset(
    {
        "a",
        "an",
        "the",
        "o",
        "os",
        "as",
        "um",
        "uma",
        "el",
        "los",
        "las",
        "una",
        "unos",
        "unas",
        "il",
        "lo",
        "gli",
        "der",
        "die",
        "das",
        "les",
        "du",
        "des",
    }
)

_MERGE_SYSTEM_PROMPT = """You are matching duplicate books in a personal library.
Every entry below is already the same language and shares its first title words,
but that does not make them the same book — a different volume, a different
author, or a loose retelling must stay apart.

Group indices together ONLY when they are the exact same book (same title, same
author, same volume/edition) allowing for typos, punctuation, or a missing author.
Never merge different volumes of a series, different authors, or different books
that merely share a title word. When unsure, leave entries apart.

Reply ONLY with JSON: {"clusters": [[0, 2], [1]]}. Every index from 0 to N-1
appears in exactly one cluster; a book with no duplicate is its own
single-element cluster."""


@dataclass
class Group:
    members: list[Path]
    winner: Path
    language: str | None


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _SPACE.sub(" ", _PUNCT.sub(" ", stripped)).strip()


def normalise_key(title: str, author: str | None) -> str:
    """A key that ignores accents, case and punctuation, so the same book
    surfaces as a duplicate even when one copy's title has a typo, a trailing
    exclamation mark, or is missing its diacritics."""
    return f"{_fold(title)}|{_fold(author or '')}"


def blocking_key(title: str, tokens: int = 2) -> str:
    """The title's first significant words, for bucketing candidates before the
    (more expensive, and riskier) LLM pass — leading articles are dropped so
    "The Alchemist" blocks on "alchemist", not "the"."""
    words = _fold(title).split()
    start = 0
    while start < len(words) - 1 and words[start] in _ARTICLES:
        start += 1
    significant = words[start:] or words
    return " ".join(significant[:tokens])


def _pick_winner(members: list[Path], formats: dict[Path, str], preference) -> Path:
    def rank(path: Path) -> tuple[int, str]:
        fmt = (formats.get(path) or "").lower()
        try:
            index = preference.index(fmt)
        except ValueError:
            index = len(preference)
        return (index, str(path))

    return min(members, key=rank)


def group(
    entries: dict[Path, Verdict],
    *,
    preference: tuple[str, ...] = DEFAULT_PREFERENCE,
    formats: dict[Path, str],
) -> list[Group]:
    """The exact pass: same language, same normalised title+author, one group.
    Only an "ok" verdict takes part in bucketing — an "unidentified"/"invalid"/
    "irrelevant" book (typically an empty title, no author) still becomes its own
    single-member group, so the pipeline can route it to a review folder, but must
    never be treated as a duplicate of another such book just because both are
    equally blank. The same guard applies to any book, "ok" or not, whose
    normalised key has no usable title or author at all. The winner is whichever
    member's format comes first in `preference`; every other member is a duplicate
    and is never converted."""
    buckets: dict[tuple[str | None, str], list[Path]] = {}
    solo: list[Path] = []
    for path, verdict in entries.items():
        key_str = normalise_key(verdict.title, verdict.author)
        if verdict.status != "ok" or not key_str.replace("|", ""):
            solo.append(path)
            continue
        key = (verdict.language, key_str)
        buckets.setdefault(key, []).append(path)

    groups = [
        Group(
            members=members,
            winner=_pick_winner(members, formats, preference),
            language=language,
        )
        for (language, _key), members in buckets.items()
    ]
    groups.extend(
        Group(members=[path], winner=path, language=entries[path].language) for path in solo
    )
    return groups


def _bucket_key(entry: Group, entries: dict[Path, Verdict]) -> tuple[str | None, str]:
    return (entry.language, blocking_key(entries[entry.winner].title))


def _ask_clusters(
    chunk: list[Group], entries: dict[Path, Verdict], model: str, api_key: str, chat
) -> tuple[list[list[int]], openrouter.Usage]:
    lines = []
    for index, candidate in enumerate(chunk):
        verdict = entries[candidate.winner]
        lines.append(f"{index}: {verdict.title} | {verdict.author or ''}")
    messages = [
        {"role": "system", "content": _MERGE_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]
    payload, usage = chat(messages, model=model, api_key=api_key)
    if not isinstance(payload, dict):
        # Valid JSON, wrong shape (e.g. a bare list) — treat exactly like a failed
        # request rather than let an AttributeError from `.get` below escape and
        # abort the whole refine() call.
        raise openrouter.OpenRouterError("OpenRouter returned an unexpected response shape")

    # Sanitise each cluster (drop invented/out-of-range/internally-repeated indices)
    # before deciding which indices are ambiguous — an index claimed by more than
    # one cluster must be dropped from ALL of them, not just kept in whichever one
    # happened to be processed first.
    sanitised: list[list[int]] = []
    counts: dict[int, int] = {}
    for raw_cluster in payload.get("clusters") or []:
        if not isinstance(raw_cluster, list):
            continue
        seen_in_cluster: set[int] = set()
        valid = []
        for index in raw_cluster:
            if not isinstance(index, int) or isinstance(index, bool):
                continue  # an invented, non-integer index
            if not (0 <= index < len(chunk)):
                continue  # out of range for this chunk
            if index in seen_in_cluster:
                continue  # repeated within the same cluster
            seen_in_cluster.add(index)
            valid.append(index)
        if valid:
            sanitised.append(valid)
            for index in valid:
                counts[index] = counts.get(index, 0) + 1

    ambiguous = {index for index, count in counts.items() if count > 1}
    clusters = [[index for index in valid if index not in ambiguous] for valid in sanitised]
    return [cluster for cluster in clusters if len(cluster) > 1], usage


def _merge_cluster(
    cluster: list[int],
    chunk: list[Group],
    formats: dict[Path, str],
    preference: tuple[str, ...],
) -> Group | None:
    """Merge one model-approved cluster of chunk indices into a single Group, or
    refuse (returning None) if it spans more than one language. Bucketing already
    keeps different languages out of the same chunk — this is the second, closer
    line of defense for that rule: it does not trust the bucketing alone, and it
    does not trust the model's cluster either, whatever it says to merge."""
    languages = {chunk[index].language for index in cluster}
    if len(languages) > 1:
        return None
    members: list[Path] = []
    for index in cluster:
        members.extend(chunk[index].members)
    return Group(
        members=members,
        winner=_pick_winner(members, formats, preference),
        language=next(iter(languages)),
    )


def refine(
    groups: list[Group],
    entries: dict[Path, Verdict],
    formats: dict[Path, str],
    *,
    model: str,
    api_key: str,
    preference: tuple[str, ...] = DEFAULT_PREFERENCE,
    chat=None,
    max_bucket: int = 60,
) -> tuple[list[Group], dict]:
    """The LLM pass: bucket the exact groups by `(language, blocking_key)` — never
    across languages — and ask the model which ones, within one bucket, are really
    the same book. A bucket with fewer than two groups costs no request at all. A
    batch whose request fails or comes back in an unexpected shape is left ungrouped
    rather than dropped, and counted in `stats["errors"]` — dedup calls cost money
    too, and the run summary is the only warning before a large bill. The model's
    clusters are trusted only where their indices are well-formed, and a cluster
    spanning more than one language is never merged, whatever the model said
    (`stats["blocked_merges"]`) — `_merge_cluster` is the second line of defense for
    that rule, independent of the bucketing that should already keep languages apart."""
    chat = chat or openrouter.chat

    buckets: dict[tuple[str | None, str], list[Group]] = {}
    for candidate in groups:
        buckets.setdefault(_bucket_key(candidate, entries), []).append(candidate)

    stats = {"llm_calls": 0, "buckets": 0, "merges": 0, "errors": 0, "blocked_merges": 0}
    result: list[Group] = []

    for bucket_groups in buckets.values():
        if len(bucket_groups) < 2:
            result.extend(bucket_groups)
            continue

        stats["buckets"] += 1
        chunks = [
            bucket_groups[i : i + max_bucket] for i in range(0, len(bucket_groups), max_bucket)
        ]
        for chunk in chunks:
            if len(chunk) < 2:
                # A tail chunk left over from splitting a large bucket at max_bucket
                # can end up alone — one entry can never cluster with itself.
                result.extend(chunk)
                continue
            try:
                clusters, _usage = _ask_clusters(chunk, entries, model, api_key, chat)
                stats["llm_calls"] += 1
            except openrouter.OpenRouterError:
                stats["errors"] += 1
                result.extend(chunk)
                continue

            merged_indices: set[int] = set()
            for cluster in clusters:
                merged = _merge_cluster(cluster, chunk, formats, preference)
                if merged is None:
                    stats["blocked_merges"] += 1
                    continue
                result.append(merged)
                merged_indices.update(cluster)
                stats["merges"] += len(cluster) - 1
            for index, candidate in enumerate(chunk):
                if index not in merged_indices:
                    result.append(candidate)

    return result, stats
