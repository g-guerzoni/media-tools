# media-tools

Command-line tools to compress, convert, split, download and organise media and ebooks —
one command, one subcommand per task, replacing several older single-purpose scripts.

`compress`, `convert`, `split`, `download`, `ebook`, `formats`, `status` and `doctor`
all work today. `ebook build` turns a folder of mixed-format ebooks into a
language-sorted, deduplicated library — see "Building an ebook library" below — and
`ebook kindle` puts that library on a Kindle, with a backup before every write; see
"Putting books on a Kindle".

## Install

### Clone + venv (for development)

```bash
git clone https://github.com/g-guerzoni/media-tools.git
cd media-tools
python3 -m venv .venv
.venv/bin/pip install -e . --group dev
.venv/bin/media-tools doctor
```

Any Python >= 3.11 works. On macOS, install one with Homebrew (`brew install python`)
if the system Python is older; no particular minor version is required, and this file
deliberately names none, because a pinned one goes stale the moment Homebrew moves.

`--group dev` needs pip >= 25.1. On an older pip, install the dev tools directly
instead:

```bash
.venv/bin/pip install -e . pytest ruff
```

### pipx

```bash
pipx install git+https://github.com/g-guerzoni/media-tools.git
```

### uv

```bash
uv tool install git+https://github.com/g-guerzoni/media-tools.git
```

Either of these gives `media-tools` its own isolated environment and puts the command on
your PATH, without a local clone.

## Requirements

- **Python >= 3.11.** Any minor version from 3.11 up; see "Install" above.
- **ffmpeg** — nothing to install: it ships with the `imageio-ffmpeg` dependency.
- **A JS runtime for YouTube extraction** — ships with the `yt-dlp[default,deno]`
  dependency (it installs the `deno` pip package, which vendors the Deno binary).
  Some sites need this to decode signature ciphers before `download` can fetch them.
- **Calibre** — needed by `ebook build` (all of it), by `convert` when converting
  to/from an ebook format, and by `ebook kindle` for a Kindle that speaks MTP (see
  "Putting books on a Kindle"). Install with `brew install --cask calibre`.
- **A Kindle** — only for `ebook kindle`, and only when you actually want to touch
  one. `media-tools doctor` reports whether one is connected, and having none attached
  is not a problem it complains about.
- **An OpenRouter API key** — optional, only for `ebook build`'s LLM-assisted cleanup.
  Skip it entirely with `--no-llm`. See "Building an ebook library" below.

Run `media-tools doctor` any time to check all of the above against this machine (see
"Troubleshooting").

## Quick start

Every file task (`compress`, `convert`, `split`) takes files and/or folders and writes
into a batch folder under an output root (see "Where output goes"). Every task accepts
`--json` for machine-readable output and `-h`/`--help` for its full flag list.

```bash
# Compress a video (presets: high, medium, small, tiny)
media-tools compress lecture.mp4 --preset small

# Convert video or audio to mp3
media-tools convert lecture.mp4 --to mp3

# Split a file into parts that never exceed a size limit
media-tools split lecture.mp4 --max-size 25MB

# Download from a URL (yt-dlp)
media-tools download "https://example.com/video"

# Download a batch of URLs from a list file
media-tools download --list examples/download-list.json

# Build a language-sorted AZW3 library from a folder of mixed ebook formats
# (offline: no OpenRouter key needed, no cost)
media-tools ebook build books/ --no-llm

# Same, with an OpenRouter key configured: an LLM pass also cleans titles/languages
media-tools ebook build books/

# Put a library on a connected Kindle (a backup is always taken first)
media-tools ebook kindle sync --batch mylibrary

# See what each task can read and write
media-tools formats

# Check a batch's progress, or list every batch
media-tools status

# Check the environment (ffmpeg, Deno, Calibre, the output root, ...)
media-tools doctor
```

`split --max-size` units matter: `MB`/`GB` (and a bare number, e.g. `25`) are decimal
(10^6/10^9 bytes); `MiB`/`GiB` are binary (2^20/2^30). The old script this replaces used
binary sizing, so a part it called "25MB" was actually 26,214,400 bytes — enough to be
rejected by a service with a real (decimal) 25 MB limit. Use `MiB`/`GiB` only when you
actually mean binary.

`media-tools ebook build` also takes `--dry-run` to preview what it would do without
converting or writing anything — see "Building an ebook library" below for the full
picture (stages, where books land, deduplication, the LLM cost, and how to supply a
key).

## Supported formats

Generated straight from the code (`media-tools formats --markdown`), so it can't drift
from what the engines actually declare — `tests/unit/test_docs.py` fails the build if
this table and the code disagree.

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

