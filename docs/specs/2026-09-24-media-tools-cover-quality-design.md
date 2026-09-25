# media-tools: cover quality repair — design

**Date:** 2026-09-24
**Status:** approved by the owner, section by section, ahead of planning
**Scope:** a new `ebook fix-covers` subcommand that repairs covers in an existing
folder of books, plus stale-thumbnail invalidation on the Kindle write path.

## Why this exists

A real Kindle showed a library where covers were repeated across unrelated books and
rendered cut in half. Measuring the source library of 5407 files found three distinct
defects that the current code cannot see:

- **1531 books across 713 groups shared a cover with a different title.** The largest
  group was 39 Goosebumps volumes carrying one 100x99 placeholder.
- **79 covers were landscape** (height <= width). A Kindle tile is portrait, so these
  render cropped. One was 358x29.
- **240 covers were portrait but under 250px wide**, too small for the tile.

`tasks/ebook/covers.py` validates exactly one thing today, `_MIN_COVER_BYTES = 1000`.
A 358x29 JPEG is well over 1000 bytes, so every defect above passes untouched.

The repair itself was carried out manually in the session that produced this design.
The numbers quoted throughout are measured, not estimated.

## Decisions taken

| Question | Decision |
| --- | --- |
| Where does the logic live | A new subcommand over an existing folder. The `build` stage is unchanged. |
| Name | `ebook fix-covers`, not `ebook covers`, because that name already means "resolve a cover during conversion" |
| Drop a bad cover when no replacement is found | Yes, by default. A generic cover beats a 29-pixel strip. |
| Safety gate | Plans only; writes nothing without `--yes`, following `ebook kindle remove` |
| Kindle-side work in scope | Stale thumbnail invalidation only. AppleDouble cleanup is out of scope. Self-review found this needs a book-replacement path that does not exist; see "A prerequisite that does not exist yet". |
| Metadata | Fill **empty** fields only, behind its own flag, never overwriting an existing value |

## Architecture

Three new modules, each answering one question and testable on its own.

### `tasks/ebook/cover_quality.py` — is this cover usable?

Pure. No network, no Calibre. Takes image bytes, returns a verdict and a reason.

Rules, all derived from measured files:

- valid JPEG: starts `FF D8`, ends `FF D9`
- portrait: height > width
- at least `min_width` pixels wide (default 250)
- rejects the 1x1 GIF that Open Library returns with HTTP 200 when it has no cover

That last rule matters most: it is the only defect that arrives disguised as success.

### `integrations/covers_online.py` — what is the best cover for this title?

Queries Google Books and Open Library, scores every candidate by the similarity
between the requested title and the returned one, and returns the best above the
threshold, or nothing.

Sources are injected, the way `detect.find_device` takes `usb_lister`, so the module
is testable without touching the network.

It owns two behaviours that were found empirically:

- **Author-free retry.** The author field is frequently junk (`me`,
  `Crais, Robert - Joe Pike 02`). Constraining on it returns zero results, while the
  unconstrained query returns the right book at similarity 1.00. A query that comes
  back empty is retried without the author. This moved a sample from 58% to 75%.
- **Title variants.** `Wheel of Time 06 - Lord of Chaos` is also tried as
  `Lord of Chaos`; `A Cidade do Sol - Khaled Hosseini` as `A Cidade do Sol`; and the
  filename is tried when the embedded title is unusable
  (`Microsoft Word - White Witch.doc`).

The similarity threshold defaults to 0.72. It exists because Open Library returns the
nearest match rather than the exact one: a query for *The Last Olympian* returned
*The Lightning Thief*, a different book in the same series. Scoring rejects it.

### `tasks/ebook/fix_covers.py` — the command

Walks the folder, classifies, decides, acts. Shared-cover detection lives here because
it is a property of the whole library, not of one book: it can only be computed after
every book has been read.

