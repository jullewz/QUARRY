"""In-memory forms of database pages and their binary layout.

Every page is ``page_size`` bytes on disk. The first byte says what kind of page it is:

=========  ======================================================================
type       content
=========  ======================================================================
``0x51``   page 0, the file header: magic, page size, page count, free list, cookies
``1``      B+tree leaf: sorted (key, value) cells
``2``      B+tree interior: separator keys and child page numbers
``3``      overflow: the next part of a value too large to fit in a leaf
``4``      free: unused page on the free list
=========  ======================================================================
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from ..errors import CorruptDatabaseError
from .encoding import decode_record, encode_record, get_varint, put_varint

MAGIC = b"QuarryDB format 1\0"
HEADER_PAGE, LEAF, INTERIOR, OVERFLOW, FREE = 0x51, 1, 2, 3, 4

_HEADER = struct.Struct(
    ">18sIIIIIIIq"
)  # magic, page_size, page_count, free_head, free_count, schema_root, schema_cookie, user_version, change_counter
_U16 = struct.Struct(">H")
_U32 = struct.Struct(">I")


@dataclass
class Header:
    page_size: int = 4096
    page_count: int = 2
    free_head: int = 0
    free_count: int = 0
    schema_root: int = 1
    schema_cookie: int = 0
    user_version: int = 0
    change_counter: int = 0
    kind: int = HEADER_PAGE

    def encode(self, page_size: int) -> bytes:
        data = bytes([HEADER_PAGE]) + _HEADER.pack(
            MAGIC,
            self.page_size,
            self.page_count,
            self.free_head,
            self.free_count,
            self.schema_root,
            self.schema_cookie,
            self.user_version,
            self.change_counter,
        )
        return data.ljust(page_size, b"\0")

    @staticmethod
    def decode(data: bytes) -> Header:
        if data[0] != HEADER_PAGE:
            raise CorruptDatabaseError("this file is not a Quarry database (bad header page)")
        magic, *fields = _HEADER.unpack_from(data, 1)
        if magic != MAGIC:
            raise CorruptDatabaseError("this file is not a Quarry database (wrong magic bytes)")
        return Header(*fields)

    def copy(self) -> Header:
        return Header(**{k: getattr(self, k) for k in self.__dataclass_fields__})

    clone = copy


@dataclass(eq=False)
class OverflowRef:
    """A leaf value stored in a chain of overflow pages."""

    first: int
    length: int


@dataclass(eq=False)
class Leaf:
    keys: list[tuple] = field(default_factory=list)
    values: list[bytes | OverflowRef] = field(default_factory=list)
    sizes: list[int] = field(default_factory=list)  # encoded size of each cell
    kind: int = LEAF

    @property
    def used(self) -> int:
        return 3 + sum(self.sizes)

    def clone(self) -> Leaf:
        return Leaf(list(self.keys), list(self.values), list(self.sizes))

    @staticmethod
    def cell_size(key: tuple, value: bytes | OverflowRef) -> int:
        k = len(encode_record(key))
        if isinstance(value, OverflowRef):
            return _varint_len(k) + k + 1 + 4 + _varint_len(value.length)
        return _varint_len(k) + k + 1 + _varint_len(len(value)) + len(value)

    def encode(self, page_size: int) -> bytes:
        out = bytearray([LEAF])
        out += _U16.pack(len(self.keys))
        for key, value in zip(self.keys, self.values):
            k = encode_record(key)
            put_varint(out, len(k))
            out += k
            if isinstance(value, OverflowRef):
                out.append(1)
                out += _U32.pack(value.first)
                put_varint(out, value.length)
            else:
                out.append(0)
                put_varint(out, len(value))
                out += value
        if len(out) > page_size:
            raise CorruptDatabaseError("leaf page overflowed while encoding")
        return bytes(out.ljust(page_size, b"\0"))

    @staticmethod
    def decode(data: bytes) -> Leaf:
        (n,) = _U16.unpack_from(data, 1)
        pos = 3
        leaf = Leaf()
        for _ in range(n):
            start = pos
            klen, pos = get_varint(data, pos)
            key, _end = decode_record(data, pos)
            pos += klen
            flag = data[pos]
            pos += 1
            value: bytes | OverflowRef
            if flag:
                (first,) = _U32.unpack_from(data, pos)
                length, pos = get_varint(data, pos + 4)
                value = OverflowRef(first, length)
            else:
                vlen, pos = get_varint(data, pos)
                value = bytes(data[pos : pos + vlen])
                pos += vlen
            leaf.keys.append(key)
            leaf.values.append(value)
            leaf.sizes.append(pos - start)
        return leaf


@dataclass(eq=False)
class Interior:
    keys: list[tuple] = field(default_factory=list)
    children: list[int] = field(default_factory=list)  # len(children) == len(keys) + 1
    sizes: list[int] = field(default_factory=list)  # encoded size of each (key, child) entry
    kind: int = INTERIOR

    @property
    def used(self) -> int:
        return 7 + sum(self.sizes)

    def clone(self) -> Interior:
        return Interior(list(self.keys), list(self.children), list(self.sizes))

    @staticmethod
    def entry_size(key: tuple) -> int:
        k = len(encode_record(key))
        return _varint_len(k) + k + 4

    def encode(self, page_size: int) -> bytes:
        out = bytearray([INTERIOR])
        out += _U16.pack(len(self.keys))
        out += _U32.pack(self.children[0])
        for key, child in zip(self.keys, self.children[1:]):
            k = encode_record(key)
            put_varint(out, len(k))
            out += k
            out += _U32.pack(child)
        if len(out) > page_size:
            raise CorruptDatabaseError("interior page overflowed while encoding")
        return bytes(out.ljust(page_size, b"\0"))

    @staticmethod
    def decode(data: bytes) -> Interior:
        (n,) = _U16.unpack_from(data, 1)
        (child0,) = _U32.unpack_from(data, 3)
        node = Interior(children=[child0])
        pos = 7
        for _ in range(n):
            start = pos
            klen, pos = get_varint(data, pos)
            key, _end = decode_record(data, pos)
            pos += klen
            (child,) = _U32.unpack_from(data, pos)
            pos += 4
            node.keys.append(key)
            node.children.append(child)
            node.sizes.append(pos - start)
        return node


@dataclass(eq=False)
class Overflow:
    next: int = 0
    data: bytes = b""
    kind: int = OVERFLOW

    def clone(self) -> Overflow:
        return Overflow(self.next, self.data)

    def encode(self, page_size: int) -> bytes:
        return (bytes([OVERFLOW]) + _U32.pack(self.next) + _U32.pack(len(self.data)) + self.data).ljust(page_size, b"\0")

    @staticmethod
    def decode(data: bytes) -> Overflow:
        (nxt,) = _U32.unpack_from(data, 1)
        (n,) = _U32.unpack_from(data, 5)
        return Overflow(nxt, bytes(data[9 : 9 + n]))

    @staticmethod
    def capacity(page_size: int) -> int:
        return page_size - 9


@dataclass(eq=False)
class Free:
    next: int = 0
    kind: int = FREE

    def clone(self) -> Free:
        return Free(self.next)

    def encode(self, page_size: int) -> bytes:
        return (bytes([FREE]) + _U32.pack(self.next)).ljust(page_size, b"\0")

    @staticmethod
    def decode(data: bytes) -> Free:
        return Free(_U32.unpack_from(data, 1)[0])


Node = Header | Leaf | Interior | Overflow | Free


def decode_page(pgno: int, data: bytes) -> Node:
    kind = data[0]
    if pgno == 0:
        return Header.decode(data)
    if kind == LEAF:
        return Leaf.decode(data)
    if kind == INTERIOR:
        return Interior.decode(data)
    if kind == OVERFLOW:
        return Overflow.decode(data)
    if kind == FREE:
        return Free.decode(data)
    raise CorruptDatabaseError(f"page {pgno} has unknown type {kind}")


def _varint_len(n: int) -> int:
    length = 1
    while n >= 0x80:
        n >>= 7
        length += 1
    return length
