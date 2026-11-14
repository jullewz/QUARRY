"""Built-in SQL functions: scalar functions, aggregates, window functions and date/time."""

from __future__ import annotations

import datetime as _dt
import math
import random
import re
import time
from collections.abc import Callable
from typing import Any

from ..errors import SQLError
from ..values import (
    INT64_MAX,
    INT64_MIN,
    Value,
    add,
    format_real,
    is_true,
    numeric_prefix,
    round_half_away,
    sort_key,
    to_integer,
    to_number,
    to_text,
    type_name,
)
from ..values import glob as glob_match
from ..values import like as like_match


class FunctionError(SQLError):
    pass


# ---------------------------------------------------------------- scalar functions

ScalarFn = Callable[..., Value]
SCALARS: dict[str, tuple[ScalarFn, int, int, bool]] = {}  # name -> (fn, min args, max args (-1 any), deterministic)


def scalar(name: str, min_args: int, max_args: int | None = None, deterministic: bool = True):  # type: ignore[no-untyped-def]
    def wrap(fn: ScalarFn) -> ScalarFn:
        SCALARS[name] = (fn, min_args, min_args if max_args is None else max_args, deterministic)
        return fn

    return wrap


def _text(v: Value) -> str | None:
    return to_text(v)


@scalar("abs", 1)
def f_abs(v: Value) -> Value:
    if v is None:
        return None
    if isinstance(v, (str, bytes)):
        return abs(float(numeric_prefix(v)))  # text and blobs become REAL, like SQLite
    n = to_number(v)
    if n is None:
        return None
    if isinstance(n, int):
        if n == INT64_MIN:
            raise FunctionError("integer overflow in abs()")
        return abs(n)
    return abs(n)


@scalar("length", 1)
def f_length(v: Value) -> Value:
    if v is None:
        return None
    if isinstance(v, bytes):
        return len(v)
    return len(to_text(v) or "")


@scalar("octet_length", 1)
def f_octet_length(v: Value) -> Value:
    if v is None:
        return None
    return len(v) if isinstance(v, bytes) else len((to_text(v) or "").encode())


@scalar("lower", 1)
def f_lower(v: Value) -> Value:
    t = _text(v)
    return None if t is None else "".join(c.lower() if c.isascii() else c for c in t)


@scalar("upper", 1)
def f_upper(v: Value) -> Value:
    t = _text(v)
    return None if t is None else "".join(c.upper() if c.isascii() else c for c in t)


def _substr(s: Any, start: int, length: int | None) -> Any:
    n = len(s)
    p1 = start
    p2 = length if length is not None else n + abs(p1) + 1
    negative_len = p2 < 0
    if negative_len:
        p2 = -p2
    if p1 < 0:
        p1 += n
        if p1 < 0:
            p2 += p1
            p1 = 0
    elif p1 > 0:
        p1 -= 1
    elif p2 > 0:
        p2 -= 1
    if negative_len:
        p1 -= p2
        if p1 < 0:
            p2 += p1
            p1 = 0
    p2 = max(0, p2)
    return s[p1 : p1 + p2]


@scalar("substr", 2, 3)
def f_substr(v: Value, start: Value, length: Value = "<absent>") -> Value:
    if v is None or start is None or length is None:
        return None
    st = to_integer(to_number(start)) or 0
    ln = None if length == "<absent>" else to_integer(to_number(length))
    if isinstance(v, bytes):
        return _substr(v, st, ln)
    return _substr(to_text(v) or "", st, ln)


SCALARS["substring"] = SCALARS["substr"]


def _trim(kind: str):  # type: ignore[no-untyped-def]
    def fn(v: Value, chars: Value = " ") -> Value:
        t = _text(v)
        c = _text(chars)
        if t is None or c is None:
            return None
        if kind == "l":
            return t.lstrip(c)
        if kind == "r":
            return t.rstrip(c)
        return t.strip(c)

    return fn


for _name, _kind in (("trim", "b"), ("ltrim", "l"), ("rtrim", "r")):
    SCALARS[_name] = (_trim(_kind), 1, 2, True)


@scalar("replace", 3)
def f_replace(v: Value, old: Value, new: Value) -> Value:
    t, o, n = _text(v), _text(old), _text(new)
    if t is None or o is None or n is None:
        return None
    return t if o == "" else t.replace(o, n)


