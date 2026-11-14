"""SQL values, type affinity, comparison and arithmetic.

Quarry follows SQLite's dynamic type system, so a database behaves the way people who know SQLite expect:

* Values are NULL (``None``), INTEGER (``int``), REAL (``float``), TEXT (``str``) or BLOB (``bytes``).
* A column has an *affinity* derived from its declared type. The affinity converts values on insert:
  ``'12'`` stored in an ``INTEGER`` column becomes ``12``.
* Values of different types compare in a fixed order: NULL < numbers < text < blobs.
"""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Union

Value = Union[None, int, float, str, bytes]

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

# affinities
INTEGER = "INTEGER"
REAL = "REAL"
NUMERIC = "NUMERIC"
TEXT = "TEXT"
BLOB = "BLOB"  # also "no affinity"

_NUMBER_RE = re.compile(r"\s*([+-]?)(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?\s*\Z")
_PREFIX_RE = re.compile(r"\s*[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")


def type_name(v: Value) -> str:
    """The result of ``typeof(v)``."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "integer"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "real"
    if isinstance(v, str):
        return "text"
    return "blob"


def affinity_of(declared: str | None) -> str:
    """Column affinity from a declared type, using SQLite's five rules in order."""
    t = (declared or "").upper()
    if "INT" in t:
        return INTEGER
    if "CHAR" in t or "CLOB" in t or "TEXT" in t:
        return TEXT
    if "BLOB" in t or not t:
        return BLOB
    if "REAL" in t or "FLOA" in t or "DOUB" in t:
        return REAL
    return NUMERIC


def normalize(v: object) -> Value:
    """Turn a Python value from the caller into a SQL value."""
    if v is None or isinstance(v, (int, float, str, bytes)):
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, int) and not INT64_MIN <= v <= INT64_MAX:
            from .errors import DataError

            raise DataError(f"integer {v} is too large: Quarry stores 64-bit integers")
        if isinstance(v, float) and math.isnan(v):
            return None
        return v
    if isinstance(v, (bytearray, memoryview)):
        return bytes(v)
    from .errors import InterfaceError

    raise InterfaceError(f"cannot bind a value of type {type(v).__name__}: use None, int, float, str or bytes")


# ---------------------------------------------------------------- text conversions


def format_real(f: float) -> str:
    """REAL to TEXT, using the shortest digits that read back as the same number."""
    if math.isinf(f):
        return "Inf" if f > 0 else "-Inf"
    if f == 0:
        return "0.0"
    if f.is_integer() and abs(f) < 1e17:
        return f"{int(f)}.0"
    text = "%.15g" % f
    if float(text) != f:
        text = "%.17g" % f
    if "e" in text:
        mantissa, exp = text.split("e")
        if "." not in mantissa:
            mantissa += ".0"
        return f"{mantissa}e{exp[0]}{exp[1:].zfill(2)}"
    if "." not in text:
        text += ".0"
    return text


def to_text(v: Value) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return format_real(v)
    return v.decode("utf-8", errors="replace")


def _int_or_real(text_number: str) -> int | float:
    """A well-formed numeric literal to INTEGER when it has no fraction or exponent, else REAL."""
    s = text_number.strip()
    if re.fullmatch(r"[+-]?\d+", s):
        n = int(s)
        if INT64_MIN <= n <= INT64_MAX:
            return n
        return float(n)
    return float(s)


def numeric_prefix(v: Value) -> int | float:
    """The number at the start of a value, as SQLite uses for arithmetic: ``'12abc'`` is 12, ``'abc'`` is 0."""
    if isinstance(v, (int, float)):
        return v
    if v is None:
        return 0
    s = v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v
    m = _PREFIX_RE.match(s)
    if not m:
        return 0
    return _int_or_real(m.group(0))


def to_number(v: Value) -> int | float | None:
    """Numeric value of an operand for arithmetic. NULL stays NULL."""
    if v is None or isinstance(v, (int, float)):
        return v
    return numeric_prefix(v)


