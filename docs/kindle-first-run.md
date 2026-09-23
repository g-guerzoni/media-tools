# Kindle first-run checklist

The order to do things in the first time a real Kindle is attached to this tool, and
what to check at each step before going on to the next.

Read the next section first. It is not a disclaimer: it is the reason this checklist
starts read-only and ends with a deletion you undo.

## What has never run against real hardware

`media-tools ebook kindle ...` is covered by a large offline test suite, and every one
of those tests drives a simulated device — a directory shaped like a mass-storage
Kindle, or a fake helper standing in for Calibre. **No line of the MTP device-facing
code has ever run against a real MTP Kindle**, and the mass-storage eject path has
never run against the real platform binaries either. The shapes below were read off a
locally installed Calibre 9.15.0 by introspection (`inspect.signature`, `dis`), so the
signatures and constants are real; the runtime behaviour is not verified.

`src/media_tools/integrations/kindle_mtp.py` carries the full list in its FIRST-RUN
VERIFICATION comment block, and that block is the thing to correct as each item is
confirmed. These are the ones that matter most, worst first:

1. **The MTP serial match has never fired.** Calibre's MTP driver is a *general* MTP
   driver, not a Kindle driver, and this code path deletes files. The helper reads the
   device's serial (`current_serial_num`, falling back to `get_device_uid()`) and
   refuses to run when it does not match the serial USB detection reported. Nobody has
   ever seen that comparison succeed. Three things to confirm, in this order:
   - at least one of the two reads yields a value at all;
   - it is the *same string* `detect.find_device` reports from the USB layer (if the
     two spell it differently, every invocation refuses to run);
   - when neither yields anything, the helper proceeds **unguarded** and says so in
     `device.checked` — decide whether that is acceptable *before* running any
     destructive command, because unguarded means "whatever MTP device answered".
2. **MTP write atomicity is unknown.** There is no known staging or rename primitive
   in Calibre's MTP driver, and none was invented: `put_file` writes straight to the
   final name. An interrupted transfer can therefore leave a short file sitting at the
   real name. Nothing detects that at write time — the only thing that catches it is
   `add`'s own verify stage, which re-reads the file's size off the device afterwards
   and fails the book as `short_write`. Confirm what `put_file` actually leaves behind
   when a transfer is cut, whether it replaces a same-named file by default, and what
   it raises when the device is full.
3. **`fonts/` and a root `My Clippings.txt` are unverified guesses.** The backup scope
   includes `fonts/` as the user-font folder, and `My Clippings.txt` at the device
   root. The only witness for the root copy is this project's own test fixture, not
   firmware — a real Kindle most likely keeps it in `documents/`. Both are backed up,
   which over-collects; that is the safe direction for a backup, and the scope is
   worth correcting once a real device says which is true.
4. **`eject` has never run against the real binaries.** The mass-storage path runs
   `sync`, then `diskutil info -plist <mount>` to read `ParentWholeDisk` and
   `diskutil eject <disk>` on macOS, or `udisksctl unmount -b` + `udisksctl power-off
   -b` on Linux. The command shapes, the plist key and the busy-retry were all written
   from documentation, never executed against a mounted device.
5. **Only Calibre's *unix* MTP driver was ever read**, and only by disassembly.
   Windows uses a different driver module and may raise different exception types for
   the same situations — in particular the "folder is not there" branch, which decides
   whether a missing prefix reads as an empty listing or as a failure. Do not trust
   the listing behaviour on Windows without re-checking it there.

Everything else still open is in that same comment block: the driver import path
(verified to import under Calibre 9.15.0 on macOS only), the open sequence past
`detect_managed_devices`, the private `_main_id` storage root, `ensure_parent`'s
sentinel component, the narrowness of the cached device tree (which is why a `.sdr`
sidecar cannot be deleted over MTP at all), the free-space return shape, the
exit-code classification — whose substring list is an outright guess — and whether
closing the session is really the whole of an MTP eject.

## Before plugging anything in

```bash
media-tools doctor
```

Two lines in the report are about this feature:

- `kindle-device` — whether a Kindle is connected, and in which mode. Having none
  attached is reported `ok`, not as a problem: a machine with no Kindle is not a broken
  machine. An MTP Kindle that **Calibre's GUI is holding** IS flagged, because there is
  something to do about that — an MTP device allows exactly one holder, so every
  `ebook kindle` command would fail `device_busy` until Calibre is closed. (Mass
  storage has no such lock and is never flagged this way.)
- `kindle-mtp-driver` — whether Calibre's MTP driver imports inside Calibre's own
  interpreter. It is only probed when the line above found an **MTP** Kindle; otherwise
  it says `not probed`, because nothing else on the machine needs the driver.

