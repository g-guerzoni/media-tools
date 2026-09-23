# media-tools — agent guide

This file is for an agent driving or modifying `media-tools`: a CLI that compresses,
converts, splits, downloads and organises media and ebooks. It has three parts:
start-of-session checks, how to drive the tool as a black box, and how to change its
code.

`compress`, `convert`, `split`, `download`, `ebook`, `formats`, `status` and `doctor`
all work today. `ebook build <folder>` turns a folder of mixed ebook formats into a
language-sorted AZW3 (or other format) library, deduplicated and (optionally)
LLM-cleaned — see "The `ebook` task" below for the full contract before you drive it.

## 1. Session start

Run this once before doing anything else in a session that will use the tool:

```bash
media-tools doctor --json --check-updates
```

(This repo's own `.claude/settings.json` already runs the text form,
`media-tools doctor --quiet --check-updates`, automatically at session start — this is
the same check, just machine-readable and explicit.)

The result is one JSON object: `{"v": 1, "type": "doctor", "exit_code": ..., "checks": [...]}`.
Each entry in `checks` is `{"name", "status", "detail", "hint"}` with `status` one of
`"ok"`, `"warn"` or `"missing"`. Read it like this:

- Any `"missing"` status blocks whatever depends on it (`ffmpeg` missing means
  `compress`/`convert`/`split` cannot run at all; `python` below 3.11 blocks everything).
  `doctor`'s own exit code is 3 whenever any check is `"missing"`, 0 otherwise.
- `"warn"` is advisory, not blocking (e.g. `openrouter-key` not configured only matters
  for `ebook build`'s LLM-assisted normalize/dedup stages — pass `--no-llm` to skip
  them; `calibre` not found only matters for `ebook build` and for `convert` on ebook
  formats). `doctor --json` reports both, plus `--op-item NAME` if you need to check a
  named 1Password item rather than an env var.
- With `--check-updates`, an extra `"<package>-update"` entry (status `"warn"`) appears
  for every pip-managed dependency (`yt-dlp`, `yt-dlp-ejs`, `deno`, `imageio-ffmpeg` —
  never `media-tools` itself) that has a newer version on PyPI. If you see one, run:

  ```bash
  media-tools doctor --update
  ```

  This upgrades those pip dependencies in a subprocess and re-checks in a fresh
  interpreter (so the report reflects the new versions, not cached ones). If the package
  was installed with pipx or uv, `--update` instead prints the right upgrade command
  (`pipx upgrade media-tools` / `uv tool upgrade media-tools`) and exits 2 without
  touching anything — that command is for a human to run, not you.

- **Calibre and any other system-tool install/upgrade hint (`brew install ...`,
  `apt install ...`) is for the user, not you.** Relay it; do not run installers,
  package managers, or anything that touches the system outside this project yourself.
- **Never edit files under `media/`** (or wherever the output root points — see
  "Output rules" below). Everything under it is generated: batch folders, downloaded or
  converted files, and each batch's `run.json` book-keeping. Hand-editing `run.json`
  desyncs it from the files actually on disk, and a later run trusts it for skip/resume
  decisions.

## 2. Using the tools

Every task accepts `-h`/`--help` for its exact flags. Every file task (`compress`,
`convert`, `split`), `download` and `ebook` accept `--json` to switch from a human
progress stream on stderr to JSON Lines events on stdout. `formats`, `status` and
`doctor` accept `--json` for a single JSON object instead of a text table.

### One example per task

```bash
media-tools compress lecture.mp4 --preset small --json
media-tools convert lecture.mp4 --to mp3 --json
media-tools split lecture.mp4 --max-size 25MB --json
media-tools download "https://example.com/video" --json
media-tools download --list examples/download-list.json --json
media-tools ebook build books/ --no-llm --json
media-tools ebook build books/ --dry-run --json
media-tools formats --json
media-tools status --json
media-tools status <batch> --json
media-tools doctor --json
```

`split --max-size` units matter: `MB`/`GB` (and a bare number, e.g. `25`) are decimal
(10^6/10^9 bytes); `MiB`/`GiB` are binary (2^20/2^30). The old script this replaces used
binary sizing, so a part it called "25MB" was actually 26,214,400 bytes — enough to be
rejected by a service with a real (decimal) 25 MB limit. Use `MiB`/`GiB` only when you
actually mean binary.

### The JSON Lines event contract (file tasks, `download`, and `ebook`)

Every line is one JSON object with `"v": 1` and a `"type"`. These are the event types
the `Reporter` class (`core/events.py`) actually emits:

| type | when | key fields |
| --- | --- | --- |
| `start` | once, at the beginning of a run that got past argument/input validation | `tool`, `batch`, `output_dir`, `stages` (list of names), `items` (count), `options` |
| `stage` | once per entry in `start`'s `stages` list, in that order | `stage`, `index`, `count` |
| `progress` | zero or more times per item, while an engine is working | `stage`, `item: {index, count, path}`, `percent`, `eta_s` |
| `item` | once per item, when it finishes | `id`, `status`, `input`, `outputs`, `bytes_in`, `bytes_out`, `reason`, `warnings` |
| `warning` | rarely, for a warning not tied to one item | `code`, `message` |
| `error` | on a hard failure | `code`, `message`, `hint`, `retryable` |
| `result` | once, at the very end of a run that started | `ok`, `exit_code`, `counts`, `failed`, `pending`, `outputs`, `run_file`, `elapsed_s`, optionally `data` (`ebook` only: `{"llm": {...}}`, its LLM cost summary, plus `{"placement": {"kept", "renamed", "leftover"}}` once the run reached the convert stage — see "`run.json`" below) |