def to_real(v: Value) -> float | None:
    n = to_number(v)
    return None if n is None else float(n)


def to_integer(v: Value) -> int | None:
    """CAST(v AS INTEGER)."""
    if v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if math.isnan(v):
            return 0
        if v >= 9.223372036854775807e18:
            return INT64_MAX
        if v <= -9.223372036854775808e18:
            return INT64_MIN
        return int(v)
    s = v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v
    m = re.match(r"\s*([+-]?\d+)", s)
    if not m:
        return 0
    n = int(m.group(1))
    return max(INT64_MIN, min(INT64_MAX, n))


def is_true(v: Value) -> bool | None:
    """Truth value of a condition: NULL is unknown, numbers are true unless zero, text is converted first."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v != 0
    return numeric_prefix(v) != 0


def apply_affinity(v: Value, affinity: str) -> Value:
    """Convert a value as it is stored in a column with this affinity."""
    if v is None or affinity == BLOB:
        return v
    if affinity == TEXT:
        if isinstance(v, (int, float)):
            return to_text(v)
        return v
    if affinity in (INTEGER, NUMERIC):
        if isinstance(v, str):
            if _NUMBER_RE.match(v):
                n = _int_or_real(v)
                if isinstance(n, float) and n.is_integer() and INT64_MIN <= n <= INT64_MAX:
                    return int(n)
                return n
            return v
        if isinstance(v, float) and v.is_integer() and -9.2e18 < v < 9.2e18:
            return int(v)
        return v
    if affinity == REAL:
        if isinstance(v, int):
            return float(v)
        if isinstance(v, str) and _NUMBER_RE.match(v):
            return float(_int_or_real(v))
        return v
    return v


def cast(v: Value, type_name_: str) -> Value:
    """CAST(v AS type)."""
    if v is None:
        return None
    aff = affinity_of(type_name_)
    if aff == INTEGER:
        return to_integer(v)
    if aff == REAL:
        if isinstance(v, (int, float)):
            return float(v)
        return float(numeric_prefix(v))
    if aff == TEXT:
        return to_text(v)
    if aff == NUMERIC:
        if isinstance(v, (int, float)):
            return v
        n = numeric_prefix(v)
        return int(n) if isinstance(n, float) and n.is_integer() and abs(n) < 9.2e18 else n
    # BLOB
    if isinstance(v, bytes):
        return v
    text = to_text(v)
    return text.encode("utf-8") if text is not None else None


# ---------------------------------------------------------------- ordering


def sort_key(v: Value) -> tuple:
    """A key that orders values the way SQL does: NULL, numbers, text, blobs."""
    if v is None:
        return (0, 0)
    if isinstance(v, (int, float)):
        return (1, v)
    if isinstance(v, str):
        return (2, v)
    return (3, v)


def compare(a: Value, b: Value) -> int:
    """-1, 0 or 1 for two non-NULL values in SQL order."""
    ka, kb = sort_key(a), sort_key(b)
    return (ka > kb) - (ka < kb)


def comparison_affinity(left: str | None, right: str | None) -> tuple[str | None, str | None]:
    """Which affinity to apply to each side of a comparison (``None`` means leave the value alone).

    Mirrors SQLite: a numeric side converts a text or untyped side to a number, and a TEXT side converts an
    untyped side to text. ``left``/``right`` are the operands' affinities, or ``None`` for expressions
    without one (literals, most function results).
    """
    numeric = (INTEGER, REAL, NUMERIC)
    # BLOB affinity (an untyped column) counts as an affinity: only expressions without one get TEXT applied
    if left in numeric and right not in numeric:
        return None, NUMERIC
    if right in numeric and left not in numeric:
        return NUMERIC, None
    if left == TEXT and right is None:
        return None, TEXT
    if right == TEXT and left is None:
        return TEXT, None
    return None, None


# ---------------------------------------------------------------- arithmetic


def _fit(n: int) -> int | float:
    return n if INT64_MIN <= n <= INT64_MAX else float(n)


def add(a: Value, b: Value) -> Value:
    x, y = to_number(a), to_number(b)
    if x is None or y is None:
        return None
    if isinstance(x, int) and isinstance(y, int):
        return _fit(x + y)
    return float(x) + float(y)


def sub(a: Value, b: Value) -> Value:
    x, y = to_number(a), to_number(b)
    if x is None or y is None:
        return None
    if isinstance(x, int) and isinstance(y, int):
        return _fit(x - y)
    return float(x) - float(y)


def mul(a: Value, b: Value) -> Value:
    x, y = to_number(a), to_number(b)
    if x is None or y is None:
        return None
    if isinstance(x, int) and isinstance(y, int):
        return _fit(x * y)
    return float(x) * float(y)


def div(a: Value, b: Value) -> Value:
    x, y = to_number(a), to_number(b)
    if x is None or y is None or y == 0:
        return None
    if isinstance(x, int) and isinstance(y, int):
        q = abs(x) // abs(y)
        return _fit(q if (x >= 0) == (y >= 0) else -q)
    return float(x) / float(y)


def mod(a: Value, b: Value) -> Value:
    x, y = to_number(a), to_number(b)
    if x is None or y is None:
        return None
    real = isinstance(x, float) or isinstance(y, float)
    if real:
        xi, yi = _c_int(x), _c_int(y)
        if yi == -1:
            yi = 1
    else:
        xi, yi = int(x), int(y)
    if not yi:
        return None
    r = abs(xi) % abs(yi)
    r = r if xi >= 0 else -r
    return float(r) if real else r


def _c_int(v: int | float) -> int:
    """REAL to INTEGER the way SQLite's % does it: truncate, and clamp to the 64-bit range."""
    if isinstance(v, int):
        return v
    return to_integer(v) or 0