Neither can ever report `missing`, and neither moves `doctor`'s exit code. Run this
again with the Kindle plugged in — that is when both lines start saying something.

## 1. `status` — read-only

Plug the Kindle in, unlock it, and:

```bash
media-tools ebook kindle status --json
```

Expected: exit 0, and a final `result` event whose `data.device` reports

- `mode` — `"mass_storage"` or `"mtp"`,
- `backend` — the matching `"mass_storage"` / `"mtp"` literal,
- `serial`, `free_space`, and `held_by` — **MTP only**: it reads `"calibre_gui"` when
  Calibre's GUI has the device, and is `null` on a mass-storage Kindle even with
  Calibre open, because mass storage has no single-holder lock to report on,

plus `data.backup` with `last: null` (no snapshot yet), `abandoned_partials: []` and
`header_cache_bytes: 0`.

**How to tell the two modes apart without reading JSON:** a mass-storage Kindle mounts
as a disk and shows up in the file manager; a 2024-or-later model (or a Scribe) shows
no disk at all and speaks MTP. `status` says which one it decided on, and detection
never trusts a model table — it looks for the mount.

Check first: does `mode` match what the machine is actually showing? Does `serial`
look like the serial on the device's own settings screen? On MTP, does the command
return at all — that is the serial-match guard above firing for the first time.

## 2. `scan` — still read-only

```bash
media-tools ebook kindle scan --json
```

Expected: exit 0, one `item` event per book (all `done`), and `data.books` with one
entry per book carrying `path`, `book_id`, `title`, `author`, `language`, `size`,
`mtime`, `has_sdr` and `has_thumbnail`.

What to check:

- **Titles come from the books, not their filenames.** A book whose filename and
  embedded title disagree must report the embedded one.
- A book with no EXTH 113 id reports `book_id: null` *and* a `book_id_missing`
  warning on its item. A book whose records could not be READ at all reports
  `book_id: null` with `book_id_unreadable` instead — permanent versus transient, and
  worth telling apart: the second means that book looks absent to every id comparison.
  A book with an id but no readable title reports `title: null` and **no** warning —
  that is not an error.
- `has_sdr` means the book has `.sdr` *content*; an empty `.sdr` directory reports
  `false`.
- Over MTP, this is the first command that pulls whole books across. It caches each
  one under the output root; a second `scan` should be much faster and should not
  re-fetch anything unchanged.

## 3. `backup` — the first write, and it is on the host only

```bash
media-tools ebook kindle backup --json
```

Expected: exit 0, exactly one `item` event (`done`), and `data.snapshot` describing
the snapshot just written. It lands under

```
<output root>/_kindle/<serial>/backups/<UTC timestamp>/
```

with a `manifest.json` and a `files/` tree mirroring the device's own paths.
`<serial>` is the device's own serial when it reports one — and `unknown-<8 hex>` when
it does not, hashed from the first of the mount name, the model hint or
`"<mode>:<product id>"` that is available. **Check which you got
before going looking for a serial-named folder**; `status` above reported the serial,
and a `null` there means the directory is the `unknown-` one.

What to check:

- The snapshot directory exists and is **not** named `*.partial` — a snapshot is
  built under a partial name and renamed only once complete, so a partial one means
  the run did not finish.
- Spot-check that a book in `files/` opens and is the right size.
- Run it a second time. The second run should hard-link most files from the first
  rather than re-transferring them: `bytes_copied` small, `bytes_linked` large. On a
  mass-storage Kindle this also exercises the daylight-saving clause, which forgives a
  whole-hour mtime shift rather than re-copying the whole library.
- `media-tools ebook kindle status --json` now reports that snapshot under
  `data.backup.last`.

**Snapshots are never pruned.** Nothing in this tool deletes a backup; that disk usage
is yours to manage.

## 4. `add` — one book, dry run first

Pick **one** small book that is not already on the device.

```bash
media-tools ebook kindle add path/to/one-book.azw3 --dry-run --json
```

Expected: exit 0, stages `detect` then `plan` only, **no backup taken**, and one item
reported `pending` with the `device_path` it would land on —
`documents/<lang>/<name>`, or `documents/unknown/<name>` when no usable language code
is available.

Then the real thing:

```bash
media-tools ebook kindle add path/to/one-book.azw3 --json
```

Expected: exit 0, stages `detect`, `backup`, `plan`, `copy`, `thumbnails`, `verify`,
one item `done`, and `data.operation` carrying the journalled operation id. **The
backup is a precondition**: if it fails, nothing is copied and the run exits 3.