`stage`'s own `stage` field is distinct both from `progress`'s `stage` field (which
names the sub-step an individual item is in, e.g. `"encode"`) and from the `stages` list
in `start` (which only declares the names up front). For `compress`/`convert`/`split`
(`core.runner.run_items`), `stage` events walk through that list as the run actually
progresses: the first two ("scan", then per-item processing) fire before any `item`,
and any further one (only `split`'s "verify") fires once every item is done. **`ebook`
used to be an exception** (every stage in `start`'s `stages` list announced immediately
after `start`, all before the first `item` or any real work) — as of the I5 fix this is
no longer true: each of the eight (or fewer, for a subcommand that stops early) `stage`
events now fires when that stage's own work actually begins (`tasks/ebook/build.py`'s
`_build_plan`/`_run_pipeline`), the same live-progress-marker role `stage` plays for
every other task. The three sub-stages that can take real, unbounded time on a large
library — **metadata**, **normalize** (only when the LLM is enabled), and **covers** —
also now emit `progress` events between their own `stage` announcement and the next
one, wired to the `on_progress` callback each of those modules already accepted (and,
before this fix, nobody ever passed): `metadata.read_all` reports one book at a time,
`normalize.classify` reports one LLM batch at a time, `covers.resolve` reports its
extract and fetch phases separately (see "Caches..."/the covers row below — combining
them into one counter used to let progress walk past 100%). `--dry-run` never emits
`stage` for the `run_items`-based tasks (nothing is actually processed, so there is no
"processing phase" to announce) — `ebook --dry-run` is again the exception, since its
`stage` events describe the plan itself (still fired live, as each planning phase
begins) rather than real work.

**Two contract details that are easy to get wrong:**

1. **`result` is the last line only for a run that actually started.** A run that
   fails validation before `run_items`/`_download_all` begins — an unrecognized flag, no
   input given, no input matched, an unsupported input format, a missing dependency, an
   unknown subcommand — emits a **bare `error` and nothing else**: no `start`, no
   `result`, process exits (2 or 3). Do not block waiting for a `result` line after an
   `error` that was not preceded by a `start`. Once `start` has been printed, `result`
   is guaranteed to follow — including on a failed item, a batch conflict, or Ctrl+C
   (`exit_code` 130) — so you can always parse the last stdout line as the outcome of a
   run that got that far.
2. **`error` also always prints to stderr as text**, even under `--json` and even under
   `--quiet` — it is the one message `Reporter` never silences. If you only read stdout
   you still get the structured version; this just means stderr is not "clean" the way
   stdout is.

`--summary-json PATH` writes the same object as the final `result` event to a file, as
`{"v": 1, "type": "result", ...}` — convenient if you'd rather read a file than the last
line of a potentially large stdout stream. It is written only when a run actually
reaches its normal end; a batch conflict or a Ctrl+C returns before it would be written.

### Query commands: one JSON object, no events

`formats`, `status` and `doctor` are reports, not multi-item runs: under `--json` each
prints exactly one line, `{"v": 1, "type": ..., ...}`:

- `formats --json` → `{"v": 1, "type": "formats", "formats": [ {task, engine, inputs, outputs, requires}, ... ]}`
- `status --json` (no batch) → `{"v": 1, "type": "status", "batches": [ {batch, task, status, updated_at, active, counts, readable}, ... ]}`
- `status <batch> --json` → `{"v": 1, "type": "status", "batch": {..., "failed": [...], "pending": [...]}}` — note the key is `"batch"` (singular object) here, `"batches"` (list) above.
- `doctor --json` → `{"v": 1, "type": "doctor", "exit_code": ..., "checks": [...]}`

`download --list-formats --json` is a different animal: it prints one `{"v": 1, "type":
"formats", "url": ..., "formats": [...]}` line **per URL** (not one envelope for the
whole list) — don't confuse it with `media-tools formats --json`'s single-envelope shape
above; both happen to use `"type": "formats"` for different payloads.

### The closed code registry

`reason`, the `error` event's `code`, and the `item` event's `warnings` all come from
closed sets in `src/media_tools/core/events.py` — `Reporter` raises `KeyError` on
anything else, so this list is exhaustive; never invent a code not in it.

