# media-tools — agent guide

This file is for an agent driving or modifying `media-tools`: a CLI that compresses,
converts, splits, downloads and (eventually) organises media and ebooks. It has three
parts: start-of-session checks, how to drive the tool as a black box, and how to change
its code.

`ebook` is registered (`media-tools ebook --help` works) but not implemented — it exits
3 with a clear message. Do not write code that assumes it works.

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
- `"warn"` is advisory, not blocking (e.g. `openrouter-key` not set only matters once the
  future `ebook` LLM features exist; `calibre` not found only matters for `ebook`).
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
`convert`, `split`) and `download` accept `--json` to switch from a human progress
stream on stderr to JSON Lines events on stdout. `formats`, `status` and `doctor` accept
`--json` for a single JSON object instead of a text table.

### One example per task

```bash
media-tools compress lecture.mp4 --preset small --json
media-tools convert lecture.mp4 --to mp3 --json
media-tools split lecture.mp4 --max-size 25MB --json
media-tools download "https://example.com/video" --json
media-tools download --list examples/download-list.json --json
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

### The JSON Lines event contract (file tasks and `download`)

Every line is one JSON object with `"v": 1` and a `"type"`. These are the event types
the `Reporter` class (`core/events.py`) actually emits:

| type | when | key fields |
| --- | --- | --- |
| `start` | once, at the beginning of a run that got past argument/input validation | `tool`, `batch`, `output_dir`, `stages` (list of names), `items` (count), `options` |
| `stage` | once per entry in `start`'s `stages` list, in that order — a run's first two ("scan"/plan, then per-item processing) fire before any `item`; any further one (only `split`'s "verify") fires once every item is done | `stage`, `index`, `count` |
| `progress` | zero or more times per item, while an engine is working | `stage`, `item: {index, count, path}`, `percent`, `eta_s` |
| `item` | once per item, when it finishes | `id`, `status`, `input`, `outputs`, `bytes_in`, `bytes_out`, `reason`, `warnings` |
| `warning` | rarely, for a warning not tied to one item | `code`, `message` |
| `error` | on a hard failure | `code`, `message`, `hint`, `retryable` |
| `result` | once, at the very end of a run that started | `ok`, `exit_code`, `counts`, `failed`, `pending`, `outputs`, `run_file`, `elapsed_s` |

`stage`'s own `stage` field is distinct both from `progress`'s `stage` field (which
names the sub-step an individual item is in, e.g. `"encode"`) and from the `stages` list
in `start` (which only declares the names up front) — `stage` events are what actually
walks through that list as the run progresses. `--dry-run` never emits `stage` (nothing
is actually processed, so there is no "processing phase" to announce).

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
| `already_target_format` | (reserved for a future engine) |
| `unsupported_input` | no engine handles this file's extension |
| `output_collision` | two inputs would resolve to the same output path/name |
| `output_equals_input` | the computed output path is the input path itself |
| `source_missing` | (reserved) |
| `under_limit` | `split`: file was already at or under `--max-size`, placed unchanged |
| `keyframe_interval_exceeds_max_size` | `split`: a part shorter than the minimum still exceeded the limit |
| `size_limit_unreachable` | `split`: could not get a part under the limit in 3 attempts |
| `engine_error` | the engine raised, or its subprocess failed |
| `dependency_missing` | a required external tool/binary is missing |
| `device_rejected` | (reserved for the Kindle device path) |
| `no_audio_only_format` | (reserved; see the `no_audio_only_format` warning below) |
| `llm_unavailable` | (reserved for the future ebook LLM features) |

`error` event `code`:

`usage`, `no_input_matched`, `batch_in_use`, `batch_task_mismatch`, `dependency_missing`,
`config_missing`, `device_not_found`, `device_busy`, `backup_failed`, `interrupted`,
`output_not_writable`, `internal_error`, `extraction_failed`.

`warning` code (on an `item` event's `warnings`, or a standalone `warning` event):

`no_gain`, `no_audio_only_format`, `cover_not_embedded`, `book_id_missing`,
`extension_filter_bypassed`, `device_rejected_thumbnail`, `hash_from_previous`.

(Several of the above — `device_*`, `book_id_missing`, `cover_not_embedded`,
`llm_unavailable`, `hash_from_previous`, `already_target_format`, `source_missing` — are
reserved for the future `ebook` task and not produced by anything today; they're listed
because the set is closed and this is the authoritative source.)

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
| 3 | missing dependency or configuration (includes the `ebook` stub) |
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
file extensions. `ebook` is unimplemented.

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
    convert/           __init__.py + audio.py (AudioEngine)
    split/             __init__.py + media.py (MediaSplitEngine)
    download/          __init__.py (its own loop; URLs, not files) + ytdlp.py (yt-dlp glue)
    ebook/             __init__.py — stub, exits 3
    formats.py         lists every task's ENGINES (formats.TASK_MODULES)
    status.py          reads run.json across batches
    doctor.py          environment checks
```

`compress`/`convert`/`split` are "file tasks": they share `tasks.common.prepare()` (CLI
args → `Source` list + batch dir + options) and `core.runner.run_items()` (the per-item
loop: plan, skip-if-exists, call the engine, update `run.json`, emit events). `download`
takes URLs, not files, so it does not use either of those — see its module docstring —
but it emits the same event/`run.json` shapes by hand. `formats`, `status` and `doctor`
are reports about the tool itself; they have no `ENGINES`.

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
   `tasks/compress/__init__.py` for the minimal worked example. If it's not file-shaped
   (like `download`), drive `core.events.Reporter` and `core.state.RunState` directly,
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
  (calls a real LLM API — reserved for the future ebook features), `device` (needs a
  real Kindle connected — same). Only `network` is used by any test today. Run the
  offline suite — the one CI runs — with:

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
- **Calibre is external and runs with an isolated config** (per the ebook plan this
  repo does not implement yet) — never assume or touch the user's own Calibre
  library/settings; `doctor`'s `calibre` check only looks for `ebook-convert`/
  `ebook-meta` on PATH.
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