Writing reuses what exists: `exth.read_records` to read, Calibre's
`ebook-meta --cover` to write, and a surgical removal that marks EXTH 201 and 202 with
`0xFFFFFFFF`, which leaves file size and `book_id` untouched.

The boundary this creates: `cover_quality` does not know what a book is,
`covers_online` does not know what a library is, and `fix_covers` does not speak HTTP.

## Behaviour

Two passes, because the shared-cover defect is invisible book by book.

**Pass 1, read only.** For each file read `book_id`, title, author, ASIN and the
embedded cover. Classify into: good cover, no cover, bad geometry, shared cover.

Books carrying an ASIN in EXTH 504 are store purchases and leave the list here, before
any decision. This rule protected 13 books during the manual run.

**Pass 2, act.** For each defective book, query the sources. A cover that passes
`cover_quality` is written. When none is found, the existing cover is removed. A book
with a good cover is never touched.

### CLI

```
media-tools ebook fix-covers <folder> [--yes] [--fill-metadata]
    [--min-width N] [--similarity N] [--op-item NAME] [--match TEXT]
```

| Flag | Effect |
| --- | --- |
| `--yes` | Without it nothing is written |
| `--fill-metadata` | Fills year, publisher and ISBN only where the field is empty |
| `--min-width N` | Default 250 |
| `--similarity N` | Default 0.72 |
| `--op-item NAME` | 1Password item holding the API key |
| `--match TEXT` | Restrict to a subset, as `ebook kindle thumbnails --match` does |

The key comes from `GOOGLE_BOOKS_API_KEY`, accepting a literal value or an
`op://vault/item/field` reference, resolved through the same path
`openrouter.resolve_key` already uses. Without a key the command still runs on Open
Library alone and says coverage will be lower. This is not hypothetical: the manual
run exhausted the anonymous per-IP quota after about a thousand queries and every
lookup then returned HTTP 429.

### Output

The project contract holds: JSON Lines on stdout, one `item` event per book, human
text on stderr, `result` last.

New reason codes must be added to the closed registry in `core/events.py`, since
emitting an unregistered code raises: `cover_shared`, `cover_too_small`,
`cover_not_portrait`, `cover_dropped`, `no_cover_source`.

### Metadata, when `--fill-metadata` is given

Fills an **empty** field only. Never overwrites an existing value, and never touches
title, author or `book_id`.

The year carries a caveat that belongs in the help text: the date the sources return
is the date of *that edition*, not of the work. A digital reprint of Machado de Assis
reports 2018, not 1881, and a Brazilian edition of Nietzsche reports the translation's
year. This is why the field is opt-in and why only empty fields are filled.

A wrong cover is noticed immediately, by looking. A wrong year is not.

## Error handling

**Quota exhaustion is the most dangerous failure, because it disguises itself as a
result.** When Google returned 429, every subsequent book was recorded as "not found"
without anything having examined it. The command must distinguish *this source refused
to answer* from *this source answered that it has nothing*. On a 429, or a run of
transport failures, it stops and reports how many are pending rather than continuing
to record failures.

**An HTTP 200 is not a success.** Open Library returns a 43-byte 1x1 GIF when it has
no cover. `cover_quality` rejects it on the magic number. This is the same principle
the project already learned in `_walk`: an empty listing must mean "nothing is there",
never "I could not look".

**A dirty field silently zeroes the query.** Covered above by the author-free retry.

**No write touches the original file.** Copy, write to the copy, verify, then replace
by rename. Verification requires four things: an intact `BOOKMOBI` header with record
offsets inside the file, a cover that actually changed, an unchanged `book_id`, and a
matching file size when the operation is a removal. Any one failing discards the copy
and leaves the original alone. Over a thousand writes were made this way during the
manual run without a single corrupted file.

**An identical cover is neither success nor failure.** It happened 47 times: the
source returned exactly the image the book already carried. It gets its own status and
the file is not touched.

**The log is the resume key.** Every decided book is recorded, so an interrupted run
resumes without repeating work. This preserved 656 books when the manual run was
stopped on quota.