`download` and `ebook` have no row of extensions: `download` isn't format-converting —
it fetches whatever URL yt-dlp understands — and `ebook` converts through Calibre
directly rather than the `Engine` protocol this table lists (see the `convert | ebook`
row above for the formats it actually moves between, and "Building an ebook library"
below for what it does with them).

Regenerate this table after adding or changing an engine:

```bash
media-tools formats --markdown
```

and paste the output between the markers above (and the matching ones in `CLAUDE.md`).

## Batch (list) files for `download`

`download --list FILE` reads URLs from a JSON file instead of positional arguments.
Three shapes are accepted, and the array form can mix plain strings with objects:

```json
["https://example.com/lessons/intro.mp4", "https://example.com/lessons/chapter-1.mp4"]
```

```json
[{"url": "https://example.com/lessons/intro.mp4", "name": "01-intro"}]
```

```json
{"urls": ["https://example.com/lessons/intro.mp4"]}
```

Worked examples: `examples/download-list.json` (plain URLs) and
`examples/download-list-named.json` (named entries — `name` sets the output filename;
without it, the entry's own title is used, falling back to a stable name derived from
the URL when the title isn't useful).

`ebook build` accepts its own, differently-shaped `--list FILE` — see "Building an
ebook library" below.

## Building an ebook library

`media-tools ebook build <folder-or-files...>` turns a folder of mixed-format ebooks
(`.epub`, `.mobi`, `.azw`, `.azw3`, `.prc`, `.pdf`) into one library, sorted by
language, with duplicates dropped and only the winning copy of each book converted.
Folders are scanned recursively by default.

It runs eight stages in order: **scan** (find the books), **metadata** (read each
one's embedded title/author/language/cover once, cached so a rebuild is fast),
**normalize** (clean up the title/author/language — offline heuristics, or an LLM pass),
**dedup** (group each book's different-format copies together, and merge near-duplicate
entries so only one copy of each book survives), **covers** (find or fetch one cover per
surviving book), **convert** (run Calibre for whatever survived dedup), **verify**
(re-read the converted file to confirm it came out right), **organize** (file it under
its final folder — done together with conversion, not as a separate move).

The other `ebook` subcommands (`scan`, `normalize`, `dedup`, `covers`, `convert`) run
that same pipeline and just stop earlier, useful for previewing one stage before
committing to a full `build`.

**Where books end up**, under the batch folder `media-tools ebook build` creates:

- `<language>/Title - Author.<ext>` — the normal case: a two-letter language code and
  a clean title/author. The language comes from, in order: a `--list` override, the
  LLM's own answer, the book's own embedded language tag (accepted only when it looks
  like a genuine two-letter code — a bare "und"/"mul"/"zxx" tag is ignored), and only
  then the offline title heuristic (which scores seven languages: `en`, `pt`, `es`,
  `it`, `fr`, `de`, `pl`). The embedded tag is checked *before* the title heuristic
  runs, not only as a fallback when the heuristic finds nothing — see "a heads-up
  from the real-library rehearsal" below for why that order matters.
- `_review/<status>/` — a book the LLM pass flagged as not a real, identifiable title
  (`invalid`, `irrelevant`, `unidentified`). It is still converted and placed here, not
  dropped — just somewhere for a human to take a look.
- `_review/unknown-language/` — a book whose embedded tag isn't a genuine two-letter
  code *and* whose title gave no language signal either. Offline title detection is
  deliberately conservative: a title with no clear marker for one of the seven scored
  languages is left unplaced rather than guessed, since a wrong shelf is worse than a
  review folder.
- `_leftover/` — a file already in the batch folder that no longer matches anything in
  the current plan (for example, a book dropped from a later `--list`), mirroring its
  own path relative to the batch (so `en/Title.azw3` and `pt/Title.azw3` both survive
  as leftovers instead of one silently overwriting the other). Each leftover is called
  out as a warning during the run (or one summarising warning past a handful), and the
  run's `kept`/`renamed`/`leftover` counts appear in the final summary alongside the
  LLM cost report.

**A heads-up from the real-library rehearsal:** the title-based guess is *not*
conservative once it does find a marker — a short, common word can still trigger a
wrong, confident match. Two examples found in a real ~3,600-book library: "Die Trying"
(an English Lee Child novel) has a marker ("Die") that an offline title guess alone
would read as German, and "Death Du Jour" (English, Kathy Reichs) has one ("Du") that
would read as French. Because the embedded language tag is now checked *before* the
title heuristic runs, both are correctly shelved under `en/` from their own `en` tag —
but a book whose *tag itself* is missing or wrong, and whose title also carries one of
these short markers, still lands on the heuristic's guess. If a book ends up under a
shelf that looks wrong, check `data.language_origin` in `run.json` for that book
(`embedded_tag` vs `title_heuristic`) before assuming the file itself is broken.

**Re-running `build` on the same folder converts nothing that's already there.** It
plans where every book should end up, then checks what's already on disk: a file
already at its planned location is left alone, and a file elsewhere in the batch whose
stable internal book id matches is *renamed* into place instead of reconverted — its
embedded title/author/language are rewritten to match too, not just its filename, so
the rename is genuinely complete. In practice this means an LLM title correction is
free — it renames (and relabels) the existing file rather than running Calibre again —
and only genuinely new books get converted. `--force` skips this rename lookup
entirely: a book it would otherwise find and relabel under an old name is reconverted
instead, matching what `--force` means everywhere else ("redo items whose output
exists").

**Duplicates and translations.** The same book showing up as an `.epub` and a `.mobi`
collapses into one entry (only the preferred format is converted; `--prefer` controls
the order). A Portuguese translation and its English original are never merged into
each other, no matter how similar their titles look — duplicate detection never
compares books across languages.

**The LLM pass costs about one request per 30 books**, and every answer is cached, so a
rebuild that adds no new books doesn't re-pay for the ones it already classified. It
needs an OpenRouter API key, which you can provide any of three ways:

```bash
export OPENROUTER_API_KEY=sk-...                 # a literal key
export OPENROUTER_API_KEY=op://vault/item/field  # a 1Password reference
media-tools ebook build books/ --op-item NAME    # a named 1Password item
```

Or skip the LLM entirely — no key, no network calls, no cost:

```bash
media-tools ebook build books/ --no-llm
```

`--dry-run` previews the whole plan (what would be kept, deduplicated, and where
everything would land) without converting, writing, or calling the LLM at all.

**`--list FILE`** builds from a JSON list instead of scanning a folder — a plain array
of paths, or of objects that override the title/author/language for one book (see
`examples/ebook-list.json`):

```json
["books/Dom Casmurro - Machado de Assis.epub",
 {"path": "books/tmp1603.mobi", "title": "The Blade Itself", "author": "Joe Abercrombie"}]
```

**Caches** live under `.cache/` in the output root, shared across every `ebook` batch
so re-scanning the same library elsewhere reuses them: `ebook-meta.json` (metadata per
file, invalidated when that file's size or modification time changes), `ebook-llm.json`
(LLM answers, keyed by filename and embedded metadata rather than by path, so moving or
reorganising the library doesn't throw away what was already classified), and
`covers/<book-id>.jpg` (resolved covers). None of these are written during `--dry-run`.

Every batch's `run.json` records, per book, its resolved title/author/language, where
it came from (offline heuristic, LLM, cache, or a `--list` override), which other files
were folded into it as duplicates, and where it was written. The language's own origin
(`language_origin`: `embedded_tag`, `title_heuristic`, `llm`, `cache`, `list`, or
`unknown`) is recorded separately, since it can come from a different step than the
title/author did.

## Putting books on a Kindle

`media-tools ebook kindle <subcommand>` talks to a Kindle plugged in over USB:

```bash
media-tools ebook kindle status                 # which Kindle, which mode, how much room
media-tools ebook kindle scan                   # every book on it, read from the books
media-tools ebook kindle backup                 # snapshot it onto this machine
media-tools ebook kindle thumbnails             # install missing cover thumbnails
media-tools ebook kindle add book.azw3          # copy books onto it
media-tools ebook kindle remove "documents/en/Book.azw3" --yes
media-tools ebook kindle sync --batch mylibrary # mirror an `ebook build` batch
media-tools ebook kindle restore --yes          # put a snapshot's files back
media-tools ebook kindle eject                  # flush and release it
```

**Everything is read from the books themselves, never from their filenames.** A book
is identified by the stable id embedded in it, so the same book under a different name
is still the same book, and a book renamed on the device is not a new one.

### The two connection modes, and how to tell them apart

- **Mass storage** — the Kindle mounts as a disk and shows up in your file manager.
- **MTP** — a 2024-or-later model (or a Scribe) shows *no disk at all*. That's
  expected, not a fault: those models speak MTP, and media-tools reaches them through
  Calibre's own device driver (so Calibre must be installed for that half; a
  mass-storage Kindle needs none of it).

`media-tools ebook kindle status` says which one it found, and `media-tools doctor`
reports the same thing — plus, when the Kindle it found speaks MTP, whether Calibre's
driver is actually available. Detection never trusts a model table: it looks for the
mount, because firmware updates have moved that line before.

**Close Calibre before using an MTP Kindle.** An MTP device allows exactly one
program to hold it, and Calibre's desktop app grabs a connected device the moment it
sees one — so every `ebook kindle` command will refuse to run while it is open, saying
the device is busy. media-tools never quits a program for you. `media-tools doctor`
flags this too, so you find out before a long command does.

Two other things work differently over MTP, both because of what Calibre's driver
exposes: a book's `.sdr` sidecar can't be deleted at all (see `remove` below), and
every `.kfx` book is refused for removal, because a purchased one can't be told apart
from a sideloaded one there.

### Every write is preceded by a backup, and there is no flag to skip it

Every run that writes to the device takes a full snapshot of it *first*, and aborts
if that snapshot fails — nothing is copied or deleted after a failed backup, and there
is no flag to turn this off. `thumbnails`, `add` and `sync` always take one (except
under `--dry-run`, which writes nothing anyway); `remove` and `restore` take one when
you pass `--yes`, which is the only way either of them writes at all. `status`, `scan`
and `eject` write nothing and take none.

Snapshots live under the output root:

```
<output root>/_kindle/<serial>/backups/<UTC timestamp>/
```

`<serial>` is your Kindle's own serial number — or, if it doesn't report one, a short
`unknown-`-prefixed name derived from whichever of the volume name, the model or the
USB product id is available, so two serial-less devices never share a folder.
`media-tools ebook kindle status` tells you which one yours got.

Each snapshot holds a `manifest.json` and a `files/` tree mirroring the device's own
paths. A snapshot is built under a temporary name and renamed only once complete, so
an interrupted run can leave an unfinished snapshot but never one that looks finished.
Backups are incremental: a file that hasn't changed is hard-linked from the previous
snapshot instead of being transferred again.

**Snapshots are never pruned.** This tool does not delete your backups — not the
oldest, not the largest, not ever. Over months of use that adds up, and clearing space
is yours to do. `media-tools ebook kindle status` reports the most recent snapshot and
any unfinished ones left behind.

### What `remove` actually deletes

A removal takes **three things together**: the book, its `.sdr` folder and its
thumbnail.

The `.sdr` folder is the part worth knowing about: it holds your **reading position,
highlights and page numbers** for that book. Removing the book takes those with it —
that is the point, and it's also why a book you delete and later re-add starts from
the beginning. A book whose `.sdr` folder is still being read by another copy on the
device keeps it.

Over MTP the sidecar can't be deleted at all (Calibre's driver offers no way to reach
it), so the book goes and the sidecar stays. The run says so with a warning rather
than pretending otherwise.

Some things are never removed, and each is refused individually with a reason rather
than aborting the run: anything the backup doesn't cover (nothing could put it back),
anything under `audible/`, anything under `system/` except its `thumbnails/` folder,
and a purchased `.kfx` book whose DRM assets nothing on this machine could
reconstitute.

### `--yes` is required for anything that deletes or overwrites

```bash
media-tools ebook kindle remove "documents/en/Book.azw3"        # plans; changes nothing
media-tools ebook kindle remove "documents/en/Book.azw3" --yes  # actually deletes
```

- **`remove`** without `--yes` lists what it would take and writes nothing at all —
  not to the device, not a backup, not a journal entry. The plan *is* the dry run,
  which is why `remove` has no separate `--dry-run` flag. `remove` never means
  "remove everything": it needs a device path, `--match TEXT` or `--asin ID`.
- **`sync --delete-extras`** needs `--yes` as well before it deletes anything. Without
  it, the extras are listed in the plan and left alone. Adding books never needs
  `--yes`. Be careful with this one: an "extra" is anything the batch doesn't name,
  *including a book somebody else put on the device*.
- **`restore`** without `--yes` reports exactly what it would put back — hashes
  checked, exactly as a real run checks them — and writes nothing. It only ever writes
  files back; it never deletes. Undoing a removal restores the books; undoing an
  *addition* restores nothing, because the snapshot that protected it was taken before
  those files existed. Taking an added book off again is `remove`'s job.

Every run that changed the device records an operation id, so a single operation can
be undone on its own:

```bash
media-tools ebook kindle restore --op <ID> --yes
```

### Cover thumbnails, and the Colorsoft limitation

`media-tools ebook kindle thumbnails` installs a cover for every book on the device
that lacks one, using the covers `ebook build` already resolved (or the cover embedded
in the book itself).

**On a Colorsoft — and newer models — this does not work, and cannot.** Those devices
accept the write and then silently discard the file; it's how they're built, not a
bug in this tool and nothing you did wrong. media-tools verifies the write afterwards
and reports those books as skipped rather than claiming a success that isn't there.
The run still succeeds. On older models, thumbnails install normally.

A book can also come back without a cover simply because there wasn't one to use —
nothing in the library cache, nothing embedded in the book, or a cover that couldn't
be resized.

### First time with a real Kindle

Large parts of this — everything MTP-specific, and the eject path — have been built
and tested against a simulated device, not a real one. `docs/kindle-first-run.md` is
the checklist to work through the first time an actual Kindle is attached: read-only
commands first, then a backup, then a single book added, then a single book removed
and restored. It is explicit about what has never run against real hardware.

## Where output goes

The output root is chosen in this order:

1. `-o`/`--output-dir` on the command line.
2. the `MEDIA_TOOLS_OUT` environment variable.
3. the checkout's own `media/` folder, if this `media-tools` command was installed
   editable (`pip install -e .`, the clone+venv path above) from a git checkout — this
   is resolved from where the installed package's code lives, **not** from your current
   directory, so it applies no matter which folder you run the command from.
4. otherwise `./media`, relative to the directory you run the command from (this is
   what a pipx/uv install falls back to, since it isn't editable).

Every run writes into a batch folder under that root: `<root>/<batch>/`. The batch name
is either the one you give with `-b`/`--batch NAME`, or, when you don't give one, a
deterministic 8-character hash of the task, its effective options and its inputs — so
the exact same command always lands in the exact same batch, and a re-run resumes it
(already-produced outputs are skipped, not redone, unless you pass `--force`). `.cache/`
and `_kindle/` directly under the root are reserved for internal use and are never
treated as your input.

Every batch carries a `run.json` recording the status and reason of every item ("done",
"skipped", "failed" or "pending", plus why). `media-tools status` reads it for you
instead of you parsing it by hand:

```bash
media-tools status            # list every batch under the output root
media-tools status <batch>    # one batch's detail: failed items, pending items, counts
```

## Chaining tasks

Tasks compose through the filesystem: point the next task at the previous one's batch
folder.

```bash
media-tools download "https://example.com/lecture" --batch lecture
media-tools convert media/lecture --to mp3 --batch lecture-mp3
```

(`media/lecture` assumes the default output root from a clone; adjust it to wherever
`-o`/`MEDIA_TOOLS_OUT` points on your machine — see "Where output goes".)

## Troubleshooting

- **YouTube extraction fails, or a signature/format error** — `download` needs the JS
  runtime that ships with `yt-dlp[default,deno]`. Run `media-tools doctor` and check the
  `deno-runtime` line; if it's missing, reinstall (`pip install -e .`) or run
  `media-tools doctor --update`.
- **ffmpeg not found, or "found at ... but it did not run"** — ffmpeg ships with the
  `imageio-ffmpeg` dependency; reinstall the package. media-tools never shells out to
  `ffprobe` (the bundled ffmpeg build doesn't include one), so a missing system
  `ffprobe` is never the cause.
- **`ebook build` fails with "no OpenRouter API key available"** — its LLM-assisted
  cleanup needs a key (see "Building an ebook library"), or pass `--no-llm` to skip it.
  `media-tools doctor` only reports whether a key resolves, never its value.
- **`--op-item NAME` is slow to fail** — resolving a named item tries up to six field
  labels, each a real `op` call; a name that never resolves can take up to ~3 minutes
  in `ebook build` itself. `media-tools doctor --op-item NAME` uses a much shorter
  per-call timeout and stops at the first one that times out (rather than a plain empty
  result), so the health check itself never hangs anywhere near that long — but the
  real `ebook build`/`convert`/etc. run still uses the full timeout.
- **Calibre missing** — needed by `ebook build`, by `convert` for ebook formats, and
  by `ebook kindle` against an MTP Kindle: `brew install --cask calibre`.
- **`ebook kindle` says no Kindle found** — plug it in over USB and unlock the screen.
  A 2024-or-later model (or a Scribe) shows no disk at all; that's expected, it speaks
  MTP and needs Calibre installed. `media-tools doctor` reports the device, and — only
  when the one it finds speaks MTP — whether Calibre's driver is there.
- **`ebook kindle` exits 3 before doing anything** — the mandatory pre-write backup
  failed, so nothing was attempted. Exit 3 means "a precondition was not met"; the one
  command that exits 1 instead is `ebook kindle backup` itself, where the snapshot is
  the work being asked for.
- Run `media-tools doctor` any time — it checks all of the above and gives an
  install/upgrade hint for anything missing.