@scalar("instr", 2)
def f_instr(hay: Value, needle: Value) -> Value:
    if hay is None or needle is None:
        return None
    if isinstance(hay, bytes) and isinstance(needle, bytes):
        return hay.find(needle) + 1
    return (to_text(hay) or "").find(to_text(needle) or "") + 1


@scalar("coalesce", 2, -1)
def f_coalesce(*args: Value) -> Value:
    for a in args:
        if a is not None:
            return a
    return None


@scalar("ifnull", 2)
def f_ifnull(a: Value, b: Value) -> Value:
    return b if a is None else a


@scalar("nullif", 2)
def f_nullif(a: Value, b: Value) -> Value:
    if a is not None and b is not None and sort_key(a) == sort_key(b):
        return None
    return a


@scalar("iif", 3)
def f_iif(cond: Value, a: Value, b: Value) -> Value:
    return a if is_true(cond) else b


@scalar("typeof", 1)
def f_typeof(v: Value) -> Value:
    return type_name(v)


@scalar("round", 1, 2)
def f_round(v: Value, digits: Value = 0) -> Value:
    if v is None or digits is None:
        return None
    n = to_number(v)
    d = to_integer(to_number(digits)) or 0
    assert n is not None
    if isinstance(n, int) and d >= 0:
        return float(n)
    return round_half_away(float(n), d)


@scalar("sign", 1)
def f_sign(v: Value) -> Value:
    n = to_number(v) if not isinstance(v, (str, bytes)) or re.fullmatch(r"\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*", str(v)) else None
    if n is None:
        return None
    return (n > 0) - (n < 0)


@scalar("hex", 1)
def f_hex(v: Value) -> Value:
    if v is None:
        return ""
    raw = v if isinstance(v, bytes) else (to_text(v) or "").encode()
    return raw.hex().upper()


@scalar("unhex", 1, 2)
def f_unhex(v: Value, ignore: Value = "") -> Value:
    t = _text(v)
    if t is None:
        return None
    for ch in _text(ignore) or "":
        t = t.replace(ch, "")
    try:
        return bytes.fromhex(t)
    except ValueError:
        return None


@scalar("quote", 1)
def f_quote(v: Value) -> Value:
    if v is None:
        return "NULL"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return format_real(v)
    if isinstance(v, bytes):
        return "X'" + v.hex().upper() + "'"
    return "'" + v.replace("'", "''") + "'"


@scalar("char", 0, -1)
def f_char(*codes: Value) -> Value:
    return "".join(chr(to_integer(to_number(c)) or 0) for c in codes if c is not None)


@scalar("unicode", 1)
def f_unicode(v: Value) -> Value:
    t = _text(v)
    return ord(t[0]) if t else None


@scalar("zeroblob", 1)
def f_zeroblob(n: Value) -> Value:
    return bytes(max(0, to_integer(to_number(n)) or 0))


@scalar("random", 0, 0, deterministic=False)
def f_random() -> Value:
    return random.randint(INT64_MIN, INT64_MAX)


@scalar("randomblob", 1, 1, deterministic=False)
def f_randomblob(n: Value) -> Value:
    return random.randbytes(max(1, to_integer(to_number(n)) or 1))


@scalar("max", 2, -1)
def f_max(*args: Value) -> Value:
    if any(a is None for a in args):
        return None
    best = args[0]
    for a in args[1:]:
        if sort_key(a) > sort_key(best):  # ties keep the earlier argument, like SQLite
            best = a
    return best


@scalar("min", 2, -1)
def f_min(*args: Value) -> Value:
    if any(a is None for a in args):
        return None
    best = args[0]
    for a in args[1:]:
        if sort_key(a) <= sort_key(best):  # ties take the later argument, like SQLite
            best = a
    return best


@scalar("like", 2, 3)
def f_like(pattern: Value, text: Value, escape: Value = None) -> Value:
    if pattern is None or text is None:
        return None
    return int(like_match(to_text(pattern) or "", to_text(text) or "", to_text(escape) if escape is not None else None))


@scalar("glob", 2)
def f_glob(pattern: Value, text: Value) -> Value:
    if pattern is None or text is None:
        return None
    return int(glob_match(to_text(pattern) or "", to_text(text) or ""))