## Kindle side: stale thumbnail invalidation

A Kindle caches thumbnails under `system/thumbnails/`, named after the book's EXTH 113
id. Because every write here preserves `book_id`, the firmware does not know the file
changed and keeps showing the old cover.

Replacing a book on the device must therefore also refresh its cached thumbnail: write
a new one when the book has a cover, remove the stale one when it does not. Without
this, fixing covers in a folder and syncing changes nothing on screen.

### A prerequisite that does not exist yet

Self-review found that this item, as approved, cannot be built: **no command replaces
a book that is already on the device.** `ebook kindle add` skips an existing book with
`reason: exists`, matched on EXTH 113. `ebook kindle sync` adds only "what the device
lacks". The manual run that produced this design copied 281 files, and later another
113, onto the device by hand, outside the tool.

So thumbnail invalidation has no host. The smallest change that makes the approved
scope coherent is a flag on the existing `add`:

```
media-tools ebook kindle add --update <books>
```

Without it, `add` behaves exactly as today. With it, a book already on the device
whose content differs is replaced rather than skipped, and its cached thumbnail is
refreshed in the same pass.

This reuses `add`'s existing pipeline rather than inventing a second write path: the
mandatory backup, the write verification, and the thumbnail installation are already
there. A new command would duplicate all three.

Two properties this must keep, both observed during the manual run:

- **Replacement is matched on `book_id`, never on filename.** The device filename and
  the source filename frequently differ, and the `.sdr` sidecar holding reading
  progress is keyed on the device filename, so the device's name is preserved and only
  the content is replaced.
- **A store purchase is never replaced.** A book carrying an ASIN in EXTH 504 is
  skipped even under `--update`.

This is a scope addition, discovered after the four design sections were approved. It
is flagged here rather than folded in silently, and is the one item in this spec the
owner has not yet seen.

AppleDouble (`._*`) cleanup is explicitly **out of scope**. The project already
filters those names through `is_volume_litter`, so they are inert clutter.

## Testing

The principle the project already holds: a test that supplies the exception the
production path cannot raise proves nothing. Each test reproduces the real condition.

- **`cover_quality`** is pure, so it is both the easiest and most valuable to test.
  Synthetic bytes for each rejection: a valid portrait JPEG passes; the 1x1 GIF is
  rejected on its magic number; a 358x29 image is rejected as not portrait; a 200x297
  image is rejected on width; a JPEG with no end marker is rejected. Every one of
  these came from a file observed in the library.
- **`covers_online`** is tested without network, since sources are injected. The
  highest-similarity candidate wins even when it comes from the other source; nothing
  below the threshold is accepted; an empty authored query is retried without the
  author; title variants are tried in order. *The Last Olympian* vs *The Lightning
  Thief* is a named regression test.
- **`fix_covers`** needs a fake library on disk: a fixture with a handful of books
  covering the four states, including two different titles sharing one cover. It also
  asserts that a book with an ASIN is skipped, and that without `--yes` no file on
  disk changes, comparing hashes before and after.
- **Quota stop** has its own test, with a source that returns 429: the command must
  stop and report pending, not mark everything as not found.
- **Writing** is tested against a real minimal MOBI built in the fixture, so the
  surgical EXTH 201/202 removal is verified for real: same size, intact header,
  preserved `book_id`, cover gone. Otherwise the test would only be checking the
  offset arithmetic against itself.

No test touches the network, a real Calibre or a device, so all of them run in the
default `not network and not llm and not device` gate. Tests that need `ebook-meta` to
write are marked and excluded, as the project already does for its Calibre-dependent
suite.

## Out of scope

- Strengthening the `build` pipeline's `covers` stage. The shared modules make it a
  later import rather than a duplication, but it is not done here.
- AppleDouble cleanup on the device.
- EPUB and PDF covers. This command covers the MOBI family (`azw3`, `azw`, `mobi`,
  `prc`), which is what a Kindle reads.
