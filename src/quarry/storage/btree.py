"""B+trees stored in pages.

Every table is a B+tree keyed by ``(rowid,)`` whose values are encoded rows. Every index is a B+tree keyed by
``(column values..., rowid)`` with empty values. Keys are tuples of SQL values and sort in SQL order, so
``(1,)`` and ``(1.0,)`` are equal and NULL sorts first.

Leaves hold the data and interior pages hold separator keys. The root keeps its page number for the life of
the tree: when it splits, its content moves to a new page and the root becomes an interior page above it.
Values larger than a quarter of a page live in chains of overflow pages.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterator

from ..errors import CorruptDatabaseError, OperationalError
from ..values import sort_key
from .pager import Pager
from .pages import INTERIOR, LEAF, Interior, Leaf, Overflow, OverflowRef


def key_of(key: tuple) -> tuple:
    """Comparable form of a key tuple."""
    return tuple(sort_key(v) for v in key)


class _SortedKeys:
    """A list view of a node's keys in comparable form, for bisect."""

    __slots__ = ("keys",)

    def __init__(self, keys: list[tuple]) -> None:
        self.keys = keys

    def __len__(self) -> int:
        return len(self.keys)

    def __getitem__(self, i: int) -> tuple:
        return key_of(self.keys[i])


class BTree:
    def __init__(self, pager: Pager, root: int) -> None:
        self.pager = pager
        self.root = root
        size = pager.page_size
        self.max_inline = size // 4
        self.max_key = size // 4 - 16

    # ------------------------------------------------------------ creation and removal

    @staticmethod
    def create(pager: Pager) -> int:
        return pager.allocate(Leaf())

    def drop(self) -> int:
        """Free every page of the tree, the root included. Returns the number of pages freed."""
        count = 0
        stack = [self.root]
        while stack:
            pgno = stack.pop()
            node = self.pager.get(pgno)
            if node.kind == INTERIOR:
                stack.extend(node.children)
            elif node.kind == LEAF:
                for value in node.values:
                    if isinstance(value, OverflowRef):
                        count += self._free_overflow(value)
            else:
                raise CorruptDatabaseError(f"page {pgno} in a tree is neither leaf nor interior")
            self.pager.free(pgno)
            count += 1
        return count

    def clear(self) -> None:
        """Remove every entry but keep the root page."""
        root = self.pager.get(self.root)
        if root.kind == INTERIOR:
            for child in root.children:
                BTree(self.pager, child).drop()
        else:
            for value in root.values:
                if isinstance(value, OverflowRef):
                    self._free_overflow(value)
        self.pager.mark_dirty(self.root)
        self.pager.put(self.root, Leaf())

    # ------------------------------------------------------------ search

    def _descend(self, key: tuple) -> tuple[list[tuple[int, int]], int, Leaf]:
        """Path of (interior pgno, child index) from the root to the leaf that would hold ``key``."""
        path: list[tuple[int, int]] = []
        pgno = self.root
        k = key_of(key)
        node = self.pager.get(pgno)
        depth = 0
        while node.kind == INTERIOR:
            i = bisect.bisect_right(_SortedKeys(node.keys), k)
            path.append((pgno, i))
            pgno = node.children[i]
            node = self.pager.get(pgno)
            depth += 1
            if depth > 64:
                raise CorruptDatabaseError("B+tree is too deep: the page links have a cycle")
        if node.kind != LEAF:
            raise CorruptDatabaseError(f"page {pgno} should be a leaf")
        return path, pgno, node

    def get(self, key: tuple) -> bytes | None:
        _path, _pgno, leaf = self._descend(key)
        k = key_of(key)
        i = bisect.bisect_left(_SortedKeys(leaf.keys), k)
        if i < len(leaf.keys) and key_of(leaf.keys[i]) == k:
            return self._load(leaf.values[i])
        return None

    def __contains__(self, key: tuple) -> bool:
        return self.get(key) is not None

    def last_key(self) -> tuple | None:
        node = self.pager.get(self.root)
        while node.kind == INTERIOR:
            for child in reversed(node.children):
                candidate = BTree(self.pager, child).last_key()
                if candidate is not None:
                    return candidate
            return None
        return node.keys[-1] if node.keys else None

    def scan(
        self,
        start: tuple | None = None,
        start_inclusive: bool = True,
        end: tuple | None = None,
        end_inclusive: bool = True,
        prefix: bool = False,
    ) -> Iterator[tuple[tuple, bytes]]:
        """Entries in key order between ``start`` and ``end``.

        With ``prefix=True``, ``start`` and ``end`` compare against the first ``len(start)`` (or ``len(end)``)
        columns of each key only, which is how index lookups on a column prefix work.
        """
        ks = key_of(start) if start is not None else None
        ke = key_of(end) if end is not None else None
        ns = len(start) if start is not None else 0
        ne = len(end) if end is not None else 0

        def head(key: tuple, n: int) -> tuple:
            kk = key_of(key)
            return kk[:n] if prefix else kk

        # find the first leaf
        if start is None:
            path: list[tuple[int, int]] = []
            pgno = self.root
            node = self.pager.get(pgno)
            while node.kind == INTERIOR:
                path.append((pgno, 0))
                pgno = node.children[0]
                node = self.pager.get(pgno)
            i = 0
        else:
            path = []
            pgno = self.root
            node = self.pager.get(pgno)
            while node.kind == INTERIOR:
                # with a prefix, an equal separator can hide matching keys in the child to its left
                if prefix:
                    i = bisect.bisect_left([k[:ns] for k in _SortedKeys(node.keys)], ks)
                else:
                    i = bisect.bisect_right(_SortedKeys(node.keys), ks)
                path.append((pgno, i))
                pgno = node.children[i]
                node = self.pager.get(pgno)
            i = 0
        started = start is None
        while True:
            keys = node.keys
            while i < len(keys):
                key = keys[i]
                if not started:
                    h = head(key, ns)
                    if h < ks or (not start_inclusive and h == ks):
                        i += 1
                        continue
                    started = True
                if ke is not None:
                    h = head(key, ne)
                    if h > ke or (not end_inclusive and h == ke):
                        return
                yield key, self._load(node.values[i])
                # the leaf can change if the caller writes; re-read it by identity
                i += 1
            # move to the next leaf through the path
            while path:
                parent_no, ci = path.pop()
                parent = self.pager.get(parent_no)
                if ci + 1 < len(parent.children):
                    path.append((parent_no, ci + 1))
                    pgno = parent.children[ci + 1]
                    node = self.pager.get(pgno)
                    while node.kind == INTERIOR:
                        path.append((pgno, 0))
                        pgno = node.children[0]
                        node = self.pager.get(pgno)
                    i = 0
                    break
            else:
                return

    def count(self) -> int:
        total = 0
        stack = [self.root]
        while stack:
            node = self.pager.get(stack.pop())
            if node.kind == INTERIOR:
                stack.extend(node.children)
            else:
                total += len(node.keys)
        return total

    # ------------------------------------------------------------ values

    def _load(self, value: bytes | OverflowRef) -> bytes:
        if not isinstance(value, OverflowRef):
            return value
        out = bytearray()
        pgno = value.first
        guard = 0
        while pgno and len(out) < value.length:
            page = self.pager.get(pgno)
            if not isinstance(page, Overflow):
                raise CorruptDatabaseError(f"page {pgno} should be an overflow page")
            out += page.data
            pgno = page.next
            guard += 1
            if guard > 1 << 22:
                raise CorruptDatabaseError("overflow chain has a loop")
        if len(out) != value.length:
            raise CorruptDatabaseError("overflow chain is shorter than its value")
        return bytes(out)

    def _store(self, data: bytes) -> bytes | OverflowRef:
        if len(data) <= self.max_inline:
            return data
        cap = Overflow.capacity(self.pager.page_size)
        chunks = [data[i : i + cap] for i in range(0, len(data), cap)]
        nxt = 0
        for chunk in reversed(chunks):
            nxt = self.pager.allocate(Overflow(nxt, chunk))
        return OverflowRef(nxt, len(data))

    def _free_overflow(self, ref: OverflowRef) -> int:
        pgno = ref.first
        count = 0
        while pgno:
            page = self.pager.get(pgno)
            nxt = page.next
            self.pager.free(pgno)
            pgno = nxt
            count += 1
        return count

    # ------------------------------------------------------------ insert

    def insert(self, key: tuple, value: bytes, replace: bool = True) -> bool:
        """Insert or replace an entry. Returns False when the key exists and ``replace`` is False."""
        cell = Leaf.cell_size(key, b"")
        if cell > self.max_key:
            raise OperationalError(
                f"index key is too large ({cell} bytes; the limit is {self.max_key} with {self.pager.page_size}-byte pages)"
            )
        path, pgno, leaf = self._descend(key)
        k = key_of(key)
        i = bisect.bisect_left(_SortedKeys(leaf.keys), k)
        exists = i < len(leaf.keys) and key_of(leaf.keys[i]) == k
        if exists and not replace:
            return False
        self.pager.mark_dirty(pgno)
        stored = self._store(value)
        size = Leaf.cell_size(key, stored)
        if exists:
            old = leaf.values[i]
            if isinstance(old, OverflowRef):
                self._free_overflow(old)
            leaf.keys[i] = key
            leaf.values[i] = stored
            leaf.sizes[i] = size
        else:
            leaf.keys.insert(i, key)
            leaf.values.insert(i, stored)
            leaf.sizes.insert(i, size)
        if leaf.used > self.pager.page_size:
            self._split_leaf(path, pgno, leaf)
        return True

    def _split_leaf(self, path: list[tuple[int, int]], pgno: int, leaf: Leaf) -> None:
        mid = self._split_point(leaf.sizes, 3)
        right = Leaf(leaf.keys[mid:], leaf.values[mid:], leaf.sizes[mid:])
        del leaf.keys[mid:], leaf.values[mid:], leaf.sizes[mid:]
        separator = right.keys[0]
        if pgno == self.root:
            # keep the root page: move both halves to new pages
            left_no = self.pager.allocate(leaf)
            right_no = self.pager.allocate(right)
            root = Interior([separator], [left_no, right_no], [Interior.entry_size(separator)])
            self.pager.put(self.root, root)
            return
        right_no = self.pager.allocate(right)
        self._insert_into_parent(path, separator, right_no)

    def _insert_into_parent(self, path: list[tuple[int, int]], key: tuple, right_no: int) -> None:
        parent_no, ci = path.pop()
        self.pager.mark_dirty(parent_no)
        parent: Interior = self.pager.get(parent_no)
        parent.keys.insert(ci, key)
        parent.children.insert(ci + 1, right_no)
        parent.sizes.insert(ci, Interior.entry_size(key))
        if parent.used <= self.pager.page_size:
            return
        mid = self._split_point(parent.sizes, 7)
        up = parent.keys[mid]
        right = Interior(parent.keys[mid + 1 :], parent.children[mid + 1 :], parent.sizes[mid + 1 :])
        del parent.keys[mid:], parent.children[mid + 1 :], parent.sizes[mid:]
        if parent_no == self.root:
            left_no = self.pager.allocate(parent)
            new_right = self.pager.allocate(right)
            self.pager.put(self.root, Interior([up], [left_no, new_right], [Interior.entry_size(up)]))
            return
        new_right = self.pager.allocate(right)
        self._insert_into_parent(path, up, new_right)

    @staticmethod
    def _split_point(sizes: list[int], header: int) -> int:
        total = header + sum(sizes)
        acc = header
        for i, s in enumerate(sizes):
            acc += s
            if acc >= total // 2:
                return max(1, min(i + 1, len(sizes) - 1))
        return len(sizes) // 2

    # ------------------------------------------------------------ delete

    def delete(self, key: tuple) -> bool:
        path, pgno, leaf = self._descend(key)
        k = key_of(key)
        i = bisect.bisect_left(_SortedKeys(leaf.keys), k)
        if i >= len(leaf.keys) or key_of(leaf.keys[i]) != k:
            return False
        self.pager.mark_dirty(pgno)
        old = leaf.values[i]
        if isinstance(old, OverflowRef):
            self._free_overflow(old)
        del leaf.keys[i], leaf.values[i], leaf.sizes[i]
        if not leaf.keys and pgno != self.root:
            self._remove_child(path, pgno)
        return True

    def _remove_child(self, path: list[tuple[int, int]], pgno: int) -> None:
        """Unlink an empty page from its parent and free it, collapsing empty parents."""
        parent_no, ci = path.pop()
        self.pager.mark_dirty(parent_no)
        parent: Interior = self.pager.get(parent_no)
        self.pager.free(pgno)
        del parent.children[ci]
        if parent.keys:
            ki = ci - 1 if ci > 0 else 0
            del parent.keys[ki], parent.sizes[ki]
        if parent.children:
            if parent_no == self.root and len(parent.children) == 1:
                # shrink the tree: copy the only child into the root page
                only = parent.children[0]
                child = self.pager.get(only)
                self.pager.free(only)
                self.pager.put(self.root, child)
            return
        if parent_no == self.root:
            self.pager.put(self.root, Leaf())
        else:
            self._remove_child(path, parent_no)

    # ------------------------------------------------------------ checking

    def check(self) -> list[str]:
        """Verify ordering, separator bounds and page types. Returns a list of problems."""
        problems: list[str] = []
        pages: set[int] = set()

        def visit(pgno: int, low: tuple | None, high: tuple | None, depth: int) -> int:
            if pgno in pages:
                problems.append(f"page {pgno} is reachable twice")
                return depth
            pages.add(pgno)
            node = self.pager.get(pgno)
            keys = [key_of(k) for k in node.keys]
            if keys != sorted(keys):
                problems.append(f"page {pgno}: keys are out of order")
            if keys and low is not None and keys[0] < low:
                problems.append(f"page {pgno}: key below its separator")
            if keys and high is not None and keys[-1] >= high:
                problems.append(f"page {pgno}: key above its separator")
            if node.kind == LEAF:
                return depth
            if len(node.children) != len(node.keys) + 1:
                problems.append(f"page {pgno}: {len(node.children)} children for {len(node.keys)} keys")
                return depth
            depths = set()
            for j, child in enumerate(node.children):
                lo = keys[j - 1] if j > 0 else low
                hi = keys[j] if j < len(keys) else high
                depths.add(visit(child, lo, hi, depth + 1))
            if len(depths) > 1:
                problems.append(f"page {pgno}: leaves at different depths")
            return max(depths)

        visit(self.root, None, None, 0)
        self.pages_seen = pages
        return problems
