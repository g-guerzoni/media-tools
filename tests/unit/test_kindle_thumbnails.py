"""`tasks.ebook.kindle.thumbnails`: the file name a device's own firmware expects for
a book's cover, and `install()`, which writes it.

Nothing here spawns a real device OR a real ffmpeg process — a tiny in-memory
`FakeBackend` stands in for `DeviceBackend`, and `_resize` is monkeypatched the same
way `tests/unit/test_compress_video.py` monkeypatches `run_ffmpeg` for its own engine
tests, per CLAUDE.md's "fast, no real media required" rule for `tests/unit/`. Real
ffmpeg — actual resize dimensions, and the full on-device-extraction pipeline against
a real embedded image — is exercised in `tests/integration/test_kindle_thumbnails.py`
instead.
"""

from __future__ import annotations

import struct
from pathlib import Path

from media_tools.tasks.ebook import exth
from media_tools.tasks.ebook.covers import cache_path
from media_tools.tasks.ebook.kindle import thumbnails

BOOK_ID = "TESTBOOKID0001"
_FAKE_JPEG = b"\xff\xd8\xff" + b"0" * 50  # enough to pass `_looks_like_image`


class FakeBackend:
    """Enough of `DeviceBackend` for `install()`'s own needs: `read` (the on-device
    extraction fallback), `write`, and `exists` (the post-write verification).
    `reject_writes_to` simulates a Colorsoft-style device that accepts a write and
    then silently discards it — `write()` runs, but the path never shows up as
    existing afterward, exactly the divergence `install()` is meant to detect.
    `fail_writes_to` simulates a GENUINE device fault (a full disk, a yanked cable,
    `DeviceWriteProtected`) — the write call itself raises, which is a different
    thing from a clean, silent rejection.
    """

    def __init__(
        self,
        files: dict[str, bytes] | None = None,
        *,
        reject_writes_to: frozenset[str] = frozenset(),
        fail_writes_to: frozenset[str] = frozenset(),
    ) -> None:
        self.files = dict(files or {})
        self.written: dict[str, bytes] = {}
        self.reject_writes_to = set(reject_writes_to)
        self.fail_writes_to = set(fail_writes_to)
        self.read_calls: list[str] = []

    def read(self, path: str, dest: Path) -> None:
        self.read_calls.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(self.files[path])

    def write(self, local: Path, path: str) -> None:
        if path in self.fail_writes_to:
            raise RuntimeError("the device refused the write (simulated fault)")
        if path in self.reject_writes_to:
            return  # accepted, then silently dropped — never lands in `written`
        self.written[path] = Path(local).read_bytes()

    def exists(self, path: str) -> bool:
        return path in self.written


def _stub_resize(source: Path, dest: Path, *, ffmpeg: str, target_height: int = 500) -> bool:
    """Stands in for `thumbnails._resize`: writes a fixed, valid-enough JPEG to
    `dest` without spawning ffmpeg. What `install()`'s own orchestration does with
    the result (write, verify, map to a status) is what these tests are about — real
    resize/decode correctness is `tests/integration/test_kindle_thumbnails.py`'s job.
    """
    Path(dest).write_bytes(_FAKE_JPEG + b"1" * 2000)
    return True


def _mobi_with_image(exth_entries: list[tuple[int, bytes]], image_bytes: bytes) -> bytes:
    """A byte blob shaped like a real MOBI file with THREE PalmDB records: the usual
    header+EXTH record (record0, mirroring `test_kindle_backup.py`'s own
    `mobi_bytes` helper), a text record, and an IMAGE record holding `image_bytes` —
    the MOBI header's own "first image record" field points PalmDB record 2 at it,
    so an EXTH cover/thumb offset of 0 resolves to this record. `image_bytes` need
    only pass `_looks_like_image` (a magic-byte check), not decode as a real
    image — these tests are about the OFFSET MATH, not ffmpeg.
    """
    blob = b"".join(struct.pack(">II", tag, 8 + len(v)) + v for tag, v in exth_entries)
    exth_block = (
        b"EXTH" + struct.pack(">I", 12 + len(blob)) + struct.pack(">I", len(exth_entries)) + blob
    )

    header_length = 232
    record = bytearray(b"\0" * (16 + header_length))
    record[16:20] = b"MOBI"
    record[20:24] = struct.pack(">I", header_length)
    record[0x80:0x84] = struct.pack(">I", 0x40)  # EXTH present
    # The "first image record" field: MOBI-header offset 92, i.e. record0 offset 108.
    record[108:112] = struct.pack(">I", 2)  # PalmDB record 2 is the first image
    record0 = bytes(record) + exth_block

    text_record = b"text record"

    palm = bytearray(b"\0" * 102)  # 78 + 3 record-info entries * 8 bytes
    palm[76:78] = struct.pack(">H", 3)
    r0 = 102
    r1 = r0 + len(record0)
    r2 = r1 + len(text_record)
    palm[78:82] = struct.pack(">I", r0)
    palm[86:90] = struct.pack(">I", r1)
    palm[94:98] = struct.pack(">I", r2)
    return bytes(palm) + record0 + text_record + image_bytes