def negate(a: Value) -> Value:
    x = to_number(a)
    if x is None:
        return None
    return _fit(-x) if isinstance(x, int) else -x


def concat(a: Value, b: Value) -> Value:
    if a is None or b is None:
        return None
    return (to_text(a) or "") + (to_text(b) or "")


def bitop(op: str, a: Value, b: Value) -> Value:
    x, y = to_integer(to_number(a)), to_integer(to_number(b))
    if x is None or y is None:
        return None
    mask = (1 << 64) - 1
    if op == "&":
        r = x & y
    elif op == "|":
        r = x | y
    elif op == "<<":
        r = (x << y) & mask if 0 <= y < 64 else (x >> -y if y < 0 else 0)
    else:  # >>
        r = x >> y if 0 <= y < 64 else (x << -y) & mask if y < 0 else (-1 if x < 0 else 0)
    r &= mask
    return r - (1 << 64) if r > INT64_MAX else r


def round_half_away(x: float, digits: int = 0) -> float:
    """round(), as SQLite does it: halves go away from zero, using the exact binary value."""
    digits = max(0, min(digits, 30))
    if not math.isfinite(x) or abs(x) >= 2.0**52:
        return x
    d = Decimal(x).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    return float(d)


def like(pattern: str, text: str, escape: str | None = None) -> bool:
    """SQL LIKE: ``%`` any run, ``_`` one character, case-insensitive for ASCII letters."""
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if escape is not None and ch == escape and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        elif ch.isascii() and ch.isalpha():
            out.append(f"[{ch.lower()}{ch.upper()}]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.fullmatch("".join(out), text, re.DOTALL) is not None


def glob(pattern: str, text: str) -> bool:
    """SQL GLOB: case-sensitive ``*``, ``?`` and ``[...]``."""
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        elif ch == "[":
            j = pattern.find("]", i + 2)
            if j < 0:
                out.append(re.escape(ch))
            else:
                body = pattern[i + 1 : j]
                if body.startswith("^"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(ch))
        i += 1
    return re.fullmatch("".join(out), text, re.DOTALL) is not None
