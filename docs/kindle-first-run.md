# Kindle first-run checklist

The order to do things in the first time a real Kindle is attached to this tool, and
what to check at each step before going on to the next.

Read the next section first. It is not a disclaimer: it is the reason this checklist
starts read-only and ends with a deletion you undo.

## What has never run against real hardware

`media-tools ebook kindle ...` is covered by a large offline test suite, and every one
of those tests drives a simulated device — a directory shaped like a mass-storage
Kindle, or a fake helper standing in for Calibre. **Nothing here has ever touched a
real Kindle**: not the MTP half, not the mass-storage half, not `eject`. The suite
passing with nothing plugged in is the point of the suite, and it is not evidence about
hardware; a `chmod`-ed directory is not a device that was unplugged mid-write.

The MTP half is the more exposed of the two, because it also depends on shapes that
were read off a locally installed Calibre 9.15.0 by introspection
(`inspect.signature`, `dis`) rather than run: the signatures and constants are real,
the runtime behaviour is not verified. The mass-storage half is ordinary filesystem
work and its failure modes are better understood — but "better understood" is not
"observed", and the checklist below treats both the same way.

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
2. **MTP write atomicity is worse than this used to say.** Reading Calibre's own
   `devices/mtp/unix/driver.py` settled the three questions, and one answer is worse
   than the guess it replaces. `put_file` takes `replace=True`, and when a file of the
   same name exists it **deletes it first** (`delete_file_or_folder`) and only then
   uploads. So an interrupted REPLACE does not leave a short file at the real name —
   it can leave **nothing at all**, the previous copy already gone. There is still no
   staging or rename primitive; the write goes straight to the final name. A failed
   upload raises a generic `DeviceError("Failed to upload file named: …")` that does
   **not** distinguish a full device from a cut transfer. `add`'s verify stage, which
   re-reads the size off the device and fails the book as `short_write`, remains the
   only thing that catches a bad write — and it cannot catch the deleted-then-not-
   replaced case, because there is no file left to measure.
3. ~~**`fonts/` and a root `My Clippings.txt` are unverified guesses.**~~ **Settled on
   hardware.** `fonts/` exists at the root of a real Kindle, as assumed. The root
   `My Clippings.txt` was wrong: the real file is `documents/My Clippings.txt` (with a
   `My Clippings.sdr` beside it), which `documents/` already puts in scope, so it is
   backed up. The root entry is kept because it never matches on this firmware and
   costs nothing, while removing it would silently lose the file on firmware that does
   put it at the root. The same real device also shows two root entries the scope does
   not mention: `voice/` and `.active_content_sandbox/`, neither of which is book
   content and neither of which is collected.
4. ~~**`eject` has never run against the real binaries.**~~ **The macOS mass-storage
   path has now run.** `sync`, then `diskutil info -plist <mount>` to read
   `ParentWholeDisk`, then `diskutil eject <disk>`: exit 0 in 1.1s, the volume gone
   from `/Volumes`, and the whole disk gone from `diskutil list` rather than merely
   unmounted. `status` afterwards exits 3 with no device, which is correct. The plist
   key and the command shapes were written from documentation and turned out right.
   **The Linux path is still unexecuted** -- `udisksctl unmount -b` plus `udisksctl
   power-off -b` -- as is the busy-retry on both platforms, which needs a device that
   refuses the first attempt.
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

## What a first real run confirmed (2026-09-24, mass storage)

A real Kindle was attached and steps 1-3 were run, plus an `add`/`remove` round trip.
This is evidence, not a promise about other models -- it was ONE device, in mass-storage
mode. Everything MTP in the list above is still unverified.

What held:

- Detection, `status`, and `scan` (999 books) work, and `scan` is stable across a
  disconnect/reconnect.
- **Incremental backup does what it claims.** First snapshot: 2450 files, 1.08 GB
  copied, 84s. Second, immediately after: the same 2450 files, **0 bytes copied,
  1.08 GB hard-linked**, 1.2s, costing 988 KB on disk instead of 1.0 GB.
- **The backup scope held against a real `system/`.** The device's `system/` carries
  `btlogs`, `Search Indexes`, `CloudIndices`, `fmcache`, `kf8`, `grok_thumbnails` and
  more; exactly `system/thumbnails` was collected (1161 files on the device, 1161 in
  the snapshot) and nothing else. `audible/`, `voice/` and `.active_content_sandbox/`
  were not collected.
- **`add` then `remove` is a faithful round trip.** A book written to the device was
  byte-identical to its source (same sha256), `add` run a second time SKIPPED it with
  `reason: exists` (matched by EXTH 113, not by name), and `remove` left the library at
  exactly the count it started with. Both took their mandatory backup first.
- A book with no cover reports `no_cover` rather than failing.
- **`eject` works on macOS mass storage.** It ejects the whole disk, not just the
  mount: `diskutil list` stops showing the device entirely.

What this run changed in the code:

- `.TemporaryItems` had to be added to `VOLUME_LITTER`. macOS creates it on removable
  volumes and denies `scandir` on it, so `scan` aborted on EVERY mass-storage Kindle on
  macOS -- reported as `device_not_found` on a mounted device.
- `find_device` now RAISES `MultipleDevicesFound` when more than one Kindle is present
  instead of taking the first. A mass-storage Kindle reports no serial, so backups are
  keyed by the volume name, and every Kindle is named "Kindle": picking silently could
  write one device's snapshot into another's directory.

Still unverified after this run: everything MTP, `eject` on Linux, and the busy-retry on either platform.

## What research settled about MTP, with no MTP device available (2026-09-24)

The owner has no 2024-or-later Kindle or Scribe, so the MTP half **cannot be verified
by running it** and this is as far as it goes. What follows was read from Calibre's
source on `master` and from libmtp's issue tracker — it is better evidence than the
`inspect.signature`/`dis` introspection the code was written from, because it is the
actual implementation rather than a shape, but **nothing here was executed**, and the
installed Calibre is 9.15.0 rather than `master`.

- **`put_file` replaces by deleting first.** See item 2 above — this is the finding
  that changes a risk rather than confirming one.
- **No staging primitive exists.** The assumption the code was built on is correct.
- **A failed upload is one generic `DeviceError`.** The message is
  `"Failed to upload file named: <name> to <path>"`, with no separate signal for a
  full device. The exit-code classification's substring list, which this project's own
  comments call "an outright guess", can at least be anchored to that string now.
- **`current_serial_num` is an attribute, not a method**, assigned during `open()`.
  The helper reads it with `getattr`, which is right.
- **`get_device_uid` does not exist in the unix driver.** The documented fallback
  therefore never fires on macOS or Linux. The helper calls it through
  `getattr(device, "get_device_uid", None)`, so this degrades rather than raising —
  but it means a device whose `current_serial_num` is unset proceeds **unguarded**,
  exactly the case item 1 says to decide about before running anything destructive.
- **`eject()` is not just closing a session.** It adds the device to `ejected_devices`
  and calls `post_yank_cleanup()`, which nulls `dev`, `_filesystem_cache` and
  `current_friendly_name`.
- **The target device may not enumerate at all.** libmtp issue #231 reports a Kindle
  Paperwhite 2024 on firmware 5.17.0 (VID `0x1949`, PID `0x9981` — the same PID this
  project's own MTP test uses) as UNKNOWN to libmtp 1.1.21, failing with
  `LIBMTP PANIC: Unable to initialize device` and a busy libusb interface where GVFS
  or KDE's MTP handling holds it. Detection fails outright there, before any file
  operation. Treat "the MTP path is untested" as covering the possibility that it does
  not reach the device at all on some stacks.

**Everything in the list above is still unverified by execution**, and should stay
unverified in this document until someone runs it against real hardware.

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
- `serial`, `model_hint` and `free_space` — **`free_space` is reported in both modes;
  `serial` is not.** On the first real mass-storage Kindle, `serial` came back `null`
  and `model_hint` was `"Internal Storage"` — the volume's name, not a model. Device
  identity therefore rests on `device_key`'s mount-name fallback there, not on a
  serial. Do not treat a null serial on mass storage as a detection failure,
- `held_by` — **MTP only**: it reads `"calibre_gui"` when Calibre's GUI has the device,
  and is `null` on a mass-storage Kindle even with Calibre open, because mass storage
  has no single-holder lock to report on,

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
it does not, hashed from the first of the mount name, the `model_hint` the device
reports over USB, or `"<mode>:<product id>"` that is available. **Check which you got
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

If it fails, the error code says which kind of failure it was. There are three, and
`device_busy` means two different things depending on the mode:

- **`device_busy`, mass storage** — the volume is still in use after the one retry.
  Close whatever is reading it and run `eject` again.
- **`device_busy`, MTP** — Calibre's GUI is running, and an MTP device allows exactly
  one holder. That check runs before *every* MTP invocation, not just this one, so the
  same code appears for `status`, `scan` and everything else while Calibre is open.
  Close Calibre.
- **`eject_failed`** — the platform's eject tool ran and refused for a reason it did
  not call "busy". The message carries what `diskutil`/`udisksctl` itself said; that
  text is the actionable part. Record it here if you hit one.
- **`dependency_missing`** — a platform binary (`diskutil`, `udisksctl` or `sync`) is
  not there. This is the only one of the three that means "install something".

All three exit 3, and the device is untouched in every one of them. Worth deliberately
provoking the busy case once, by leaving a file manager open on the volume (or Calibre
open, over MTP), since none of these branches has ever run for real.

## When something does not match

Correct the code and the FIRST-RUN VERIFICATION block in
`src/media_tools/integrations/kindle_mtp.py` rather than working around it further
downstream. Record the real error text a device produces, especially for the
exit-code classification, whose substring list is a guess.

Do not delete the backup taken in step 3 until everything above has been checked. It
is the thing standing behind every write on this list.