# --- thumbnail_name ---------------------------------------------------------------


def test_thumbnail_name_matches_the_device_naming_scheme():
    assert (
        thumbnails.thumbnail_name("ABCDEF0123456789")
        == "thumbnail_ABCDEF0123456789_EBOK_portrait.jpg"
    )
    assert (
        thumbnails.thumbnail_name("ABCDEF0123456789", "PDOC")
        == "thumbnail_ABCDEF0123456789_PDOC_portrait.jpg"
    )


# --- install: cached cover (orchestration, `_resize` stubbed) --------------------


def test_a_cached_cover_is_installed_and_written_to_system_thumbnails(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbnails, "_resize", _stub_resize)
    cache_dir = tmp_path / "cache"
    cache_path(cache_dir, BOOK_ID).parent.mkdir(parents=True)
    cache_path(cache_dir, BOOK_ID).write_bytes(_FAKE_JPEG + b"2" * 2000)

    backend = FakeBackend()
    book = thumbnails.Book(device_path="documents/en/Book.azw3", book_id=BOOK_ID, cdetype="EBOK")

    statuses = thumbnails.install(backend, [book], cache_dir=cache_dir)

    assert statuses == {BOOK_ID: "installed"}
    device_path = f"system/thumbnails/thumbnail_{BOOK_ID}_EBOK_portrait.jpg"
    assert device_path in backend.written
    # The book file itself was never fetched: a cached cover means no device read.
    assert backend.read_calls == []


def test_install_reports_progress_once_per_book(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbnails, "_resize", _stub_resize)
    cache_dir = tmp_path / "cache"
    cache_path(cache_dir, BOOK_ID).parent.mkdir(parents=True)
    cache_path(cache_dir, BOOK_ID).write_bytes(_FAKE_JPEG + b"2" * 2000)
    backend = FakeBackend()
    book = thumbnails.Book(device_path="documents/en/Book.azw3", book_id=BOOK_ID)

    calls: list[tuple[int, int]] = []
    thumbnails.install(
        backend, [book], cache_dir=cache_dir, on_progress=lambda d, t: calls.append((d, t))
    )

    assert calls == [(1, 1)]


# --- install: device silently discards the write ----------------------------------


def test_a_device_that_discards_the_write_reports_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbnails, "_resize", _stub_resize)
    cache_dir = tmp_path / "cache"
    cache_path(cache_dir, BOOK_ID).parent.mkdir(parents=True)
    cache_path(cache_dir, BOOK_ID).write_bytes(_FAKE_JPEG + b"2" * 2000)
    device_path = f"system/thumbnails/thumbnail_{BOOK_ID}_EBOK_portrait.jpg"
    backend = FakeBackend(reject_writes_to=frozenset({device_path}))
    book = thumbnails.Book(device_path="documents/en/Book.azw3", book_id=BOOK_ID)

    statuses = thumbnails.install(backend, [book], cache_dir=cache_dir)

    assert statuses == {BOOK_ID: "rejected"}
    assert device_path not in backend.written


# --- install: no cover anywhere ----------------------------------------------------


def test_a_book_with_no_cover_anywhere_reports_no_cover_and_writes_nothing(tmp_path):
    backend = FakeBackend(files={"documents/en/Book.azw3": b"not a real mobi file at all"})
    book = thumbnails.Book(device_path="documents/en/Book.azw3", book_id="NOCOVERBOOK01")

    statuses = thumbnails.install(backend, [book], cache_dir=tmp_path / "cache")

    assert statuses == {"NOCOVERBOOK01": "no_cover"}
    assert backend.written == {}