What to check:

- The book is on the device, in `documents/<lang>/`, and opens on the Kindle.
- Run the same `add` again. It must report `skipped` with `detail` starting
  `exists:` — matched by the book's own EXTH 113 id, never by filename.
- If the source is an `.epub` or `.pdf` (no EXTH id), the second run recognises it by
  provenance instead — the journal records the exact bytes that were sent, and the
  device still holds them at that size.
- Over MTP especially: confirm `verify` really compared sizes. A short write is the
  MTP failure mode item 2 above is about, and this stage is the only thing that
  catches it.

## 5. `remove` — one book, plan first, then undo it

Use the book that was just added.

```bash
media-tools ebook kindle remove "documents/<lang>/<name>" --json
```

Expected: exit 0, stages `detect` and `plan` only, the book reported `pending`, and
**nothing written at all** — no deletion, no backup, no journal entry. Without
`--yes`, the plan *is* the dry run.

Then:

```bash
media-tools ebook kindle remove "documents/<lang>/<name>" --yes --json
```

Expected: exit 0, stages `detect`, `backup`, `plan`, `remove`, the book `done`,
`data.removed` listing everything that went, and `data.operation` carrying the
operation id.

**A removal takes three things together**: the book, its `.sdr` folder (reading
position, highlights, page numbers) and its thumbnail. Over MTP the `.sdr` folder
cannot be deleted at all — Calibre 9.15 has no delete-by-name and its cached device
tree omits `.sdr` folders and everything under `system/` — so the book is still
reported `done`, with a `sidecar_not_removed` warning. That is the honest answer, not
a failure: confirm on real hardware that the sidecar really is still there afterwards.

Now undo it, using the id from `data.operation`:

```bash
media-tools ebook kindle restore --op <ID> --json        # plan; writes nothing
media-tools ebook kindle restore --op <ID> --yes --json  # actually put it back
```

Expected: one item per *file* (the book, its sidecar, its thumbnail — not one per
book), each hashed against the manifest before a byte is written. A file that no
longer hashes to what the manifest recorded is refused, not restored.

Check that the book is back, and that opening it resumes where it left off — that is
the `.sdr` folder having come back with it.

Note the asymmetry: `restore` only ever *writes files back*. Undoing an `add` restores
nothing and reports `not_in_snapshot`, because the snapshot that protected that run
was taken before those files existed. Taking an added book off again is `remove`'s
job.

## 6. `thumbnails` — and the Colorsoft limitation

```bash
media-tools ebook kindle thumbnails --dry-run --json
media-tools ebook kindle thumbnails --json
```

On a Colorsoft (and newer), expect books to come back `skipped` with reason
`device_rejected` and a `device_rejected_thumbnail` warning: the device accepts the
write and then silently discards it, by design. That is not a failure and the run
still exits 0. On an older model, expect `done`.

A book that reports `no_cover` has one of four causes, and they are not distinguished
in the reason: no cover anywhere, no book id at all, an EXTH id rejected as unsafe to
use as a filename, or a cover that could not be resized.

## 7. `eject`

```bash
media-tools ebook kindle eject --json
```

Expected: exit 0, stages `detect` and `eject`, no items, nothing written to the
device. On mass storage this flushes pending host writes and unmounts the disk; over
MTP it closes the session.

This is the step with the least verification behind it (item 4 above). Check that the
volume really did disappear, that the Kindle says it is safe to unplug, and — over
MTP — that the device is in a clean state afterwards and nothing else on the host is
still holding it.

If it fails, the error code says which kind of failure it was, and `device_busy` means
two different things depending on the mode:

- **Mass storage** — the volume is still in use after the one retry. Close whatever is
  reading it and run `eject` again.
- **MTP** — Calibre's GUI is running, and an MTP device allows exactly one holder. That
  check runs before *every* MTP invocation, not just this one, so the same code appears
  for `status`, `scan` and everything else while Calibre is open. Close Calibre.

`dependency_missing` is the other branch: a platform binary — `diskutil`, `udisksctl`
or `sync` — is not there. Both exit 3, and the device is untouched either way. Worth
deliberately provoking the busy case once, by leaving a file manager open on the volume
(or Calibre open, over MTP), since neither branch has ever run for real.

## When something does not match

Correct the code and the FIRST-RUN VERIFICATION block in
`src/media_tools/integrations/kindle_mtp.py` rather than working around it further
downstream. Record the real error text a device produces, especially for the
exit-code classification, whose substring list is a guess.

Do not delete the backup taken in step 3 until everything above has been checked. It
is the thing standing behind every write on this list.