@scalar("concat", 1, -1)
def f_concat(*args: Value) -> Value:
    return "".join(to_text(a) or "" for a in args)


@scalar("concat_ws", 2, -1)
def f_concat_ws(sep: Value, *args: Value) -> Value:
    if sep is None:
        return None
    return (to_text(sep) or "").join(to_text(a) or "" for a in args if a is not None)


# math functions
def _math1(fn: Callable[[float], float]):  # type: ignore[no-untyped-def]
    def wrapped(v: Value) -> Value:
        n = to_number(v)
        if n is None:
            return None
        try:
            r = fn(float(n))
        except (ValueError, OverflowError):
            return None
        return None if math.isnan(r) else r

    return wrapped


for _name, _fn in {
    "sqrt": math.sqrt, "exp": math.exp, "ln": math.log, "log10": math.log10, "log2": math.log2, "sin": math.sin,
    "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos, "atan": math.atan, "degrees": math.degrees,
    "radians": math.radians, "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
}.items():  # fmt: skip
    SCALARS[_name] = (_math1(_fn), 1, 1, True)


def _int_or_float(fn: Callable[[float], float]):  # type: ignore[no-untyped-def]
    def wrapped(v: Value) -> Value:
        n = to_number(v)
        if n is None:
            return None
        return n if isinstance(n, int) else float(fn(n))

    return wrapped


SCALARS["ceil"] = SCALARS["ceiling"] = (_int_or_float(math.ceil), 1, 1, True)
SCALARS["floor"] = (_int_or_float(math.floor), 1, 1, True)
SCALARS["trunc"] = (_int_or_float(math.trunc), 1, 1, True)


@scalar("pi", 0)
def f_pi() -> Value:
    return math.pi


@scalar("pow", 2)
def f_pow(a: Value, b: Value) -> Value:
    x, y = to_number(a), to_number(b)
    if x is None or y is None:
        return None
    try:
        return float(x) ** float(y)
    except (OverflowError, ZeroDivisionError):
        return None


SCALARS["power"] = SCALARS["pow"]


@scalar("log", 1, 2)
def f_log(a: Value, b: Value = "<absent>") -> Value:
    if b == "<absent>":
        return _math1(math.log10)(a)
    base, x = to_number(a), to_number(b)
    if base is None or x is None:
        return None
    try:
        return math.log(float(x), float(base))
    except (ValueError, ZeroDivisionError):
        return None


@scalar("mod", 2)
def f_mod(a: Value, b: Value) -> Value:
    x, y = to_number(a), to_number(b)
    if x is None or y is None or y == 0:
        return None
    return math.fmod(float(x), float(y))


@scalar("printf", 1, -1)
def f_printf(fmt: Value, *args: Value) -> Value:
    if fmt is None:
        return None
    return printf(to_text(fmt) or "", list(args))


SCALARS["format"] = SCALARS["printf"]

_SPEC = re.compile(r"%(?:(\d+)\$)?([-+ 0#,!]*)(\d+|\*)?(?:\.(\d+|\*))?([dioxXfFeEgGsqQcwz%])")


def printf(fmt: str, args: list[Value]) -> str:
    """SQLite's printf(): %d %f %e %g %s %x %o %c %q %Q %w and %%, with flags, width, precision and N$."""
    out = []
    pos = 0
    argi = 0

    def next_arg(explicit: str | None) -> Value:
        nonlocal argi
        if explicit:
            k = int(explicit) - 1
            return args[k] if 0 <= k < len(args) else None
        v = args[argi] if argi < len(args) else None
        argi += 1
        return v

    for m in _SPEC.finditer(fmt):
        out.append(fmt[pos : m.start()])
        pos = m.end()
        idx, flags, width, prec, conv = m.groups()
        if conv == "%":
            out.append("%")
            continue
        if width == "*":
            width = str(to_integer(to_number(next_arg(None))) or 0)
        if prec == "*":
            prec = str(to_integer(to_number(next_arg(None))) or 0)
        v = next_arg(idx)
        pyflags = flags.replace("!", "").replace(",", "")
        spec = "%" + pyflags + (width or "") + (f".{prec}" if prec is not None else "")
        if conv in "dioxX":
            n = to_integer(to_number(v)) or 0
            if conv in "xXo" and n < 0:
                n += 1 << 64
            text = (spec + ("d" if conv == "i" else conv)) % n
            if "," in flags:
                text = f"{n:,}".rjust(int(width or 0))
        elif conv in "fFeEgG":
            text = (spec + conv) % float(to_number(v) or 0)
        elif conv == "c":
            t = to_text(v) or ""
            text = (spec + "s") % (t[:1] if t else "\0")
        elif conv in "qQw":
            if v is None:
                text = "NULL" if conv == "Q" else "(NULL)"
            else:
                t = to_text(v) or ""
                if conv == "w":
                    text = t.replace('"', '""')
                else:
                    text = t.replace("'", "''")
                    if conv == "Q":
                        text = f"'{text}'"
            text = (spec + "s") % text
        elif conv in "zs":
            t = "" if v is None else (to_text(v) or "")
            text = (spec + "s") % t
        else:
            text = ""
        out.append(text)
    out.append(fmt[pos:])
    return "".join(out)