def test_a_book_vanished_from_the_device_reports_no_cover_not_a_crash(tmp_path):
    """Nothing cached, and the book itself is no longer on the device (renamed or
    deleted between listing and this call) — `backend.read` raises, which must be
    absorbed the same way one bad book is absorbed everywhere else in this Kindle
    path, not propagated to abort the whole `install()` batch."""
    backend = FakeBackend(files={})  # the book is simply not there
    book = thumbnails.Book(device_path="documents/en/Gone.azw3", book_id="GONEBOOK000001")

    statuses = thumbnails.install(backend, [book], cache_dir=tmp_path / "cache")

    assert statuses == {"GONEBOOK000001": "no_cover"}


def test_resize_returning_false_reports_no_cover(tmp_path, monkeypatch):
    """`_resize` itself returning False (ffmpeg ran but produced nothing usable —
    a corrupt or undecodable cached/extracted source) is a clean, non-exceptional
    "no cover", not a "failed" device fault. Faked at the `run_ffmpeg` layer, the
    same seam `test_compress_video.py` uses, so no real ffmpeg process is spawned."""
    monkeypatch.setattr(thumbnails, "run_ffmpeg", lambda argv, **kw: (1, "decode error"))
    cache_dir = tmp_path / "cache"
    cache_path(cache_dir, BOOK_ID).parent.mkdir(parents=True)
    cache_path(cache_dir, BOOK_ID).write_bytes(b"not really a jpeg" * 100)  # > 1000 bytes
    backend = FakeBackend()
    book = thumbnails.Book(device_path="documents/en/Book.azw3", book_id=BOOK_ID)

    statuses = thumbnails.install(backend, [book], cache_dir=cache_dir)

    assert statuses == {BOOK_ID: "no_cover"}
    assert backend.written == {}


# --- install: a genuine device fault is isolated per book (C1) --------------------


def test_install_isolates_a_per_book_device_fault_and_still_reports_the_rest(tmp_path, monkeypatch):
    """A device fault mid-write on ONE book (a full disk, a yanked cable,
    `DeviceWriteProtected`, an MTP `CalibreError`) must not propagate out of
    `install()` — that would abort every book still queued and discard whatever
    already succeeded. Three books: the first and third have a usable cache and
    succeed; the second's write raises."""
    monkeypatch.setattr(thumbnails, "_resize", _stub_resize)
    cache_dir = tmp_path / "cache"
    for book_id in ("GOODBOOK0001", "FAILBOOK0001", "GOODBOOK0002"):
        cache_path(cache_dir, book_id).parent.mkdir(parents=True, exist_ok=True)
        cache_path(cache_dir, book_id).write_bytes(_FAKE_JPEG + b"9" * 2000)

    fail_path = f"system/thumbnails/{thumbnails.thumbnail_name('FAILBOOK0001')}"
    backend = FakeBackend(fail_writes_to=frozenset({fail_path}))
    books = [
        thumbnails.Book(device_path="documents/en/A.azw3", book_id="GOODBOOK0001"),
        thumbnails.Book(device_path="documents/en/B.azw3", book_id="FAILBOOK0001"),
        thumbnails.Book(device_path="documents/en/C.azw3", book_id="GOODBOOK0002"),
    ]

    statuses = thumbnails.install(backend, books, cache_dir=cache_dir)

    assert statuses == {
        "GOODBOOK0001": "installed",
        "FAILBOOK0001": "failed",
        "GOODBOOK0002": "installed",
    }


