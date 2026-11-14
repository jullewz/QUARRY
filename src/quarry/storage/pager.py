"""Page cache, transactions, savepoints and the write-ahead log.

The pager hands out decoded pages and keeps track of which ones changed. Nothing reaches the database file
directly. On commit, the changed pages are appended to the write-ahead log (``<db>-wal``) with checksums and
a commit marker, and the log is flushed to disk. A transaction counts only when its commit marker is complete,
so a crash at any moment leaves either the old or the new state, never a mix.

Now and then (and on close), a checkpoint copies the newest version of each logged page into the main file
and empties the log.
"""

from __future__ import annotations

import os
import struct
import sys
import zlib
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from ..errors import CorruptDatabaseError, InternalError, OperationalError
from .pages import FREE, Free, Header, Leaf, Node, decode_page

WAL_MAGIC = b"QWAL"
_WAL_HEADER = struct.Struct(">4sIII")  # magic, page_size, salt, checkpoint sequence
_FRAME = struct.Struct(">IIII")  # pgno, db size after commit (0 if not a commit frame), salt, checksum
CLEAN = None  # savepoint marker: the page was unchanged when the savepoint began


class MemoryBackend:
    """Committed pages kept in a dict, for ``:memory:`` databases."""

    path = ":memory:"

    def __init__(self, page_size: int) -> None:
        self.page_size = page_size
        self.pages: dict[int, bytes] = {}

    def exists(self) -> bool:
        return bool(self.pages)

    def read(self, pgno: int) -> bytes:
        try:
            return self.pages[pgno]
        except KeyError:
            raise CorruptDatabaseError(f"page {pgno} does not exist") from None

    def commit(self, pages: dict[int, bytes], page_count: int) -> None:
        self.pages.update(pages)
        for pgno in [p for p in self.pages if p >= page_count]:
            del self.pages[pgno]

    def checkpoint(self) -> None:
        pass

    def wal_frames(self) -> int:
        return 0

    def close(self) -> None:
        pass