Item statuses (`item.status`, and `run.json`'s per-item `status`):
`done`, `skipped`, `failed`, `pending`.

`reason` (on an `item` event, and in `run.json`):

| reason | meaning |
| --- | --- |
| `exists` | output already present; skipped (not `--force`) |
| `no_gain` | compressed output was not smaller than the input |
| `already_target_format` | `convert`'s ebook engine: the source is already the requested `--to` format |
| `unsupported_input` | no engine handles this file's extension |
| `output_collision` | two inputs would resolve to the same output path/name |
| `output_equals_input` | the computed output path is the input path itself |
| `source_missing` | `ebook build`: a source file vanished between scan and processing |
| `under_limit` | `split`: file was already at or under `--max-size`, placed unchanged |
| `keyframe_interval_exceeds_max_size` | `split`: a part shorter than the minimum still exceeded the limit |
| `size_limit_unreachable` | `split`: could not get a part under the limit in 3 attempts |
| `engine_error` | the engine raised, or its subprocess failed |
| `dependency_missing` | a required external tool/binary is missing |
| `device_rejected` | (reserved for the future Kindle device path) |
| `no_audio_only_format` | (reserved; see the `no_audio_only_format` warning below) |
| `llm_unavailable` | (reserved; a failed/unavailable LLM call currently falls back to the offline heuristic per batch instead of failing the item) |

`error` event `code`:

`usage`, `no_input_matched`, `batch_in_use`, `batch_task_mismatch`, `dependency_missing`,
`config_missing`, `device_not_found`, `device_busy`, `backup_failed`, `interrupted`,
`output_not_writable`, `internal_error`, `extraction_failed`. `config_missing` is what
`ebook build`/`normalize`/`dedup`/`covers`/`convert` (every subcommand but `scan`) raise
when the LLM is enabled and no OpenRouter key resolves.

`warning` code (on an `item` event's `warnings`, or a standalone `warning` event):

`no_gain`, `no_audio_only_format`, `cover_not_embedded`, `book_id_missing`,
`extension_filter_bypassed`, `device_rejected_thumbnail`, `hash_from_previous`,
`name_collision_suffixed`, `leftover_book`.

(`device_*`, `hash_from_previous` and the `llm_unavailable` reason above are reserved
for the future Kindle-device path and not produced by anything today; `book_id_missing`,
`cover_not_embedded`, `already_target_format`, `source_missing`,
`name_collision_suffixed` and `leftover_book` are all produced by the `ebook` task
described below (`leftover_book` is a standalone `warning` event, not attached to any
one `item` — a leftover was never part of the plan to begin with). They're listed here
regardless because the set is closed and this is the authoritative source.)

`-e/--extensions` only filters a **folder** scan. A file named directly on the command
line is still processed as long as some engine accepts it, even when its extension is
not in an explicitly given `-e` list — with an `extension_filter_bypassed` warning
noting the mismatch, not a silent pass-through.

### Exit codes

| code | meaning |
| --- | --- |
| 0 | success (items with status `skipped` still count as success) |
| 1 | at least one item failed |
| 2 | usage error: bad flags, invalid input, nothing matched, or a batch name/task/options conflict |
| 3 | missing dependency or configuration (a missing tool, or `ebook`'s LLM enabled with no OpenRouter key resolvable and `--no-llm` not passed) |
| 130 | interrupted (Ctrl+C / SIGINT) |

### `run.json`

Every batch folder holds a `run.json` with this shape (abridged; see `core/state.py`):

```json
{
  "v": 1,
  "task": "compress",
  "engine_options": {"codec": "h265", "crf": 28, "audio_bitrate": "96k", "mono": false},
  "batch": "demo",
  "output_dir": "/path/to/output-root/demo",
  "status": "done",
  "owner": null,
  "created_at": "2026-01-01T00:00:00Z",
  "updated_at": "2026-01-01T00:00:01Z",
  "inputs": ["/path/to/input/clip.mp4"],
  "counts": {"total": 1, "done": 1, "skipped": 0, "failed": 0, "pending": 0},
  "items": [
    {
      "id": 1,
      "input": "/path/to/input/clip.mp4",
      "status": "done",
      "reason": null,
      "outputs": [{"path": "clip.mp4", "bytes": 49806}],
      "bytes_in": 77748,
      "elapsed_s": null,
      "warnings": [],
      "data": {}
    }
  ]
}
```

`status` is one of `"running"`, `"done"`, `"failed"`, `"interrupted"`. `owner` is
`{"pid", "host", "since"}` while a run holds the batch's lock, and `null` once it
finishes — `status <batch> --json`'s `"active"` field is exactly `owner is not None`.

**`ebook build`'s per-item `data`** is not empty like the generic shape above — each
surviving item's `data` describes the winning book:

```json
{
  "book_id": "a1b2c3d4-...",
  "format": "epub",
  "size": 512000,
  "meta": {"title": "tmp1603", "author": null, "language": "en"},
  "title": "The Blade Itself",
  "author": "Joe Abercrombie",
  "language": "en",
  "origin": "llm",
  "language_origin": "llm",
  "duplicates": ["/path/to/input/blade-itself-copy.mobi"],
  "cover_source": "embedded",
  "output": "/path/to/output-root/<batch>/en/The Blade Itself - Joe Abercrombie.azw3"
}
```

`meta.*` is the raw embedded metadata Calibre read (before cleanup); `title`/`author`/
`language` are the resolved verdict actually used to place and name the file.
`origin` is one of `"heuristic"` (offline), `"llm"`, `"cache"` (an LLM answer reused
from `.cache/ebook-llm.json`), or `"list"` (a `--list` override) — it covers title,
author and language jointly. `language_origin` (RB22) tracks the language field
*specifically*, since it can come from a different step than the title/author did:
`"embedded_tag"` (the book's own `<dc:language>`, trusted before the title heuristic
even runs), `"title_heuristic"`, `"llm"`, `"cache"`, `"list"`, or `"unknown"`. It exists
separately from `origin` because the tool writes its own guess back into the OUTPUT's
`<dc:language>` field — without a separate field, a re-ingested output's embedded tag
would silently outrank a fresher answer the next time around. `duplicates` lists every
other source path this item's group absorbed — never converted themselves.
`cover_source` is `"embedded"`, `"fetched"`, or `"none"`. A dropped source (vanished
between scan and processing) instead gets `"reason": "source_missing"` and
`data: {"error": "<message>"}`, with no `output`.

The final `result` event (and `--summary-json`'s file) additionally carries a top-level
`data.llm` object summarising the whole run's LLM cost — absent entirely for a
non-ebook task, and all-zero for `--no-llm`/`--dry-run`:

```json
{"llm": {"requests": 12, "cache_hits": 340, "prompt_tokens": 48000,
         "completion_tokens": 6100, "heuristic_fallback_batches": 0}}
```

`heuristic_fallback_batches` counts batches whose OpenRouter request failed and fell
back to the offline heuristic — the one thing worth noticing even when `requests` and
`cache_hits` are both 0 (a total outage still produces usable output, just not
LLM-cleaned).

Whenever the run reached the convert stage, `data` also carries a `placement` object —
the organize stage's own `kept`/`renamed`/`leftover` counts, previously computed by
`library.reconcile()` and never surfaced anywhere:

```json
{"placement": {"kept": 340, "renamed": 2, "leftover": 1}}
```

Each leftover is additionally called out as its own `leftover_book` `warning` event
during the run (named individually, or one warning summarising the count past a
handful) — `run.json` itself gets no per-leftover entry, since a leftover was never
part of the plan to begin with (no book id, no verdict, nothing an `item` expects).

Rather than parsing this file yourself, use:

```bash
media-tools status              # every batch under the output root
media-tools status <batch>      # one batch: counts, failed items + reasons, pending items
```

### Output rules

Output root: `-o`/`--output-dir` > `MEDIA_TOOLS_OUT` env var > the checkout's own
`media/` (only if `media-tools` was installed editable, `pip install -e .`, from that
checkout) > `./media`. Inside it: `<batch>/`, where `<batch>` is `-b`/`--batch NAME` or,
by default, a deterministic 8-character hash of the task, its effective options and its
inputs (same inputs + same flags ⇒ same batch ⇒ a re-run resumes it, skipping items
whose output already exists, unless `--force`). `.cache/` and `_kindle/` directly under
the root are reserved; never point an input at them.

**Reusing a batch name with different options is not silent.** An explicit `-b/--batch
NAME` whose `run.json` already recorded different effective options than this run's
(e.g. a different `--preset`) exits 2 with `batch_task_mismatch`, naming both option
sets, instead of quietly rewriting the record to describe options that never actually
produced the files on disk — pass `--force` to proceed anyway (this updates the
recorded options). A corrupt or non-object `run.json` (from disk corruption, or hand
editing — don't) gets the same treatment: `batch_task_mismatch`, not a crash. This does
not apply to a plain re-run with the *same* options, which resumes normally. A
**default, hash-derived** batch name is handled differently on the same collision: since
you never chose that name yourself, it is not yours to force through or refuse —
`-2`, `-3`, ... is appended instead until a free or matching name is found.

**The editable-install fallback is keyed off the installed package's own location, not
your current directory** (`core/paths.py:_checkout_root`, via `__file__`). If you `cd`
elsewhere mid-session but keep invoking the same repo's `.venv/bin/media-tools`, output
still lands in *that repo's* `media/`, not your new cwd's. Pass `-o`/`MEDIA_TOOLS_OUT`
explicitly if you need output somewhere else — don't assume cwd controls it.

### Supported formats

Generated from the code (`media-tools formats --markdown`); `tests/unit/test_docs.py`
fails if this drifts from `README.md` or from `formats.collect()`.

<!-- formats:start -->
| Task | Engine | Input formats | Output formats | Requires |
| --- | --- | --- | --- | --- |
| compress | video | .avi, .flv, .m4v, .mkv, .mov, .mp4, .mpeg, .mpg, .ts, .webm, .wmv | mp4 | ffmpeg |
| convert | audio | .aac, .avi, .flac, .flv, .m4a, .m4v, .mkv, .mov, .mp3, .mp4, .mpeg, .mpg, .ogg, .opus, .ts, .wav, .webm, .wmv | mp3 | ffmpeg |
| convert | ebook | .azw, .azw3, .epub, .mobi, .pdf, .prc | azw3, epub, mobi, pdf | ebook-convert |
| split | media | .aac, .avi, .flac, .flv, .m4a, .m4v, .mkv, .mov, .mp3, .mp4, .mpeg, .mpg, .ogg, .opus, .ts, .wav, .webm, .wmv | parts | ffmpeg |
| download | - | - | - | - |
| ebook | - | - | - | - |
<!-- formats:end -->

`download` has no engine row: it takes any URL yt-dlp understands, not a fixed set of
file extensions. `ebook` has no row of its own either, but for a different reason: its
conversion step calls Calibre directly rather than through the `Engine` protocol this
table lists, so its own input/output formats are the same as `convert`'s `ebook` row
above (`.azw`/`.azw3`/`.epub`/`.mobi`/`.pdf`/`.prc` in, one of `azw3`/`epub`/`mobi`/`pdf`
out via `--to`) plus `fetch-ebook-metadata` for online cover lookup — see "The `ebook`
task" below for what it actually does with them.

### `download --list` shapes

`--list FILE` reads a JSON file instead of positional URLs. Three shapes, and the array
form can mix plain strings with objects (examples: `examples/download-list.json`,
`examples/download-list-named.json`):

```json
["https://example.com/a.mp4", "https://example.com/b.mp4"]
```

```json
[{"url": "https://example.com/a.mp4", "name": "01-intro"}]
```

```json
{"urls": ["https://example.com/a.mp4"]}
```

`name` sets the output filename; omit it and the entry's own title is used (falling back
to a stable name derived from the URL when the title is generic or missing).

### The `ebook` task

`media-tools ebook <subcommand> <folder-or-files...>` builds a language-sorted ebook
library from a folder of mixed-format ebooks (`.epub`, `.mobi`, `.azw`, `.azw3`,
`.prc`, `.pdf`). Unlike the other file tasks, ebook sources are scanned recursively by
default (pass `--no-recursive` to turn that off), and metadata reads / conversions run
with several concurrent calls into Calibre (8 workers for metadata; `--workers`,
default `min(4, cpu_count)`, for conversion).

**Subcommands** all run the same pipeline and simply stop after their own named stage —
useful for inspecting one stage before committing to a full build:

| subcommand | stops after | notes |
| --- | --- | --- |
| `scan` | dedup | inventory only (metadata, resolved title/author/language, duplicates); converts nothing; never blocks on a missing key — falls back to the offline heuristic instead |
| `normalize` | normalize | scan, read metadata, clean title/author/language |
| `dedup` | dedup | ...through duplicate grouping |
| `covers` | covers | ...through cover resolution |
| `convert` | convert | ...through conversion — books land in their final folder |
| `build` | organize | the full pipeline |

**Known limitation: a subcommand cannot resume a `--batch` from its sources alone.**
Every subcommand — including one that only continues an existing batch — still requires
positional sources (or `--list`) on the command line; there is no way to say "run
`covers` again against `--batch NAME`" and have it re-read that batch's own already-
recorded `inputs` from `run.json` instead of being told the sources again. This is a
real gap (an agent that only has a batch name, not the original file list, cannot
resume it), left alone deliberately rather than folded into this fix round — not a
free change to make alongside everything else here.

**The eight stages** (`tasks/ebook/build.py:STAGE_ORDER`): `scan` (expand inputs) ->
`metadata` (read every book's embedded title/author/language/cover once, cached) ->
`normalize` (clean title/author/language — offline heuristic, or LLM) -> `dedup` (group
a book's different-format copies and merge near-duplicate entries — offline exact
match, then an optional LLM fuzzy-match pass) -> `covers` (resolve one cover per
surviving book before conversion, so it can ride along in the same `ebook-convert`
call) -> `convert` (run `ebook-convert` for whatever survived dedup, straight to its
final path) -> `verify` (re-read the produced file and confirm the title matches and,
for a Kindle-format output, that the cover/book-id are embedded) -> `organize` (placed
as part of `convert`, not a second pass — see "Placement" below).

**Placement — where a converted book ends up**, under the batch directory:

- `<language>/Title - Author.<ext>` — a clean title with a determined language (a
  two-letter code). `verdict.language`'s precedence (RB22 — supersedes an earlier
  ruling and the code that implemented it): `--list` override > the LLM's own answer >
  the book's own embedded `<dc:language>` tag (accepted only when it is exactly two
  alphabetic characters, and never `und`/`mul`/`zxx`) > `language.detect` on the title
  (scores seven languages: `en`, `pt`, `es`, `it`, `fr`, `de`, `pl`) > unknown. All of
  this (below the `--list`/LLM layers in `build.py`) now lives inside
  `normalize.heuristic()`/`_verdict_from()` — there is no separate fallback pass in
  `build.py` any more. The embedded tag is checked *before* the title heuristic runs,
  not only consulted afterward when the heuristic finds nothing — see "Language
  detection" below for why that ordering was the actual bug. `run.json`'s
  `language_origin` field (distinct from `origin`) records which of these actually
  produced the language.
- `_review/<status>/` — a book the LLM flagged `invalid`/`irrelevant`/`unidentified`
  (still converted and placed, never dropped — just somewhere a human should look).
- `_review/unknown-language/` — a book with no usable embedded tag *and* no title-based
  signal either (see "Language detection" below).
- `_leftover/` — a file already in the batch directory that matches nothing in the
  current plan (e.g. dropped from a later `--list`, or the batch was reused for
  different sources), mirroring its own path relative to the batch directory rather
  than flattening to `_leftover/<name>` (RB21 — the old flattened form silently
  destroyed one of two same-named files from different language folders, e.g.
  `en/Title.azw3` and `pt/Title.azw3`). Each leftover is called out as a
  `leftover_book` warning during the run (or one summarising warning past a handful —
  see the JSON event contract above), and the run's `kept`/`renamed`/`leftover` counts
  land in the final `result.data.placement` — none of this was surfaced anywhere
  before (I4): a book moved to `_leftover/` used to produce no item, no warning, and no
  `run.json` entry at all.

Two books whose titles sanitise to the identical filename get a numeric suffix
(` (2)`, ` (3)`, ...) instead of overwriting each other, flagged with the
`name_collision_suffixed` warning. `ebook build --dry-run`'s own preview uses the same
`library.plan_placement()` the real run does (not a per-book `target_path()` call in
isolation), so a collision's suffix actually shows up in the preview too.

**A rebuild converts nothing already on disk; a title fix renames instead of
reconverting — and the rename is honest, not just a filename change (RB20/C1).** `build`
first plans where every source *should* end up, then reconciles that plan against what
already exists: a file already sitting at its planned target is left alone; a file
elsewhere in the batch whose embedded book id (EXTH 113, stable across conversions)
matches is *renamed* into place instead of reconverted, and its embedded title/authors/
language are rewritten (`calibre.update_metadata`, via `ebook-meta --title/--authors/
--language`) to match the plan. Before this fix, only the filename changed — the file's
own embedded title still held whatever it was converted with, so the verify stage
(which compares the file's embedded title against the plan) failed it as
`engine_error` forever, on every single rerun, no matter how many times it was "fixed".
This is what makes an LLM title correction — which changes the target filename — free
instead of a full reconversion: only a book genuinely new to the plan gets an actual
`ebook-convert` call. `--force` skips the book-id lookup entirely (not just the
already-at-target check) — a book it could otherwise find and rename under an old name
is reconverted instead, matching what `--force` means everywhere else.

**`--list FILE`** reads sources from a JSON file instead of positional paths (mutually
exclusive with positional sources — pass one or the other). A plain array of path
strings, or of objects overriding the title/author/language `normalize` would otherwise
produce for that one book (worked example: `examples/ebook-list.json`):

```json
[
  "books/Dom Casmurro - Machado de Assis.epub",
  {
    "path": "books/tmp1603.mobi",
    "title": "The Blade Itself",
    "author": "Joe Abercrombie",
    "language": "en"
  }
]
```

**The LLM pass (`normalize`/`dedup`) costs roughly one OpenRouter request per 30
books**, batched and cached — a rebuild that adds no new books re-pays nothing. It
needs a key, resolved in this order:

1. `OPENROUTER_API_KEY=sk-...` — a literal key.
2. `OPENROUTER_API_KEY=op://vault/item/field` — resolved via `op read` (the 1Password
   CLI must be installed and signed in).
3. `--op-item NAME` — a named 1Password item; its `credential`/`password`/`api key`/
   `apikey`/`key`/`token` field is tried in that order via `op item get ... --reveal`.
   A named item that never resolves can cost up to ~3 minutes here (six field labels,
   30s each) — `media-tools doctor --op-item NAME` uses a much shorter (~5s) per-call
   timeout for this one check and stops after the first call that TIMES OUT (as
   opposed to one that simply returns nothing, which still tries the next label), so
   the health check itself never hangs anywhere near that long; a real `ebook build`
   still uses the full timeout and tries every label.

Pass `--no-llm` to skip all of this and use the offline heuristic only — no key needed,
no network calls, no cost. Every subcommand except `scan` treats a missing key as a
hard `config_missing` error (exit 3) instead of silently falling back, so a real run
never spends Calibre time on a build whose LLM cleanup turns out to be missing by
accident; `scan` is the one exception (see the subcommand table above). `--dry-run`
never calls the LLM either (nor writes its cache) — planning only.

**Translations are never treated as duplicates.** Dedup buckets candidates by
`(language, blocking key)` before comparing them — even the LLM fuzzy-merge pass never
sees two different languages' entries together, and a cross-language cluster the model
proposes anyway is refused, not merged — so a translation and its original always
survive as two separate books, whatever their titles have in common.

**Language detection is deliberately conservative — but only up to the point where it
finds a marker at all.** The offline detector (`tasks/ebook/language.py`) only trusts
stopwords and diacritics exclusive to one of the seven scored languages; a title with
no decisive marker returns `None` rather than a guess. **Once it does find a marker,
though, it is not conservative at all**, and a short, common word can produce a wrong,
confident classification: "Die Trying" (an English Lee Child novel) reads as German
purely from the title, because "Die" is one of German's exclusive marker words, and
"Death Du Jour" (English, Kathy Reichs) reads as French because of "Du" — confirmed
against a real ~3,600-book library (Task 12's rehearsal).

**RB22 fixed what this actually broke: the title heuristic used to run BEFORE the
embedded tag was ever consulted**, so a wrong heuristic guess could *override* an
already-correct tag — both "Die Trying" and "Death Du Jour" have their own embedded
`<dc:language>` set to `en`, and both were shelved on the heuristic's wrong guess
anyway, because the tag was only ever checked as a fallback for when the heuristic
found *nothing*, never as a check the heuristic's own answer could lose to. The fix
(see "Placement" above) checks the tag *first*: a book whose tag is present and looks
like a genuine two-letter code never even reaches `language.detect`. The heuristic's
own blind spot (a short marker word producing a wrong guess) is therefore now
UNREACHABLE for a book with a valid tag — it only still applies to a book with no tag
at all, or an invalid one (`und`/`mul`/`zxx`, or anything not exactly two letters). A
book under a shelf that looks wrong is worth checking `run.json`'s `language_origin`
for that book (`embedded_tag` means the file's own tag put it there — check the file;
`title_heuristic` means the title did — check `tasks/ebook/language.py`'s marker
lists) before assuming the file itself, or the placement logic, is broken.

**Caches, all under `<output-root>/.cache/`, shared across every ebook batch** (not
per-batch — re-scanning the same library into a new batch should reuse them):

| file/dir | keyed by | invalidated by |
| --- | --- | --- |
| `ebook-meta.json` | resolved path + file size + mtime | the file itself changing on disk; moving/renaming the *library* does not invalidate this, since the path is part of the key — a moved file just re-reads once |
| `ebook-llm.json` | filename + embedded title/author + `--model` + the prompt version (never the path) | the filename, embedded metadata, `--model`, or the prompt changing — moving/reorganising the library on disk never invalidates it |
| `covers/<book-id>.jpg` | the book's stable EXTH 113 id | nothing automatically; delete the file to force re-resolution |

Both `ebook-meta.json` and `ebook-llm.json` are written through the project's own
temp+fsync+rename helper (`core.paths.fsync_replace`, via a `.partial` name), not a
bare `write_text` (I7) — both files are shared across every batch and are written from
OUTSIDE any single batch's lock, so a concurrent run or a Ctrl+C mid-write must not
truncate one and silently discard everything cached in it.

`--dry-run` never writes to any of these — metadata is still read (through a private,
auto-removed scratch directory so concurrent dry runs never fight over Calibre's
config) but nothing persists to `.cache/`.

Per-book fields in `run.json`, and the LLM cost summary on the final `result` event,
are documented in "`run.json`" below.

### Safety guarantees

- **Never prompts.** No task falls back to interactive input; a format/quality choice
  that would need one instead picks a safe default or fails with a `usage`/
  `dependency_missing` error.
- **Never overwrites an input.** `output_equals_input` is checked and refused before any
  write; outputs live in the batch folder, never mixed into the input tree.
- **`--dry-run` writes nothing** — not the output files, not `run.json`, not the batch
  directory itself. It only prints the plan (as `item`/`result` events with no `start`
  producing real state).
- **URLs are redacted on every surface** — stdout JSON, stderr text, and `run.json` —
  before they are ever printed or written. Only scheme+host+path survive; query strings
  and fragments (where tokens/signatures live) are stripped by `core/redact.py`.

## 3. Changing the code

### Architecture map

```
src/media_tools/
  cli.py               argparse wiring: builds the parser, dispatches to TASKS
  core/
    engine.py          Engine protocol, Dependency, select_engine, missing_dependencies
    runner.py          Item / Context / Outcome, run_items() — the shared per-item loop
    events.py          Reporter, the JSON Lines schema, exit codes, the closed code registries
    state.py           RunState / run.json, the batch lock
    paths.py           output_root(), batch_hash()/sanitize_batch(), mirror_output(), temp_path()
    inputs.py          expand_inputs(): CLI paths -> Source objects (recursive scan, extensions, include/exclude)
    ffmpeg.py          ffmpeg_exe(), probe() (no ffprobe), run_ffmpeg() (progress-aware subprocess)
    media_formats.py   shared extension sets: VIDEO_EXTENSIONS, AUDIO_EXTENSIONS
    redact.py          redact_url()/redact_text()/redact() — strip query+fragment from URLs
    sizes.py           parse_size()/format_size() — decimal vs binary units
  tasks/
    common.py          add_common_flags(), prepare(), UsageError — shared by every file task
    compress/          __init__.py (NAME/HELP/ENGINES/register/run) + video.py (VideoEngine)
    convert/           __init__.py + audio.py (AudioEngine) + ebook.py (EbookEngine, plain format-to-format)
    split/             __init__.py + media.py (MediaSplitEngine)
    download/          __init__.py (its own loop; URLs, not files) + ytdlp.py (yt-dlp glue)
    ebook/             __init__.py + build.py (the pipeline, STAGE_ORDER, all 6 subcommands)
                       metadata.py (read_all, cached ebook-meta) · normalize.py (heuristic/classify)
                       dedup.py (group/refine) · covers.py (resolve) · library.py (plan_placement/reconcile)
                       names.py · language.py · opf.py · exth.py (EXTH-record helpers)
    formats.py         lists every task's ENGINES (formats.TASK_MODULES)
    status.py          reads run.json across batches
    doctor.py          environment checks
  integrations/
    calibre.py         ebook-convert/ebook-meta/fetch-ebook-metadata: locate, read metadata, convert, fetch a cover
    openrouter.py      OpenRouter chat client + the 3-way key lookup (resolve_key/key_present)
```

`compress`/`convert`/`split` are "file tasks": they share `tasks.common.prepare()` (CLI
args → `Source` list + batch dir + options) and `core.runner.run_items()` (the per-item
loop: plan, skip-if-exists, call the engine, update `run.json`, emit events). `download`
and `ebook` take URLs/whole-library plans respectively, not one file per engine call, so
neither uses `prepare()`/`run_items()` — see each one's own module docstring — but both
emit the same event/`run.json` shapes by hand. `formats`, `status` and `doctor` are
reports about the tool itself; they have no `ENGINES`.

### The `Engine` protocol (`core/engine.py`)

```python
class Engine(Protocol):
    name: str
    inputs: frozenset[str]
    outputs: frozenset[str]
    dependencies: tuple[Dependency, ...]

    def add_arguments(self, group) -> None: ...
    def hash_options(self, args) -> dict: ...
    def output_names(self, src: Path, args) -> list[str]: ...
    def process(self, item, ctx) -> Any: ...
```

`inputs` are dotted extensions (`".mp4"`); `outputs` are bare format names (`"mp4"`,
`"mp3"`, `"parts"`) — `select_engine` matches `f".{suffix}" in engine.inputs` and, when a
target format was requested (`convert --to`), `to in engine.outputs`. `hash_options`
returns the subset of parsed args that affects the *output* (feeds the default batch
hash — see "Batch hashes must stay canonical" below). `output_names` returns the
filename(s) the engine will actually write for one source, which the runner mirrors into
the batch dir and uses for skip/collision detection — it must be exactly what `process`
writes, or the runner's bookkeeping and the real files disagree.

### `Item` / `Context` / `Outcome` (`core/runner.py`)

```python
@dataclass
class Item:
    id: int
    source: Path
    root: Path | None
    outputs: list[Path]
    engine: Engine | None = None


@dataclass
class Context:
    batch_dir: Path
    reporter: Reporter
    dry_run: bool = False
    force: bool = False
    deps: dict[str, Any] = field(default_factory=dict)
    total_items: int = 0


@dataclass
class Outcome:
    status: str
    outputs: list[Path]
    bytes_out: int | None
    reason: str | None = None
    warnings: list[str] | None = None
    data: dict | None = None
```

`Engine.process(item, ctx)` reads `ctx.deps` (whatever the task put there — e.g.
`{"ffmpeg": ..., "options": ...}`), does the work, and returns an `Outcome`.
`status` must be `"done"` or `"failed"` (the runner assigns `"skipped"`/`"pending"`
itself); `reason`/`warnings` must be codes from the closed registry in `core/events.py`
— an unlisted code raises `KeyError` inside `Reporter`. On `"failed"`, the runner deletes
any partial/final files under `item.outputs` itself; an engine does not need to clean up
its own declared outputs (only truly temporary files it created outside that list).

### How to add a task

1. Create `src/media_tools/tasks/<name>/__init__.py` with module-level `NAME`, `HELP`,
   `register(subparsers) -> ArgumentParser`, and `run(args) -> int`. If it processes
   files, add `ENGINES: list` too.
2. If it's a file task: call `tasks.common.add_common_flags(parser)` in `register`, and
   in `run` call `tasks.common.prepare(args, task=NAME, engines=ENGINES, to=...)` then
   `core.runner.run_items(prepared.sources, task=NAME, engines=ENGINES, ...)` — see
   `tasks/compress/__init__.py` for the minimal worked example. If it isn't a simple
   one-file-in-one-file-out loop (`download`'s URLs, or `ebook`'s whole-library
   planning — dedup and placement need every book's verdict at once, not just one item
   at a time), drive `core.events.Reporter` and `core.state.RunState` directly,
   matching the same event sequence and `run.json` shape.
3. Register it — one line in **two** places:
   - `cli.py`: import the module and add it to `TASKS`.
   - `tasks/formats.py`: add it to `TASK_MODULES` if it has (or will have) `ENGINES`, so
     it shows up in the formats table instead of only through a manual code review.
4. Add a test: at minimum a unit test of `register`/`run` wiring (see
   `tests/unit/test_cli.py`); if it touches real media, an integration test using the
   `make_video`/`make_audio` fixtures (see "Testing conventions" below).

### How to add an engine

1. Create a module under the task's package (e.g. `tasks/compress/image.py`)
   implementing the `Engine` protocol above.
2. Add one line: append an instance to the task's `ENGINES` list in its `__init__.py`.
3. Regenerate the docs table and paste it between the `<!-- formats:start -->` /
   `<!-- formats:end -->` markers in **both** `README.md` and this file:

   ```bash
   media-tools formats --markdown
   ```

   `tests/unit/test_docs.py` fails the build if either copy is stale.

### Testing conventions and markers

- `tests/unit/` — fast, no real media required; call modules directly, or spawn the CLI
  via `subprocess.run([sys.executable, "-m", "media_tools", ...])` for boundary tests
  (argument parsing, exit codes, JSON event shapes).
- `tests/integration/` — spawn the real CLI the same way, but exercise real ffmpeg via
  the `make_video`/`make_audio` fixtures in `tests/conftest.py`. Those fixtures generate
  tiny synthetic clips with `ffmpeg`'s `lavfi` sources (`testsrc`, `sine`) — no fixture
  media files ship in the repo, and no network is used.
- Markers (declared in `pyproject.toml`): `network` (needs the public internet), `llm`
  (calls a real LLM API — the ebook task's own normalize/dedup tests instead inject a
  fake `chat` callable, so this marker still isn't used by anything today), `device`
  (needs a real Kindle connected — reserved, same). Only `network` is used by any test
  today. Run the offline suite — the one CI runs — with:

  ```bash
  .venv/bin/pytest -m "not network and not llm and not device"
  ```
- Lint/format gate: `.venv/bin/ruff check . && .venv/bin/ruff format --check .`.

### Gotchas

- **No `ffprobe`.** The bundled ffmpeg (via `imageio-ffmpeg`) does not ship an `ffprobe`
  binary. Never shell out to one. Use `core.ffmpeg.probe(path)`, which reads duration
  and bitrate by parsing the header `ffmpeg -i <path>` prints to stderr (exit code 1 is
  expected there — no output file was given).
- **yt-dlp needs Deno** for extractors that require running the site's own JS (signature
  ciphers on some hosts). It comes from the `yt-dlp[default,deno]` dependency; `doctor`'s
  `deno-runtime` check actually runs `deno --version` — the `deno` *check* only confirms
  the pip package is installed, which is not the same thing.
- **Calibre is external and runs with an isolated config.** Every call
  (`integrations/calibre.py`) sets its own `CALIBRE_CONFIG_DIRECTORY` — never assume or
  touch the user's own Calibre library/settings. `doctor`'s `calibre` check looks for
  all three CLI tools it uses: `ebook-convert`, `ebook-meta`, and `fetch-ebook-metadata`
  (the last one only for `ebook build`'s online cover lookup, skipped entirely by
  `--no-cover-fetch`) — via `calibre.find_tool` (I6), the same lookup `ebook build`/
  `convert` themselves use, not a bare `shutil.which`. `find_tool` also searches e.g.
  `/Applications/calibre.app/Contents/MacOS` on macOS, so a .dmg/App-bundle install
  that isn't on PATH is still found; before this fix `doctor` alone used `shutil.which`
  and could warn "not found" while every other command worked fine (same defect class
  RB2 already fixed for the OpenRouter key check).
- **Split's parts overlap at keyframes by design.** `MediaSplitEngine._split`
  (`tasks/split/media.py`) starts each next part slightly *before* the previous part's
  measured end (`duration * (1 - MARGIN_RATIO)`), guaranteeing overlap rather than ever
  dropping content — because parts are cut with `-c copy` on keyframe boundaries, and
  per-stream duration can't be measured without `ffprobe`. A test that expects parts to
  tile the source exactly is wrong; assert coverage (`sum(part durations) >= source
  duration`), not equality.
- **Batch hashes must stay canonical.** `core.paths.batch_hash` and
  `download._batch_hash` both serialize their payload with
  `json.dumps(..., sort_keys=True, separators=(",", ":"))` before hashing. Changing what
  goes into that payload (a new option, a new selection field, reordering) changes every
  future hash for commands that used to land in the same batch — that is a breaking
  change to resumability, not a free refactor. If you must change it, bump the payload's
  `"hash_v"` deliberately rather than silently.
- **URLs must be redacted on every surface.** Anything that might print or store a URL —
  a new event field, a new error message, a new `run.json` field — must go through
  `core.redact.redact_url`/`redact_text`/`redact`, not `str(url)` directly. `Reporter`
  already redacts every field it emits and every message it prints, but code that builds
  its own strings (like an engine's `data={"error": ...}`) does not get this for free.
