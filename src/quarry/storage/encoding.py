"""Binary encoding of records (rows and index keys) and of variable-length integers."""

from __future__ import annotations

import struct

from ..errors import CorruptDatabaseError
from ..values import Value

_DOUBLE = struct.Struct(">d")

# type tags
T_NULL, T_INT, T_REAL, T_TEXT, T_BLOB, T_ZERO, T_ONE = range(7)


def put_varint(out: bytearray, n: int) -> None:
    """Unsigned LEB128."""
    if n < 0:
        raise ValueError("varint must be non-negative")
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def get_varint(data: bytes | memoryview, pos: int) -> tuple[int, int]:
    n = 0
    shift = 0
    while True:
        try:
            b = data[pos]
        except IndexError:
            raise CorruptDatabaseError("truncated varint") from None
        pos += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, pos
        shift += 7
        if shift > 70:
            raise CorruptDatabaseError("varint too long")


def _zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63) if n >= 0 else ((-n - 1) << 1) | 1


def _unzigzag(z: int) -> int:
    return (z >> 1) if not z & 1 else -((z >> 1) + 1)


def encode_record(values: tuple | list) -> bytes:
    """Encode a tuple of SQL values. Small integers take two bytes, 0 and 1 take one."""
    out = bytearray()
    put_varint(out, len(values))
    for v in values:
        if v is None:
            out.append(T_NULL)
        elif isinstance(v, int):
            if v == 0:
                out.append(T_ZERO)
            elif v == 1:
                out.append(T_ONE)
            else:
                out.append(T_INT)
                put_varint(out, _zigzag(v))
        elif isinstance(v, float):
            out.append(T_REAL)
            out += _DOUBLE.pack(v)
        elif isinstance(v, str):
            raw = v.encode("utf-8", errors="surrogatepass")
            out.append(T_TEXT)
            put_varint(out, len(raw))
            out += raw
        else:
            out.append(T_BLOB)
            put_varint(out, len(v))
            out += v
    return bytes(out)


def decode_record(data: bytes | memoryview, pos: int = 0) -> tuple[tuple[Value, ...], int]:
    count, pos = get_varint(data, pos)
    values: list[Value] = []
    for _ in range(count):
        tag = data[pos]
        pos += 1
        if tag == T_NULL:
            values.append(None)
        elif tag == T_ZERO:
            values.append(0)
        elif tag == T_ONE:
            values.append(1)
        elif tag == T_INT:
            z, pos = get_varint(data, pos)
            values.append(_unzigzag(z))
        elif tag == T_REAL:
            values.append(_DOUBLE.unpack_from(data, pos)[0])
            pos += 8
        elif tag == T_TEXT:
            n, pos = get_varint(data, pos)
            values.append(bytes(data[pos : pos + n]).decode("utf-8", errors="surrogatepass"))
            pos += n
        elif tag == T_BLOB:
            n, pos = get_varint(data, pos)
            values.append(bytes(data[pos : pos + n]))
            pos += n
        else:
            raise CorruptDatabaseError(f"unknown value tag {tag}")
    return tuple(values), pos
