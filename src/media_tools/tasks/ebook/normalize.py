"""Clean a book's title, author and language before conversion.

This library's filenames are curated; its embedded metadata frequently is not — a
URL, a temp name like `tmp1603`, or the name of whoever packaged the file instead of
the author. `heuristic` is the offline fallback (no LLM, no network): it trusts the
filename over the embedded metadata wherever the two disagree. `classify` is the LLM
path: it batches unclassified books, asks the model to clean them up, and caches every
answer so re-running a build never re-pays for a book it already classified.

The cache key (`signature`) is deliberately independent of the file's path — it is
built from the filename plus the embedded title/author plus the model plus the prompt
version — so moving or reorganising the library on disk does not invalidate it.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from media_tools.integrations import openrouter
from media_tools.tasks.ebook import language as lang
from media_tools.tasks.ebook import names
from media_tools.tasks.ebook.metadata import BookFacts

CACHE_FILENAME = "ebook-llm.json"
PROMPT_VERSION = 1

_STATUSES = frozenset({"ok", "unidentified", "invalid", "irrelevant"})

SYSTEM_PROMPT = """You clean up book records for a personal library.

For each entry you get the SOURCE FILENAME and the title/author embedded in the file.

MOST IMPORTANT RULE: the filename is the most reliable source. Embedded metadata in
this collection is frequently junk — a URL, a temporary name like 'tmp1603', a single
stray letter, or the name of whoever packaged the file instead of the author. Ignore
the embedded values whenever they disagree with the filename.

Filenames follow one of two conventions: 'Title - Author' or 'Lastname, First - Title'.

Return status 'ok' whenever you can form a plausible title, even for a book you do not
recognise. Give 'title' and 'author' with correct capitalisation in the book's own
language, in the right order, using what you know about the book to fix them (for
example 'paulo lins - cidade de deus' -> title 'Cidade de Deus', author 'Paulo Lins').
Unknown author: status 'ok' with an empty author.

Also return 'language' as a two-letter ISO 639-1 code, inferred from the TITLE.

These three statuses are RARE — use them only when neither the filename nor the
metadata yields any plausible title:
- 'unidentified': no clue at all ('documento1.pdf', 'scan0001').
- 'invalid': clearly not a book (a dump of a URL, random bytes).
- 'irrelevant': clearly not reading material (a config file, a loose flyer).

When in doubt use 'ok' with your best reading of the filename.

