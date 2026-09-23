# media-tools

Command-line tools to compress, convert, split, download and organise media and ebooks —
one command, one subcommand per task, replacing five older single-purpose scripts.

`compress`, `convert`, `split`, `download`, `formats`, `status` and `doctor` all work
today. `ebook` is registered (`media-tools ebook --help` works) but not implemented yet
— see "Supported formats" below.

## Install

### Clone + venv (for development)

```bash
git clone https://github.com/g-guerzoni/media-tools.git
cd media-tools
python3.13 -m venv .venv
.venv/bin/pip install -e . --group dev
.venv/bin/media-tools doctor
```

On macOS, install Python 3.13 first with Homebrew: `brew install python@3.13`.

`--group dev` needs pip >= 25.1. On an older pip (a stock 3.11/3.12 venv, most often),
install the dev tools directly instead:

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

- **Python >= 3.11.** On macOS, use Homebrew's `python3.13` (see above).
- **ffmpeg** — nothing to install: it ships with the `imageio-ffmpeg` dependency.
- **A JS runtime for YouTube extraction** — ships with the `yt-dlp[default,deno]`
  dependency (it installs the `deno` pip package, which vendors the Deno binary).
  Some sites need this to decode signature ciphers before `download` can fetch them.
- **Calibre** — needed only by the future `ebook` command. Install with
  `brew install --cask calibre` once that command exists and you need it.

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

`media-tools ebook` is a registered stub for the next piece of work: it exits 3 with
"the ebook task is not implemented yet" rather than pretending to do something.

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
it fetches whatever URL yt-dlp understands — and `ebook` isn't implemented yet.

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

Only `download` accepts a list file today. The future `ebook` command (Plan B) is
expected to add its own for the library-building step, once that task exists.

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
- **`ebook`'s future LLM features need `OPENROUTER_API_KEY`** — export it in your shell
  before using them once they exist. `media-tools doctor` only reports whether it's set,
  never its value.
- **Calibre missing** — only needed by the future `ebook` command:
  `brew install --cask calibre`.
- Run `media-tools doctor` any time — it checks all of the above and gives an
  install/upgrade hint for anything missing.