class FileBackend:
    """A database file plus its write-ahead log."""

    def __init__(self, path: str, page_size: int, autocheckpoint: int = 1000, sync: bool = True) -> None:
        self.path = path
        self.wal_path = path + "-wal"
        self.page_size = page_size
        self.autocheckpoint = autocheckpoint
        self.sync = sync
        try:
            self.file = open(path, "r+b" if os.path.exists(path) else "w+b")
        except OSError as e:
            raise OperationalError(f"cannot open database file {path!r}: {e.strerror}") from e
        self._lock()
        if os.path.getsize(path) >= 1:
            # use the page size recorded in the file
            self.file.seek(0)
            head = self.file.read(64)
            if len(head) >= 23:
                try:
                    self.page_size = Header.decode(head.ljust(page_size, b"\0")).page_size
                except CorruptDatabaseError:
                    self._unlock()
                    self.file.close()
                    raise
        self.wal_index: dict[int, int] = {}  # pgno -> file offset of its newest committed frame
        self.wal = open(self.wal_path, "r+b" if os.path.exists(self.wal_path) else "w+b")
        self.salt = 0
        self.checksum = 0
        self.wal_end = _WAL_HEADER.size
        self.db_size: int | None = None  # page count from the last commit frame
        self._recover()

    # ------------------------------------------------------------ locking

    def _lock(self) -> None:
        try:
            if sys.platform == "win32":
                import msvcrt

                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise OperationalError(
                f"database {self.path!r} is already open in another connection or process; Quarry allows one at a time"
            ) from None

    def _unlock(self) -> None:
        try:
            if sys.platform == "win32":
                import msvcrt

                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    # ------------------------------------------------------------ recovery

    def _recover(self) -> None:
        """Read the log and index every frame of every complete transaction."""
        self.wal.seek(0)
        head = self.wal.read(_WAL_HEADER.size)
        if len(head) < _WAL_HEADER.size:
            self._reset_wal(salt=1)
            return
        magic, page_size, salt, _seq = _WAL_HEADER.unpack(head)
        if magic != WAL_MAGIC or page_size != self.page_size:
            self._reset_wal(salt=1)  # not ours or unusable: start a fresh log
            return
        self.salt = salt
        checksum = zlib.crc32(head)
        offset = _WAL_HEADER.size
        pending: dict[int, int] = {}
        frame_size = _FRAME.size + self.page_size
        good_end = offset
        good_checksum = checksum
        while True:
            self.wal.seek(offset)
            raw = self.wal.read(frame_size)
            if len(raw) < frame_size:
                break  # torn tail
            pgno, commit_size, fsalt, fsum = _FRAME.unpack_from(raw)
            if fsalt != salt:
                break  # left over from before the last checkpoint
            expect = zlib.crc32(raw[_FRAME.size :], zlib.crc32(raw[:12], checksum))
            if expect != fsum:
                break  # damaged or half-written frame
            checksum = expect
            pending[pgno] = offset + _FRAME.size
            offset += frame_size
            if commit_size:
                self.wal_index.update(pending)
                pending.clear()
                self.db_size = commit_size
                good_end = offset
                good_checksum = checksum
        self.wal_end = good_end
        self.checksum = good_checksum

    def _reset_wal(self, salt: int) -> None:
        self.salt = salt & 0xFFFFFFFF or 1
        head = _WAL_HEADER.pack(WAL_MAGIC, self.page_size, self.salt, 0)
        self.wal.seek(0)
        self.wal.truncate()
        self.wal.write(head)
        self._flush(self.wal)
        self.checksum = zlib.crc32(head)
        self.wal_end = _WAL_HEADER.size
        self.wal_index.clear()
        self.db_size = None

    def _flush(self, f: Any) -> None:
        f.flush()
        if self.sync:
            os.fsync(f.fileno())

    # ------------------------------------------------------------ pages

    def exists(self) -> bool:
        return self.db_size is not None or os.path.getsize(self.path) > 0

    def read(self, pgno: int) -> bytes:
        offset = self.wal_index.get(pgno)
        f = self.file
        if offset is None:
            offset = pgno * self.page_size
        else:
            f = self.wal
        f.seek(offset)
        data = f.read(self.page_size)
        if len(data) != self.page_size:
            raise CorruptDatabaseError(f"page {pgno} is missing from {self.path!r} (the file is truncated)")
        return data

    def commit(self, pages: dict[int, bytes], page_count: int) -> None:
        if not pages:
            return
        out = bytearray()
        checksum = self.checksum
        items = sorted(pages.items())
        offsets: dict[int, int] = {}
        offset = self.wal_end
        for i, (pgno, data) in enumerate(items):
            commit_size = page_count if i == len(items) - 1 else 0
            head = struct.pack(">III", pgno, commit_size, self.salt)
            checksum = zlib.crc32(data, zlib.crc32(head, checksum))
            out += head + struct.pack(">I", checksum) + data
            offsets[pgno] = offset + _FRAME.size
            offset += _FRAME.size + self.page_size
        self.wal.seek(self.wal_end)
        self.wal.write(out)
        self._flush(self.wal)
        self.wal_end = offset
        self.checksum = checksum
        self.wal_index.update(offsets)
        self.db_size = page_count
        if self.autocheckpoint and self.wal_frames() >= self.autocheckpoint:
            self.checkpoint()

    def wal_frames(self) -> int:
        return (self.wal_end - _WAL_HEADER.size) // (_FRAME.size + self.page_size)

    def checkpoint(self) -> None:
        """Copy the newest logged pages into the main file and empty the log."""
        if not self.wal_index:
            return
        for pgno, offset in sorted(self.wal_index.items()):
            self.wal.seek(offset)
            data = self.wal.read(self.page_size)
            self.file.seek(pgno * self.page_size)
            self.file.write(data)
        if self.db_size is not None:
            self.file.truncate(self.db_size * self.page_size)
        self._flush(self.file)
        self._reset_wal(self.salt + 1)

    def close(self) -> None:
        try:
            if not self.file.closed:
                self.checkpoint()
        finally:
            if not self.wal.closed:
                self.wal.close()
                if os.path.exists(self.wal_path) and os.path.getsize(self.wal_path) <= _WAL_HEADER.size:
                    try:
                        os.remove(self.wal_path)
                    except OSError:
                        pass
            if not self.file.closed:
                self._unlock()
                self.file.close()