Reply ONLY with JSON: {"books":[{"i":0,"status":"ok","title":"...","author":"...",
"language":"pt"}]}. Every index appears exactly once."""


@dataclass(frozen=True)
class Verdict:
    status: str
    title: str
    author: str | None
    language: str | None
    source: str


def signature(facts: BookFacts, model: str) -> str:
    """A path-independent cache key: moving the library must not invalidate it."""
    payload = "|".join(
        [
            facts.path.name,
            facts.meta_title or "",
            facts.meta_author or "",
            model,
            str(PROMPT_VERSION),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _usable_meta_title(text: str | None) -> bool:
    """A meta title only beats the filename when it looks like an actual title:
    not flagged by `names.is_junk_title`, and carrying at least one capital letter
    the way a real title page does. This library's junk embedded titles are
    routinely a lowercase slug — a packager's filename, a URL fragment — that
    `is_junk_title` alone does not catch."""
    if not text or names.is_junk_title(text):
        return False
    return any(c.isupper() for c in text)


def heuristic(facts: BookFacts) -> Verdict:
    """The offline path: title from a usable embedded title else the filename;
    author from the filename first, since this library's filenames are more
    reliable than its embedded authors; language from `language.detect`."""
    title = facts.meta_title.strip() if _usable_meta_title(facts.meta_title) else facts.file_title

    author: str | None
    if facts.file_author:
        author = facts.file_author
    elif facts.meta_author and not names.is_junk_author(facts.meta_author):
        author = facts.meta_author.strip()
    else:
        author = None

    return Verdict(
        status="ok",
        title=title,
        author=author,
        language=lang.detect(title, author),
        source="heuristic",
    )


def _load_cache(cache_file: Path | None) -> dict[str, dict]:
    if cache_file is None or not cache_file.is_file():
        return {}
    try:
        loaded = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save_cache(cache_file: Path | None, cache: dict) -> None:
    if cache_file is None:
        return
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def _verdict_from(answer: dict, *, source: str, facts: BookFacts) -> Verdict:
    """Normalise one book's answer from the model, falling back to the offline
    heuristic for any field the model left blank or gave nonsense for."""
    fallback = heuristic(facts)

    status = answer.get("status")
    if status not in _STATUSES:
        status = "ok"

    raw_title = answer.get("title")
    has_title = isinstance(raw_title, str) and raw_title.strip()
    title = raw_title.strip() if has_title else fallback.title

    author: str | None
    raw_author = answer.get("author")
    if raw_author is None:
        author = fallback.author
    elif isinstance(raw_author, str) and raw_author.strip():
        author = raw_author.strip()
    else:
        author = None

    language = None
    raw_language = answer.get("language")
    if isinstance(raw_language, str):
        code = raw_language.strip().lower()
        if len(code) == 2 and code.isalpha():
            language = code
    if language is None:
        language = fallback.language

    return Verdict(status=status, title=title, author=author, language=language, source=source)


def _ask(
    batch: list[BookFacts], model: str, api_key: str, chat
) -> tuple[dict[int, dict], openrouter.Usage]:
    lines = [
        f"{index}: {facts.path.name} | {facts.meta_title or ''} | {facts.meta_author or ''}"
        for index, facts in enumerate(batch)
    ]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]
    payload, usage = chat(messages, model=model, api_key=api_key)

    answers: dict[int, dict] = {}
    seen: set[int] = set()
    for entry in payload.get("books") or []:
        if not isinstance(entry, dict):
            continue
        index = entry.get("i")
        if not isinstance(index, int) or isinstance(index, bool) or not (0 <= index < len(batch)):
            continue  # an invented or out-of-range index: drop it
        if index in seen:
            answers.pop(index, None)  # a repeated index is ambiguous: trust neither copy
            continue
        seen.add(index)
        answers[index] = entry
    return answers, usage


def classify(
    facts_list: list[BookFacts],
    *,
    model: str,
    api_key: str,
    cache_dir: Path | None,
    workers: int = 4,
    batch_size: int = 30,
    chat=None,
    on_progress=None,
) -> tuple[dict[Path, Verdict], dict]:
    """Classify every book, cache first. Books already in the cache cost nothing;
    the rest are sent to the model in batches of `batch_size`, `workers` batches in
    flight at once. The cache is written after every batch completes, so an
    interrupted run keeps the answers it already paid for. A batch whose request
    fails falls back to `heuristic` for its books and counts one error — a failed
    request never loses a book."""
    chat = chat or openrouter.chat
    cache_file = Path(cache_dir) / CACHE_FILENAME if cache_dir else None
    cache = _load_cache(cache_file)

    verdicts: dict[Path, Verdict] = {}
    pending: list[BookFacts] = []
    for facts in facts_list:
        cached = cache.get(signature(facts, model))
        if isinstance(cached, dict):
            verdicts[facts.path] = _verdict_from(cached, source="cache", facts=facts)
        else:
            pending.append(facts)

    batches = [pending[i : i + batch_size] for i in range(0, len(pending), batch_size)]
    stats = {
        "llm_calls": 0,
        "cache_hits": len(verdicts),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "errors": 0,
    }

    if not batches:
        return verdicts, stats

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_ask, batch, model, api_key, chat): batch for batch in batches}
        for done, future in enumerate(as_completed(futures), start=1):
            batch = futures[future]
            try:
                answers, usage = future.result()
                stats["llm_calls"] += 1
                stats["prompt_tokens"] += usage.prompt_tokens
                stats["completion_tokens"] += usage.completion_tokens
            except openrouter.OpenRouterError:
                stats["errors"] += 1
                answers = {}

            for index, facts in enumerate(batch):
                answer = answers.get(index)
                if answer:
                    verdict = _verdict_from(answer, source="llm", facts=facts)
                    cache[signature(facts, model)] = answer
                else:
                    verdict = heuristic(facts)
                verdicts[facts.path] = verdict

            _save_cache(cache_file, cache)  # incremental: an interrupted run keeps what it paid for
            if on_progress:
                on_progress(done, len(batches))

    return verdicts, stats