def test_a_host_side_failure_outside_the_write_is_isolated_per_book_too(tmp_path, monkeypatch):
    """`install()` promises no per-book fault escapes, but four statements used to sit
    OUTSIDE any handler: locating ffmpeg, creating the scratch directory, stat-ing a
    cached cover and writing an extracted one. A full host `/tmp` is enough to hit
    them, and for `ebook kindle add` an escape there happens AFTER books are already
    on the device but BEFORE the journal records them. Here the scratch directory
    itself refuses to be created."""
    monkeypatch.setattr(thumbnails, "_resize", _stub_resize)
    cache_dir = tmp_path / "cache"
    for book_id in ("GOODBOOK0001", "GOODBOOK0002"):
        cache_path(cache_dir, book_id).parent.mkdir(parents=True, exist_ok=True)
        cache_path(cache_dir, book_id).write_bytes(_FAKE_JPEG + b"9" * 2000)

    real_temporary_directory = thumbnails.tempfile.TemporaryDirectory
    calls = {"n": 0}

    def flaky_temporary_directory(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        return real_temporary_directory(*args, **kwargs)

    monkeypatch.setattr(thumbnails.tempfile, "TemporaryDirectory", flaky_temporary_directory)

    books = [
        thumbnails.Book(device_path="documents/en/A.azw3", book_id="GOODBOOK0001"),
        thumbnails.Book(device_path="documents/en/B.azw3", book_id="GOODBOOK0002"),
    ]
    statuses = thumbnails.install(FakeBackend(), books, cache_dir=cache_dir)

    assert statuses == {"GOODBOOK0001": "failed", "GOODBOOK0002": "installed"}


def test_an_unlocatable_ffmpeg_fails_the_books_instead_of_escaping(tmp_path, monkeypatch):
    """`ffmpeg_exe()` used to run once, before the loop and outside every handler, so
    a broken imageio-ffmpeg install aborted the whole batch by raising out of
    `install()`. It is now resolved inside the per-book guard."""

    def no_ffmpeg() -> str:
        raise RuntimeError("no ffmpeg anywhere")

    monkeypatch.setattr(thumbnails, "ffmpeg_exe", no_ffmpeg)
    cache_dir = tmp_path / "cache"
    cache_path(cache_dir, "GOODBOOK0001").parent.mkdir(parents=True, exist_ok=True)
    cache_path(cache_dir, "GOODBOOK0001").write_bytes(_FAKE_JPEG + b"9" * 2000)

    book = thumbnails.Book(device_path="documents/en/A.azw3", book_id="GOODBOOK0001")
    assert thumbnails.install(FakeBackend(), [book], cache_dir=cache_dir) == {
        "GOODBOOK0001": "failed"
    }


# --- install: no EXTH 113 at all ---------------------------------------------------


def test_a_book_with_no_book_id_is_handled_without_crashing(tmp_path):
    backend = FakeBackend()
    book = thumbnails.Book(device_path="documents/en/Unknown.azw3", book_id="")

    statuses = thumbnails.install(backend, [book], cache_dir=tmp_path / "cache")

    # Keyed by device_path, since there is no id to key it by.
    assert statuses == {"documents/en/Unknown.azw3": "no_cover"}
    assert backend.written == {}


# --- install: an unsafe EXTH value is rejected, not sanitized (C2) ----------------


def test_a_book_with_an_unsafe_book_id_is_rejected_not_sanitized(tmp_path):
    """A book id containing a path separator (or `..`) must never reach a device
    write path OR a host cache lookup built from it — rejected outright as
    `"no_cover"`, never silently renamed into a different-but-still-wrong name."""
    backend = FakeBackend()
    book = thumbnails.Book(device_path="documents/en/Evil.azw3", book_id="../../audible/x")

    statuses = thumbnails.install(backend, [book], cache_dir=tmp_path / "cache")

    assert statuses == {"../../audible/x": "no_cover"}
    assert backend.written == {}
    assert backend.read_calls == []  # never even attempted extraction


def test_a_book_with_an_unsafe_cdetype_is_rejected_not_sanitized(tmp_path):
    backend = FakeBackend()
    book = thumbnails.Book(
        device_path="documents/en/Evil.azw3", book_id="OKBOOKID0001", cdetype="../system"
    )

    statuses = thumbnails.install(backend, [book], cache_dir=tmp_path / "cache")

    assert statuses == {"OKBOOKID0001": "no_cover"}
    assert backend.written == {}


# --- install: an already-materialised local copy is reused, not re-fetched (I2) ---


def test_a_book_with_a_local_path_is_never_fetched_a_second_time(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbnails, "_resize", _stub_resize)
    local = tmp_path / "already-fetched.azw3"
    local.write_bytes(
        _mobi_with_image(
            [(113, b"LOCALPATHBOOK1"), (501, b"EBOK"), (201, struct.pack(">I", 0))], _FAKE_JPEG
        )
    )
    # A backend whose `read` always raises: if `install()` fetched the book again
    # instead of reusing `local_path`, this test would fail on that call.
    backend = FakeBackend(files={})
    book = thumbnails.Book(
        device_path="documents/en/Already.azw3", book_id="LOCALPATHBOOK1", local_path=local
    )

    statuses = thumbnails.install(backend, [book], cache_dir=tmp_path / "cache")

    assert statuses == {"LOCALPATHBOOK1": "installed"}
    assert backend.read_calls == []


# --- _read_cover_image: the offset math, without ffmpeg ---------------------------


def test_read_cover_image_returns_none_for_a_plain_non_mobi_file(tmp_path):
    plain = tmp_path / "plain.txt"
    plain.write_bytes(b"just some bytes, not a PalmDB file at all")
    assert thumbnails._read_cover_image(plain) is None


def test_read_cover_image_extracts_via_the_cover_offset(tmp_path):
    mobi = tmp_path / "book.azw3"
    mobi.write_bytes(
        _mobi_with_image(
            [(113, b"COVEROFFSET01"), (501, b"EBOK"), (201, struct.pack(">I", 0))], _FAKE_JPEG
        )
    )
    assert thumbnails._read_cover_image(mobi) == _FAKE_JPEG


def test_read_cover_image_falls_back_to_the_thumb_offset_when_the_cover_offset_is_absent(
    tmp_path,
):
    """EXTH 201 (the full cover) is the preferred source; every other fixture in
    this file sets it, so without this test the EXTH 202 fallback branch never
    runs at all. Here 201 is absent entirely and only 202 is set."""
    mobi = tmp_path / "book.azw3"
    mobi.write_bytes(
        _mobi_with_image(
            [(113, b"THUMBOFFSET01"), (501, b"EBOK"), (202, struct.pack(">I", 0))], _FAKE_JPEG
        )
    )
    assert thumbnails._read_cover_image(mobi) == _FAKE_JPEG


def test_read_cover_image_falls_back_to_thumb_offset_when_cover_offset_is_the_no_cover_sentinel(
    tmp_path,
):
    """EXTH 201 present but explicitly `0xFFFFFFFF` ("no cover" per the format) must
    fall through to 202, not be treated as a real (if odd) record index."""
    mobi = tmp_path / "book.azw3"
    mobi.write_bytes(
        _mobi_with_image(
            [
                (113, b"NOCOVERSENTIN1"),
                (501, b"EBOK"),
                (201, struct.pack(">I", 0xFFFFFFFF)),
                (202, struct.pack(">I", 0)),
            ],
            _FAKE_JPEG,
        )
    )
    assert thumbnails._read_cover_image(mobi) == _FAKE_JPEG


def test_image_record_index_returns_none_for_the_no_cover_sentinel():
    records = {exth.TAG_COVER_OFFSET: struct.pack(">I", 0xFFFFFFFF)}
    assert thumbnails._image_record_index(records, exth.TAG_COVER_OFFSET, 5) is None


def test_reading_a_cover_never_escapes_as_something_install_cannot_catch(tmp_path, monkeypatch):
    """`_read_cover_image` reads the WHOLE book to reach an image record, guarded only
    by `except OSError` — while the comment three lines below it names `ValueError` and
    `MemoryError` as the families `read_records` does not absorb and `_install_guarded`
    does not catch either, and fixes it for the callee but not for this read. An escape
    from here breaks `install()`'s "no per-book fault aborts the batch", and surfaces
    as `internal_error`/exit 1 AFTER the device has already been written to.
    """
    # The live arm, through the real function: `open()` raises `ValueError`, not
    # `OSError`, for a path carrying an embedded NUL byte.
    assert thumbnails._read_cover_image(Path("no\0pe")) is None

    book = tmp_path / "Book.azw3"
    book.write_bytes(b"\0" * 128)
    real_read_bytes = Path.read_bytes

    def out_of_memory(self):
        if self.name == "Book.azw3":
            raise MemoryError("cannot allocate")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", out_of_memory)
    assert thumbnails._read_cover_image(book) is None