# ---------------------------------------------------------------- date and time

_DATE_RE = re.compile(
    r"\s*(?P<y>[+-]?\d{4})-(?P<m>\d\d)-(?P<d>\d\d)(?:[T ](?P<H>\d\d):(?P<M>\d\d)(?::(?P<S>\d\d(?:\.\d+)?))?)?\s*(?P<tz>Z|[+-]\d\d:\d\d)?\s*\Z"
)
_TIME_RE = re.compile(r"\s*(?P<H>\d\d):(?P<M>\d\d)(?::(?P<S>\d\d(?:\.\d+)?))?\s*(?P<tz>Z|[+-]\d\d:\d\d)?\s*\Z")
_EPOCH_JD = 2440587.5


def _jd(y: int, m: int, d: int, H: int = 0, M: int = 0, S: float = 0.0) -> float:
    """Julian day number of a proleptic Gregorian date and time."""
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    x1 = int(36525 * (y + 4716) / 100)
    x2 = int(306001 * (m + 1) / 10000)
    jd = x1 + x2 + d + b - 1524.5
    return jd + (H * 3600 + M * 60 + S) / 86400.0


def _from_jd(jd: float) -> tuple[int, int, int, int, int, float]:
    ms = round(jd * 86400000)
    z = int((ms + 43200000) // 86400000)
    a = int((z - 1867216.25) / 36524.25)
    a = z + 1 + a - a // 4
    b = a + 1524
    c = int((b - 122.1) / 365.25)
    d = int(365.25 * c)
    e = int((b - d) / 30.6001)
    day = b - d - int(30.6001 * e)
    month = e - 1 if e < 14 else e - 13
    year = c - 4716 if month > 2 else c - 4715
    day_ms = (ms + 43200000) % 86400000
    H = day_ms // 3600000
    M = (day_ms // 60000) % 60
    S = (day_ms % 60000) / 1000.0
    return year, month, day, int(H), int(M), S


def _parse_time(v: Value) -> float | None:
    """Julian day from a time value: text, a julian day number or 'now'."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = to_text(v) or ""
    if t.strip().lower() == "now":
        return time.time() / 86400.0 + _EPOCH_JD
    m = _DATE_RE.match(t)
    if m:
        try:
            _dt.date(int(m["y"]), int(m["m"]), int(m["d"]))
        except ValueError:
            return None
        jd = _jd(int(m["y"]), int(m["m"]), int(m["d"]), int(m["H"] or 0), int(m["M"] or 0), float(m["S"] or 0))
        return _apply_tz(jd, m["tz"])
    m = _TIME_RE.match(t)
    if m:
        jd = _jd(2000, 1, 1, int(m["H"]), int(m["M"]), float(m["S"] or 0))
        return _apply_tz(jd, m["tz"])
    if re.fullmatch(r"\s*[+-]?(\d+\.?\d*|\.\d+)\s*", t):
        return float(t)
    return None


def _apply_tz(jd: float, tz: str | None) -> float:
    if not tz or tz == "Z":
        return jd
    sign = -1 if tz[0] == "+" else 1
    return jd + sign * (int(tz[1:3]) * 60 + int(tz[4:6])) / 1440.0


_MOD_RE = re.compile(r"\s*([+-]?\d+(?:\.\d*)?)\s+(day|hour|minute|second|month|year)s?\s*\Z", re.I)


def _modify(jd: float, mod: str, first: bool, raw: Value) -> float | None:
    m = mod.strip().lower()
    if m == "unixepoch":
        if not first or not isinstance(raw, (int, float, str)):
            return None
        return float(numeric_prefix(raw)) / 86400.0 + _EPOCH_JD
    if m in ("utc", "localtime"):
        y, mo, d, H, M, S = _from_jd(jd)
        try:
            stamp = _dt.datetime(y, mo, d, H, M, int(S), tzinfo=_dt.UTC if m == "localtime" else None)
        except ValueError:
            return None
        if m == "localtime":
            local = stamp.astimezone().replace(tzinfo=None)
        else:
            local = stamp.astimezone(_dt.UTC).replace(tzinfo=None)
        return _jd(local.year, local.month, local.day, local.hour, local.minute, local.second + S % 1)
    if m == "auto" or m == "subsec" or m == "subsecond":
        return jd
    if m.startswith("start of "):
        y, mo, d, *_ = _from_jd(jd)
        unit = m[9:].strip()
        if unit == "day":
            return _jd(y, mo, d)
        if unit == "month":
            return _jd(y, mo, 1)
        if unit == "year":
            return _jd(y, 1, 1)
        return None
    if m.startswith("weekday "):
        try:
            target = int(m[8:])
        except ValueError:
            return None
        day = int(jd + 1.5) % 7
        delta = (target - day) % 7
        return jd + delta
    mm = _MOD_RE.match(m)
    if not mm:
        return None
    n = float(mm.group(1))
    unit = mm.group(2).lower()
    if unit in ("month", "year"):
        y, mo, d, H, M, S = _from_jd(jd)
        months = int(n) * (12 if unit == "year" else 1)
        total = y * 12 + (mo - 1) + months
        y2, mo2 = divmod(total, 12)
        base = _jd(y2, mo2 + 1, 1, H, M, S) + (d - 1)
        frac = n - int(n)
        return base + frac * (365.25 if unit == "year" else 30.0)
    seconds = {"day": 86400, "hour": 3600, "minute": 60, "second": 1}[unit]
    return jd + n * seconds / 86400.0


def _resolve(args: tuple[Value, ...]) -> float | None:
    if not args:
        jd = _parse_time("now")
    else:
        first = args[0]
        mods = [to_text(a) or "" for a in args[1:]]
        if mods and mods[0].strip().lower() == "unixepoch":
            jd = _modify(0.0, "unixepoch", True, first)
            mods = mods[1:]
        else:
            jd = _parse_time(first)
        for mod in mods:
            if jd is None:
                return None
            jd = _modify(jd, mod, False, first)
    return jd


def _fmt_date(jd: float) -> str:
    y, m, d, *_ = _from_jd(jd)
    return f"{y:04d}-{m:02d}-{d:02d}"


def _fmt_time(jd: float) -> str:
    *_, H, M, S = _from_jd(jd)
    return f"{H:02d}:{M:02d}:{int(S):02d}"


@scalar("date", 0, -1, deterministic=False)
def f_date(*args: Value) -> Value:
    jd = _resolve(args)
    return None if jd is None else _fmt_date(jd)


@scalar("time", 0, -1, deterministic=False)
def f_time(*args: Value) -> Value:
    jd = _resolve(args)
    return None if jd is None else _fmt_time(jd)


@scalar("datetime", 0, -1, deterministic=False)
def f_datetime(*args: Value) -> Value:
    jd = _resolve(args)
    return None if jd is None else f"{_fmt_date(jd)} {_fmt_time(jd)}"


@scalar("julianday", 0, -1, deterministic=False)
def f_julianday(*args: Value) -> Value:
    return _resolve(args)


@scalar("unixepoch", 0, -1, deterministic=False)
def f_unixepoch(*args: Value) -> Value:
    jd = _resolve(args)
    return None if jd is None else int(math.floor((jd - _EPOCH_JD) * 86400 + 1e-6))


@scalar("strftime", 1, -1, deterministic=False)
def f_strftime(fmt: Value, *args: Value) -> Value:
    if fmt is None:
        return None
    jd = _resolve(args) if args else _resolve(("now",))
    if jd is None:
        return None
    y, m, d, H, M, S = _from_jd(jd)
    weekday = int(jd + 1.5) % 7
    day_of_year = int(_jd(y, m, d) - _jd(y, 1, 1)) + 1
    out = []
    f = to_text(fmt) or ""
    i = 0
    while i < len(f):
        c = f[i]
        if c != "%" or i + 1 >= len(f):
            out.append(c)
            i += 1
            continue
        k = f[i + 1]
        i += 2
        if k == "Y":
            out.append(f"{y:04d}")
        elif k == "m":
            out.append(f"{m:02d}")
        elif k == "d":
            out.append(f"{d:02d}")
        elif k == "e":
            out.append(f"{d:2d}")
        elif k == "H":
            out.append(f"{H:02d}")
        elif k == "M":
            out.append(f"{M:02d}")
        elif k == "S":
            out.append(f"{int(S):02d}")
        elif k == "f":
            out.append(f"{S:06.3f}")
        elif k == "s":
            out.append(str(int(math.floor((jd - _EPOCH_JD) * 86400 + 1e-6))))
        elif k == "j":
            out.append(f"{day_of_year:03d}")
        elif k == "J":
            out.append(repr(jd))
        elif k == "w":
            out.append(str(weekday))
        elif k == "u":
            out.append(str(weekday or 7))
        elif k == "W":
            out.append(f"{(day_of_year + 6 - ((weekday + 6) % 7)) // 7:02d}")
        elif k == "F":
            out.append(f"{y:04d}-{m:02d}-{d:02d}")
        elif k == "T":
            out.append(f"{H:02d}:{M:02d}:{int(S):02d}")
        elif k == "%":
            out.append("%")
        else:
            return None
    return "".join(out)


# ---------------------------------------------------------------- aggregates


class Aggregate:
    """One group's running state. ``step`` is called per row, ``final`` once."""

    def step(self, *args: Value) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def final(self) -> Value:  # pragma: no cover - interface
        raise NotImplementedError

    # window support: remove a row that left the frame
    def inverse(self, *args: Value) -> None:
        raise NotImplementedError


class Count(Aggregate):
    def __init__(self, star: bool = False) -> None:
        self.n = 0
        self.star = star

    def step(self, *args: Value) -> None:
        if self.star or (args and args[0] is not None):
            self.n += 1

    def inverse(self, *args: Value) -> None:
        if self.star or (args and args[0] is not None):
            self.n -= 1

    def final(self) -> Value:
        return self.n


class Sum(Aggregate):
    def __init__(self, total: bool = False) -> None:
        self.int_sum = 0
        self.real_sum = 0.0
        self.has_real = False
        self.count = 0
        self.total = total

    def step(self, *args: Value) -> None:
        v = args[0]
        if v is None:
            return
        n = to_number(v)
        self.count += 1
        if isinstance(n, int):
            self.int_sum += n
        else:
            self.has_real = True
            self.real_sum += float(n)  # type: ignore[arg-type]

    def inverse(self, *args: Value) -> None:
        v = args[0]
        if v is None:
            return
        n = to_number(v)
        self.count -= 1
        if isinstance(n, int):
            self.int_sum -= n
        else:
            self.real_sum -= float(n)  # type: ignore[arg-type]

    def final(self) -> Value:
        if self.total:
            return float(self.int_sum) + self.real_sum
        if self.count == 0:
            return None
        if self.has_real:
            return float(self.int_sum) + self.real_sum
        if not INT64_MIN <= self.int_sum <= INT64_MAX:
            raise FunctionError("integer overflow in sum()")
        return self.int_sum


class Avg(Aggregate):
    def __init__(self) -> None:
        self.s = Sum()

    def step(self, *args: Value) -> None:
        self.s.step(*args)

    def inverse(self, *args: Value) -> None:
        self.s.inverse(*args)

    def final(self) -> Value:
        if self.s.count == 0:
            return None
        return (float(self.s.int_sum) + self.s.real_sum) / self.s.count


def _ckey(v: Value, collation: str | None) -> tuple:
    if collation and isinstance(v, str):
        if collation == "NOCASE":
            v = "".join(c.lower() if c.isascii() else c for c in v)
        elif collation == "RTRIM":
            v = v.rstrip(" ")
    return sort_key(v)


class MinMax(Aggregate):
    def __init__(self, is_max: bool, collation: str | None = None) -> None:
        self.best: Value = None
        self.is_max = is_max
        self.collation = collation
        self.values: list[Value] | None = None  # kept only for sliding windows

    def step(self, *args: Value) -> None:
        v = args[0]
        if self.values is not None:
            self.values.append(v)
        if v is None:
            return
        if self.best is None:
            self.best = v
        elif self.is_max and _ckey(v, self.collation) > _ckey(self.best, self.collation):
            self.best = v
        elif not self.is_max and _ckey(v, self.collation) < _ckey(self.best, self.collation):
            self.best = v

    def inverse(self, *args: Value) -> None:
        if self.values is None:
            raise NotImplementedError
        self.values.remove(args[0]) if args[0] in self.values else None
        present = [v for v in self.values if v is not None]
        self.best = (max if self.is_max else min)(present, key=sort_key) if present else None

    def final(self) -> Value:
        return self.best


class GroupConcat(Aggregate):
    def __init__(self) -> None:
        self.parts: list[tuple[str, str]] = []

    def step(self, *args: Value) -> None:
        v = args[0]
        if v is None:
            return
        sep = "," if len(args) < 2 else (to_text(args[1]) if args[1] is not None else "")
        self.parts.append((to_text(v) or "", sep or ""))

    def inverse(self, *args: Value) -> None:
        if args[0] is not None and self.parts:
            self.parts.pop(0)

    def final(self) -> Value:
        if not self.parts:
            return None
        out = self.parts[0][0]
        for text, sep in self.parts[1:]:
            out += sep + text
        return out


class Distinct(Aggregate):
    """Wraps an aggregate so it sees each distinct value once."""

    def __init__(self, inner: Aggregate, collation: str | None = None) -> None:
        self.inner = inner
        self.seen: set = set()
        self.collation = collation

    def step(self, *args: Value) -> None:
        if args and args[0] is None:
            return
        k = tuple(_ckey(a, self.collation) for a in args[:1])
        if k in self.seen:
            return
        self.seen.add(k)
        self.inner.step(*args)

    def final(self) -> Value:
        return self.inner.final()


AGGREGATES: dict[str, tuple[Callable[[], Aggregate], int, int]] = {
    "count": (lambda: Count(), 0, 1),
    "sum": (lambda: Sum(), 1, 1),
    "total": (lambda: Sum(total=True), 1, 1),
    "avg": (lambda: Avg(), 1, 1),
    "min": (lambda: MinMax(False), 1, 1),
    "max": (lambda: MinMax(True), 1, 1),
    "group_concat": (lambda: GroupConcat(), 1, 2),
    "string_agg": (lambda: GroupConcat(), 2, 2),
}

WINDOW_ONLY = {
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile", "lag", "lead", "first_value",
    "last_value", "nth_value",
}  # fmt: skip


def is_aggregate_call(name: str, nargs: int, star: bool) -> bool:
    """Whether ``name(args)`` is an aggregate. ``min``/``max`` with two or more arguments are scalar."""
    if name in ("min", "max"):
        return nargs == 1
    if name == "count":
        return True
    return name in AGGREGATES


def make_aggregate(name: str, star: bool, distinct: bool, collation: str | None = None) -> Aggregate:
    if name == "count":
        agg: Aggregate = Count(star)
    elif name in ("min", "max"):
        agg = MinMax(name == "max", collation)
    else:
        agg = AGGREGATES[name][0]()
    return Distinct(agg, collation) if distinct else agg


def check_arity(name: str, nargs: int, sql: str = "", pos: int = -1) -> None:
    if name in AGGREGATES or name == "count":
        lo, hi = (0, 1) if name == "count" else AGGREGATES[name][1:]
        if name in ("min", "max") and nargs >= 2:
            return
    elif name in SCALARS:
        _fn, lo, hi, _det = SCALARS[name]
    else:
        return
    if nargs < lo or (hi >= 0 and nargs > hi):
        want = f"{lo}" if lo == hi else (f"at least {lo}" if hi < 0 else f"{lo} to {hi}")
        raise SQLError(f"wrong number of arguments to function {name}(): expected {want}, got {nargs}", sql, pos)


__all__ = ["SCALARS", "AGGREGATES", "WINDOW_ONLY", "Aggregate", "make_aggregate", "is_aggregate_call", "add"]