class Pager:
    """Decoded page cache with transactions and nested savepoints."""

    def __init__(self, path: str | None = None, page_size: int = 4096, cache_size: int = 2000, sync: bool = True) -> None:
        if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
            raise OperationalError("page size must be a power of two between 512 and 65536")
        self.backend: MemoryBackend | FileBackend = (
            MemoryBackend(page_size) if path in (None, ":memory:") else FileBackend(str(path), page_size, sync=sync)
        )
        self.page_size = self.backend.page_size
        self.cache: OrderedDict[int, Node] = OrderedDict()
        self.cache_size = cache_size
        self.dirty: set[int] = set()
        self.in_txn = False
        self.savepoints: list[dict[int, Any]] = []  # pgno -> copy of the page, or CLEAN
        self.on_rollback: list[Callable[[], None]] = []
        if not self.backend.exists():
            self._create()
        self.header: Header = self.get(0)  # type: ignore[assignment]
        if self.header.page_size != self.page_size:
            raise CorruptDatabaseError("page size in the header does not match the file")

    def _create(self) -> None:
        header = Header(page_size=self.page_size, page_count=2)
        self.backend.commit({0: header.encode(self.page_size), 1: Leaf().encode(self.page_size)}, 2)
        self.backend.checkpoint()

    # ------------------------------------------------------------ reading

    def get(self, pgno: int) -> Any:
        node = self.cache.get(pgno)
        if node is not None:
            self.cache.move_to_end(pgno)
            return node
        if pgno < 0 or (pgno > 0 and hasattr(self, "header") and pgno >= self.header.page_count):
            raise CorruptDatabaseError(f"page {pgno} is out of range")
        node = decode_page(pgno, self.backend.read(pgno))
        self.cache[pgno] = node
        if len(self.cache) > self.cache_size:
            self._evict()
        return node

    def _evict(self) -> None:
        for pgno in list(self.cache):
            if len(self.cache) <= self.cache_size * 3 // 4:
                break
            if pgno not in self.dirty and pgno != 0:
                del self.cache[pgno]

    # ------------------------------------------------------------ writing

    def _require_txn(self) -> None:
        if not self.in_txn:
            raise InternalError("write outside a transaction")

    def mark_dirty(self, pgno: int) -> None:
        """Call before changing a page, so savepoints and rollback can restore it."""
        self._require_txn()
        for sp in self.savepoints:
            if pgno not in sp:
                if pgno in self.dirty:
                    sp[pgno] = self.cache[pgno].clone()  # an in-memory copy: much cheaper than encoding
                else:
                    sp[pgno] = CLEAN
        self.dirty.add(pgno)
        if pgno not in self.cache and pgno < self.header.page_count:
            self.get(pgno)

    def allocate(self, node: Node) -> int:
        """Store a new page and return its number, reusing a free page when there is one."""
        self.mark_dirty(0)
        h = self.header
        if h.free_head:
            pgno = h.free_head
            free = self.get(pgno)
            if not isinstance(free, Free):
                raise CorruptDatabaseError(f"page {pgno} is on the free list but is not free")
            self.mark_dirty(pgno)
            h.free_head = free.next
            h.free_count -= 1
        else:
            pgno = h.page_count
            self.mark_dirty(pgno)
            h.page_count += 1
        self.cache[pgno] = node
        return pgno

    def free(self, pgno: int) -> None:
        self.mark_dirty(pgno)
        self.mark_dirty(0)
        self.cache[pgno] = Free(self.header.free_head)
        self.header.free_head = pgno
        self.header.free_count += 1

    def put(self, pgno: int, node: Node) -> None:
        """Replace a page's content (after mark_dirty)."""
        if pgno not in self.dirty:
            raise InternalError(f"page {pgno} replaced without mark_dirty")
        self.cache[pgno] = node

    # ------------------------------------------------------------ transactions

    def begin(self) -> None:
        if self.in_txn:
            raise OperationalError("a transaction is already active")
        self.in_txn = True

    def commit(self) -> None:
        if not self.in_txn:
            return
        if self.dirty:
            self.mark_dirty(0)
            self.header.change_counter += 1
            pages = {pgno: self.cache[pgno].encode(self.page_size) for pgno in self.dirty}
            self.backend.commit(pages, self.header.page_count)
        self.dirty.clear()
        self.savepoints.clear()
        self.in_txn = False

    def rollback(self) -> None:
        for pgno in self.dirty:
            self.cache.pop(pgno, None)
        self.dirty.clear()
        self.savepoints.clear()
        self.in_txn = False
        self.header = self.get(0)
        for callback in self.on_rollback:
            callback()

    def savepoint(self) -> int:
        """Start a savepoint and return its depth."""
        self._require_txn()
        self.savepoints.append({})
        return len(self.savepoints)

    def rollback_to(self, depth: int) -> None:
        """Undo everything since savepoint ``depth``. The savepoint stays active."""
        if not 1 <= depth <= len(self.savepoints):
            raise InternalError("no such savepoint")
        sp = self.savepoints[depth - 1]
        for pgno, image in sp.items():
            if image is CLEAN:
                self.dirty.discard(pgno)
                self.cache.pop(pgno, None)
            else:
                self.cache[pgno] = image
                self.dirty.add(pgno)
        del self.savepoints[depth:]
        sp.clear()
        self.header = self.get(0)
        for callback in self.on_rollback:
            callback()

    def release(self, depth: int) -> None:
        """Keep the changes since savepoint ``depth`` and forget it (and any newer ones)."""
        del self.savepoints[depth - 1 :]

    # ------------------------------------------------------------ maintenance

    def checkpoint(self) -> None:
        if self.in_txn:
            raise OperationalError("cannot checkpoint inside a transaction")
        self.backend.checkpoint()

    def close(self) -> None:
        if self.in_txn:
            self.rollback()
        self.backend.close()

    def free_pages(self) -> list[int]:
        out = []
        pgno = self.header.free_head
        seen = set()
        while pgno:
            if pgno in seen:
                raise CorruptDatabaseError("the free list has a loop")
            seen.add(pgno)
            out.append(pgno)
            node = self.get(pgno)
            if node.kind != FREE:
                raise CorruptDatabaseError(f"page {pgno} is on the free list but is not free")
            pgno = node.next
        return out
