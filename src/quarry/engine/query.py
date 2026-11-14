"""Query planning and execution for SELECT.

A query compiles into a tree of plans. Each plan's ``run(env)`` yields result tuples lazily, so ``LIMIT``
stops work early. The planner places every WHERE condition at the earliest join level where all its columns
are known, and picks an access path per table:

* ``SEARCH t USING INTEGER PRIMARY KEY``: rowid equality, IN list or range
* ``SEARCH t USING INDEX i``: equality on a prefix of an index, optionally followed by a range
* ``HASH JOIN``: an equality join with no usable index builds a hash table of the inner table once
* ``SCAN t``: everything else

Conditions used for a lookup are still checked on every row, so a lookup only has to return a superset of
the matching rows. That keeps the planner simple and the results exact.
"""

from __future__ import annotations

import functools
import itertools
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from ..errors import NotSupported, SQLError
from ..sql import ast as A
from ..storage.btree import BTree
from ..storage.encoding import decode_record
from ..values import (
    INTEGER,
    NUMERIC,
    Value,
    apply_affinity,
    comparison_affinity,
    is_true,
    sort_key,
    to_integer,
    to_number,
)
from .catalog import ROWID_NAMES, TableInfo
from .expr import (
    Compiled,
    Context,
    Env,
    ExprCompiler,
    Scope,
    Source,
    collate_key,
    compare_values,
    find_aggregates,
    find_windows,
    walk,
)
from .functions import make_aggregate

Row = tuple


# ---------------------------------------------------------------- plans


class Plan:
    columns: list[str]
    affinities: list[str | None]
    collations: list[str | None]
    correlated: bool = False

    def run(self, env: Env) -> Iterator[Row]:  # pragma: no cover - interface
        raise NotImplementedError

    def explain(self, depth: int, out: list[tuple[int, str]]) -> None:  # pragma: no cover - interface
        raise NotImplementedError


def explain_lines(plan: Plan) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    plan.explain(0, out)
    return out


# ---------------------------------------------------------------- table access


def table_row_reader(table: TableInfo) -> Callable[[tuple, bytes], Row]:
    """Turn a (key, record) pair into a row: declared columns followed by the rowid."""
    ncols = len(table.columns)
    alias = table.rowid_alias
    defaults: list[Value] = []
    for c in table.columns:
        d = None
        if c.default is not None and isinstance(c.default, A.Literal):
            d = c.default.value
        defaults.append(d)

    def read(key: tuple, raw: bytes) -> Row:
        values, _ = decode_record(raw)
        if len(values) < ncols:  # rows written before ALTER TABLE ADD COLUMN
            values = values + tuple(defaults[len(values) :])
        rowid = key[0]
        if alias is not None:
            values = values[:alias] + (rowid,) + values[alias + 1 : ncols]
        return values[:ncols] + (rowid,)

    return read


@dataclass
class Lookup:
    """How one join level fetches candidate rows."""

    kind: str  # scan, rowid_eq, rowid_in, rowid_range, index_eq, index_in, index_range, hash, plan
    detail: str = ""
    index: Any = None
    eq: list[Callable[[Env], Value]] = field(default_factory=list)  # probe values for index/rowid equality
    eq_aff: list[str | None] = field(default_factory=list)
    in_values: list[Callable[[Env], Value]] = field(default_factory=list)
    lo: Callable[[Env], Value] | None = None
    lo_incl: bool = True
    hi: Callable[[Env], Value] | None = None
    hi_incl: bool = True
    range_aff: str | None = None
    hash_inner: Callable[[Env], Value] | None = None
    hash_outer: Callable[[Env], Value] | None = None
    hash_conv: tuple[str | None, str | None, str | None] = (None, None, None)  # inner aff, outer aff, collation
    build_filters: list[Callable[[Env], bool]] = field(default_factory=list)


@dataclass
class Level:
    source: Source
    join: str  # FIRST, INNER, LEFT, CROSS
    lookup: Lookup
    table: TableInfo | None = None
    plan: Plan | None = None
    on_filters: list[Callable[[Env], bool]] = field(default_factory=list)  # LEFT JOIN match conditions
    filters: list[Callable[[Env], bool]] = field(default_factory=list)  # applied after the row is formed
    working: Any = None  # recursive CTE working set holder


def _numeric_probe(v: Value) -> int | None | str:
    """A probe value for rowid lookups: an integer, None (matches nothing) or 'any' (cannot narrow)."""
    if v is None:
        return None
    v = apply_affinity(v, NUMERIC)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else None
    return None  # text never equals an integer rowid


class Fetcher:
    """Executes a Level's lookup for the current outer row."""

    def __init__(self, level: Level, pager: Any) -> None:
        self.level = level
        self.pager = pager
        self.reader = table_row_reader(level.table) if level.table else None

    def tree(self) -> BTree:
        assert self.level.table is not None
        return BTree(self.pager, self.level.table.root)

    def rows(self, env: Env, state: dict) -> Iterator[Row]:
        lk = self.level.lookup
        kind = lk.kind
        read = self.reader
        if kind == "plan":
            plan = self.level.plan
            assert plan is not None
            penv = env.outer if env.outer is not None else Env((), None, env.ctx)
            if plan.correlated:
                yield from plan.run(penv)
                return
            key = id(self.level)
            cached = state.get(key)
            if cached is None:
                cached = state[key] = list(plan.run(penv))
            yield from cached
            return
        if kind == "working":
            yield from self.level.working.current
            return
        if kind == "hash":
            yield from self._hash(env, state)
            return
        assert read is not None
        tree = self.tree()
        if kind == "scan":
            for k, v in tree.scan():
                yield read(k, v)
            return
        if kind == "rowid_eq":
            rid = _numeric_probe(lk.eq[0](env))
            if rid is None:
                return
            raw = tree.get((rid,))
            if raw is not None:
                yield read((rid,), raw)
            return
        if kind == "rowid_in":
            seen = set()
            for f in lk.in_values:
                rid = _numeric_probe(f(env))
                if rid is None or rid in seen:
                    continue
                seen.add(rid)
                raw = tree.get((rid,))
                if raw is not None:
                    yield read((rid,), raw)
            return
        if kind == "rowid_range":
            lo = hi = None
            if lk.lo is not None:
                v = lk.lo(env)
                if v is None:
                    return
                v = apply_affinity(v, NUMERIC)
                if not isinstance(v, (int, float)):
                    return  # rowid > 'text' matches nothing: text sorts after numbers
                lo = (v,)
            if lk.hi is not None:
                v = lk.hi(env)
                if v is None:
                    return
                v = apply_affinity(v, NUMERIC)
                if isinstance(v, (int, float)):
                    hi = (v,)
            for k, val in tree.scan(lo, lk.lo_incl, hi, lk.hi_incl):
                yield read(k, val)
            return
        # index lookups: collect rowids, then fetch rows
        ix = lk.index
        itree = BTree(self.pager, ix.root)
        if kind == "index_eq":
            probe = []
            for f, aff in zip(lk.eq, lk.eq_aff):
                v = f(env)
                if v is None:
                    return
                probe.append(apply_affinity(v, aff) if aff else v)
            lo_t = tuple(probe)
            if lk.lo is not None or lk.hi is not None:
                start, start_incl, end, end_incl = lo_t, True, lo_t, True
                if lk.lo is not None:
                    v = lk.lo(env)
                    if v is None:
                        return
                    start = lo_t + (apply_affinity(v, lk.range_aff) if lk.range_aff else v,)
                    start_incl = lk.lo_incl
                else:
                    start = lo_t + (None,)  # skip NULLs: a range comparison is never true for NULL
                    start_incl = False
                if lk.hi is not None:
                    v = lk.hi(env)
                    if v is None:
                        return
                    end = lo_t + (apply_affinity(v, lk.range_aff) if lk.range_aff else v,)
                    end_incl = lk.hi_incl
                entries = itree.scan(start, start_incl, end, end_incl, prefix=True)
            else:
                entries = itree.scan(lo_t, True, lo_t, True, prefix=True)
            rids = [k[-1] for k, _ in entries]
        elif kind == "index_in":
            rids = []
            seen_vals = set()
            for f in lk.in_values:
                v = f(env)
                if v is None:
                    continue
                if lk.eq_aff and lk.eq_aff[0]:
                    v = apply_affinity(v, lk.eq_aff[0])
                sk = sort_key(v)
                if sk in seen_vals:
                    continue
                seen_vals.add(sk)
                rids += [k[-1] for k, _ in itree.scan((v,), True, (v,), True, prefix=True)]
        else:  # index_range
            start = end = None
            start_incl = end_incl = True
            if lk.lo is not None:
                v = lk.lo(env)
                if v is None:
                    return
                start = (apply_affinity(v, lk.range_aff) if lk.range_aff else v,)
                start_incl = lk.lo_incl
            else:
                start, start_incl = (None,), False
            if lk.hi is not None:
                v = lk.hi(env)
                if v is None:
                    return
                end = (apply_affinity(v, lk.range_aff) if lk.range_aff else v,)
                end_incl = lk.hi_incl
            rids = [k[-1] for k, _ in itree.scan(start, start_incl, end, end_incl, prefix=True)]
        for rid in rids:
            raw = tree.get((rid,))
            if raw is not None:
                yield read((rid,), raw)

    def _hash(self, env: Env, state: dict) -> Iterator[Row]:
        lk = self.level.lookup
        key = id(self.level)
        table = state.get(key)
        inner_aff, outer_aff, coll = lk.hash_conv
        if table is None:
            table = {}
            prefix_len = self.level.source.offset
            pad = (None,) * prefix_len
            assert lk.hash_inner is not None
            source_rows: Iterator[Row]
            if self.level.plan is not None:
                penv = env.outer if env.outer is not None else Env((), None, env.ctx)
                source_rows = iter(list(self.level.plan.run(penv)))
            else:
                assert self.reader is not None
                source_rows = (self.reader(k, v) for k, v in self.tree().scan())
            for r in source_rows:
                e = Env(pad + r, env.outer, env.ctx)
                if not all(f(e) for f in lk.build_filters):
                    continue
                v = lk.hash_inner(e)
                if v is None:
                    continue
                if inner_aff:
                    v = apply_affinity(v, inner_aff)
                table.setdefault(sort_key(collate_key(v, coll)), []).append(r)
            state[key] = table
        assert lk.hash_outer is not None
        v = lk.hash_outer(env)
        if v is None:
            return
        if outer_aff:
            v = apply_affinity(v, outer_aff)
        yield from table.get(sort_key(collate_key(v, coll)), ())


# ---------------------------------------------------------------- SELECT core


@dataclass
class OrderKey:
    fn: Callable[[Env], Value] | None  # None: use the output column at ``column``
    column: int
    descending: bool
    nulls_first: bool | None
    collation: str | None


def make_sort_key(keys: list[OrderKey]) -> Callable[[tuple], Any]:
    def cmp(a: tuple, b: tuple) -> int:
        for i, k in enumerate(keys):
            x, y = a[i], b[i]
            if x is None or y is None:
                if x is None and y is None:
                    continue
                nulls_first = k.nulls_first if k.nulls_first is not None else not k.descending
                return (-1 if x is None else 1) * (1 if nulls_first else -1)
            c = compare_values(x, y, k.collation)
            if c:
                return -c if k.descending else c
        return 0

    return functools.cmp_to_key(cmp)


class CorePlan(Plan):
    """One SELECT ... FROM ... WHERE ... GROUP BY ... HAVING."""

    def __init__(self) -> None:
        self.levels: list[Level] = []
        self.fetchers: list[Fetcher] = []
        self.final_filters: list[Callable[[Env], bool]] = []
        self.width = 0
        self.grouped = False
        self.group_fns: list[Callable[[Env], Value]] = []
        self.group_colls: list[str | None] = []
        self.aggs: list[tuple[A.Func, list[Callable[[Env], Value]], Callable[[Env], bool] | None]] = []
        self.agg_colls: list[str | None] = []
        self.minmax_rep = False
        self.having: Callable[[Env], bool] | None = None
        self.windows: list[WindowPlan] = []
        self.outputs: list[Callable[[Env], Value]] = []
        self.order: list[OrderKey] = []
        self.distinct = False
        self.limit: Callable[[Env], Value] | None = None
        self.offset: Callable[[Env], Value] | None = None
        self.columns = []
        self.affinities = []
        self.collations = []
        self.notes: list[str] = []
        self.subplans: list[tuple[str, Plan]] = []
        self.no_from = False

    # -------------------------------------------------- execution

    def join_rows(self, outer: Env) -> Iterator[Row]:
        ctx = outer.ctx
        state: dict = {}
        levels = self.levels
        fetchers = self.fetchers
        n = len(levels)
        if n == 0:
            yield ()
            return
        parent = outer

        def rec(i: int, prefix: Row) -> Iterator[Row]:
            lvl = levels[i]
            env = Env(prefix, parent, ctx)
            last = i == n - 1
            matched = False
            for r in fetchers[i].rows(env, state):
                row = prefix + r
                e = Env(row, parent, ctx)
                if lvl.on_filters and not all(f(e) for f in lvl.on_filters):
                    continue
                matched = True
                if lvl.filters and not all(f(e) for f in lvl.filters):
                    continue
                if last:
                    yield row
                else:
                    yield from rec(i + 1, row)
            if not matched and lvl.join == "LEFT":
                row = prefix + (None,) * lvl.source.width
                e = Env(row, parent, ctx)
                if lvl.filters and not all(f(e) for f in lvl.filters):
                    return
                if last:
                    yield row
                else:
                    yield from rec(i + 1, row)

        yield from rec(0, ())

    def filtered_rows(self, outer: Env) -> Iterator[Row]:
        ff = self.final_filters
        if not ff:
            yield from self.join_rows(outer)
            return
        for row in self.join_rows(outer):
            e = Env(row, outer, outer.ctx)
            if all(f(e) for f in ff):
                yield row

    def grouped_rows(self, outer: Env) -> Iterator[Row]:
        ctx = outer.ctx
        groups: dict[tuple, list] = {}
        order: list[tuple] = []
        aggs = self.aggs
        for row in self.filtered_rows(outer):
            e = Env(row, outer, ctx)
            if self.group_fns:
                key = tuple(sort_key(collate_key(f(e), c)) for f, c in zip(self.group_fns, self.group_colls))
            else:
                key = ()
            g = groups.get(key)
            if g is None:
                g = groups[key] = [row, [make_aggregate(f.name, f.star, f.distinct, c) for (f, _a, _fl), c in zip(aggs, self.agg_colls)]]
                order.append(key)
            states = g[1]
            for (_func, args, flt), st in zip(aggs, states):
                if flt is not None and not flt(e):
                    continue
                vals = [a(e) for a in args]
                if self.minmax_rep:
                    before = st.final()
                    st.step(*vals)
                    if st.final() is not before:
                        g[0] = row
                else:
                    st.step(*vals)
            if not self.minmax_rep:
                g[0] = row
        if not groups and not self.group_fns:
            groups[()] = [
                (None,) * self.width,
                [make_aggregate(f.name, f.star, f.distinct, c) for (f, _a, _fl), c in zip(aggs, self.agg_colls)],
            ]
            order.append(())
        for key in sorted(order, key=lambda k: k):
            rep, states = groups[key]
            yield rep + tuple(st.final() for st in states)

    def run(self, env: Env) -> Iterator[Row]:
        ctx = env.ctx
        rows: Iterator[Row] = self.grouped_rows(env) if self.grouped else self.filtered_rows(env)
        if self.having is not None:
            having = self.having
            rows = (r for r in rows if having(Env(r, env, ctx)))
        if self.windows:
            materialized = list(rows)
            for w in self.windows:
                materialized = w.apply(materialized, env)
            rows = iter(materialized)
        outputs = self.outputs
        order = self.order
        limit, offset = self._limits(env)
        if limit == 0:
            return
        if order:
            keyed = []
            seen: set | None = set() if self.distinct else None
            for r in rows:
                e = Env(r, env, ctx)
                out = tuple(f(e) for f in outputs)
                if seen is not None:
                    dk = tuple(sort_key(collate_key(v, c)) for v, c in zip(out, self.collations))
                    if dk in seen:
                        continue
                    seen.add(dk)
                keys = tuple(out[k.column] if k.fn is None else k.fn(e) for k in order)
                keyed.append((keys, out))
            sorter = make_sort_key(self.order)
            keyed.sort(key=lambda p: sorter(p[0]))
            result: Iterator[Row] = (out for _k, out in keyed)
        else:
            result = (tuple(f(Env(r, env, ctx)) for f in outputs) for r in rows)
            if self.distinct:
                colls = self.collations
                result = _dedupe_by(result, lambda r: tuple(sort_key(collate_key(v, c)) for v, c in zip(r, colls)))
        if offset:
            result = itertools.islice(result, offset, None)
        if limit is not None and limit >= 0:
            result = itertools.islice(result, limit)
        yield from result

    def _limits(self, env: Env) -> tuple[int | None, int]:
        limit = offset = None
        if self.limit is not None:
            limit = _int_clause(self.limit(env), "LIMIT")
            if limit is not None and limit < 0:
                limit = None
        if self.offset is not None:
            offset = _int_clause(self.offset(env), "OFFSET")
            if offset is not None and offset < 0:
                offset = 0
        return limit, offset or 0

    # -------------------------------------------------- explain

    def explain(self, depth: int, out: list[tuple[int, str]]) -> None:
        for lvl in self.levels:
            lk = lvl.lookup
            if lk.kind == "plan":
                out.append((depth, f"SCAN {lvl.source.name}" + (" (subquery)" if lvl.table is None else "")))
                if lvl.plan is not None:
                    lvl.plan.explain(depth + 1, out)
            elif lk.kind == "working":
                out.append((depth, f"SCAN {lvl.source.name} (recursive step)"))
            else:
                name = lvl.source.name
                tname = lvl.table.name if lvl.table else name
                label = tname if name.lower() == tname.lower() else f"{tname} AS {name}"
                if lk.kind == "scan":
                    text = f"SCAN {label}"
                elif lk.kind == "hash":
                    text = f"HASH JOIN {label} ON ({lk.detail})"
                    if lvl.plan is not None:
                        out.append((depth, text))
                        lvl.plan.explain(depth + 1, out)
                        continue
                elif lk.kind.startswith("rowid"):
                    text = f"SEARCH {label} USING INTEGER PRIMARY KEY ({lk.detail})"
                else:
                    text = f"SEARCH {label} USING INDEX {lk.index.name} ({lk.detail})"
                if lvl.join == "LEFT":
                    text += " LEFT JOIN"
                out.append((depth, text))
        if self.no_from and not self.levels:
            out.append((depth, "SCAN CONSTANT ROW"))
        for kind, plan in self.subplans:
            out.append((depth, kind))
            plan.explain(depth + 1, out)
        for note in self.notes:
            out.append((depth, note))


def _dedupe(rows: Iterator[Row]) -> Iterator[Row]:
    seen = set()
    for r in rows:
        k = tuple(sort_key(v) for v in r)
        if k not in seen:
            seen.add(k)
            yield r


def _int_clause(v: Value, what: str) -> int | None:
    if v is None:
        raise SQLError(f"{what} must be an integer, got NULL")
    if isinstance(v, str):
        n = to_number(v)
        if isinstance(n, int) and str(n) == v.strip():
            return n
        raise SQLError(f"datatype mismatch: {what} must be an integer, got {v!r}")
    if isinstance(v, float):
        if v.is_integer():
            return int(v)
        raise SQLError(f"datatype mismatch: {what} must be an integer, got {v}")
    if isinstance(v, bytes):
        raise SQLError(f"datatype mismatch: {what} must be an integer")
    return v


# ---------------------------------------------------------------- window functions


class WindowPlan:
    """Computes one window function call for every row and appends the result."""

    def __init__(
        self,
        func: A.Func,
        args: list[Callable[[Env], Value]],
        partition: list[Callable[[Env], Value]],
        order: list[OrderKey],
        frame: tuple | None,
        frame_fns: tuple[Callable[[Env], Value] | None, Callable[[Env], Value] | None],
    ) -> None:
        self.func = func
        self.args = args
        self.partition = partition
        self.order = order
        self.frame = frame
        self.frame_fns = frame_fns

    def apply(self, rows: list[Row], outer: Env) -> list[Row]:
        ctx = outer.ctx
        n = len(rows)
        envs = [Env(r, outer, ctx) for r in rows]
        pkeys = [tuple(sort_key(f(e)) for f in self.partition) for e in envs]
        okeys = [tuple(k.fn(e) if k.fn else None for k in self.order) for e in envs]
        sorter = make_sort_key(self.order)
        idx = sorted(range(n), key=lambda i: (pkeys[i], sorter(okeys[i])))
        results: list[Value] = [None] * n
        start = 0
        while start < n:
            end = start
            while end < n and pkeys[idx[end]] == pkeys[idx[start]]:
                end += 1
            part = idx[start:end]
            self._partition(part, envs, okeys, sorter, results, outer)
            start = end
        return [r + (results[i],) for i, r in enumerate(rows)]

    def _peers(self, part: list[int], okeys: list[tuple], sorter: Callable) -> list[int]:
        """Peer group number for each position (rows equal in ORDER BY share one)."""
        groups = []
        g = -1
        prev = None
        for pos, i in enumerate(part):
            k = sorter(okeys[i])
            if pos == 0 or not (prev == k):
                g += 1
            groups.append(g)
            prev = k
        return groups

    def _frame_bounds(self, pos: int, m: int, peers: list[int], env: Env) -> tuple[int, int]:
        """Inclusive start and exclusive end positions of the frame for the row at ``pos``."""
        if self.frame is None:
            if not self.order:
                return 0, m
            g = peers[pos]
            end = pos
            while end < m and peers[end] == g:
                end += 1
            return 0, end
        unit, lo, hi = self.frame
        lo_fn, hi_fn = self.frame_fns

        def bound(b: tuple, fn: Callable[[Env], Value] | None, is_start: bool) -> int:
            kind = b[0]
            if kind == "UNBOUNDED_PRECEDING":
                return 0
            if kind == "UNBOUNDED_FOLLOWING":
                return m
            if kind == "CURRENT":
                if unit == "ROWS":
                    return pos if is_start else pos + 1
                g = peers[pos]
                if is_start:
                    s = pos
                    while s > 0 and peers[s - 1] == g:
                        s -= 1
                    return s
                e = pos
                while e < m and peers[e] == g:
                    e += 1
                return e
            assert fn is not None
            off = to_integer(to_number(fn(env)))
            if off is None or off < 0:
                raise SQLError("frame offset must be a non-negative integer")
            if unit != "ROWS":
                raise NotSupported("RANGE frames with a numeric offset are not supported yet")
            if kind == "PRECEDING":
                p = pos - off
            else:
                p = pos + off
            return max(0, min(m, p if is_start else p + 1))

        s = bound(lo, lo_fn, True)
        e = bound(hi, hi_fn, False)
        return s, max(s, e)

    def _partition(self, part: list[int], envs: list[Env], okeys: list[tuple], sorter: Callable, results: list[Value], outer: Env) -> None:
        name = self.func.name
        m = len(part)
        peers = self._peers(part, okeys, sorter)
        if name == "row_number":
            for pos, i in enumerate(part):
                results[i] = pos + 1
            return
        if name in ("rank", "dense_rank", "percent_rank", "cume_dist"):
            rank = 1
            for pos, i in enumerate(part):
                if pos and peers[pos] != peers[pos - 1]:
                    rank = pos + 1
                if name == "rank":
                    results[i] = rank
                elif name == "dense_rank":
                    results[i] = peers[pos] + 1
                elif name == "percent_rank":
                    results[i] = (rank - 1) / (m - 1) if m > 1 else 0.0
                else:
                    last = pos
                    while last + 1 < m and peers[last + 1] == peers[pos]:
                        last += 1
                    results[i] = (last + 1) / m
            return
        if name == "ntile":
            buckets = to_integer(to_number(self.args[0](envs[part[0]]))) if self.args else None
            if not buckets or buckets <= 0:
                raise SQLError("argument of ntile must be a positive integer")
            base, extra = divmod(m, buckets)
            pos = 0
            for b in range(buckets):
                size = base + (1 if b < extra else 0)
                for _ in range(size):
                    results[part[pos]] = b + 1
                    pos += 1
            return
        if name in ("lag", "lead"):
            for pos, i in enumerate(part):
                e = envs[i]
                off = to_integer(to_number(self.args[1](e))) if len(self.args) > 1 else 1
                default = self.args[2](e) if len(self.args) > 2 else None
                target = pos - off if name == "lag" else pos + off  # type: ignore[operator]
                results[i] = self.args[0](envs[part[target]]) if 0 <= target < m else default
            return
        if name in ("first_value", "last_value", "nth_value"):
            for pos, i in enumerate(part):
                s, e = self._frame_bounds(pos, m, peers, envs[i])
                if s >= e:
                    results[i] = None
                    continue
                if name == "first_value":
                    results[i] = self.args[0](envs[part[s]])
                elif name == "last_value":
                    results[i] = self.args[0](envs[part[e - 1]])
                else:
                    k = to_integer(to_number(self.args[1](envs[i])))
                    if k is None or k <= 0:
                        raise SQLError("second argument to nth_value must be a positive integer")
                    results[i] = self.args[0](envs[part[s + k - 1]]) if s + k - 1 < e else None
            return
        # aggregate used as a window function
        func = self.func
        if func.distinct:
            raise NotSupported("DISTINCT is not supported in window aggregates")
        running = self.frame is None or self.frame[1][0] == "UNBOUNDED_PRECEDING"
        if running:
            agg = make_aggregate(name, func.star, False)
            added = 0
            for pos, i in enumerate(part):
                s, e = self._frame_bounds(pos, m, peers, envs[i])
                while added < e:
                    ee = envs[part[added]]
                    agg.step(*[a(ee) for a in self.args])
                    added += 1
                results[i] = agg.final()
            return
        for pos, i in enumerate(part):
            s, e = self._frame_bounds(pos, m, peers, envs[i])
            agg = make_aggregate(name, func.star, False)
            for j in range(s, e):
                ee = envs[part[j]]
                agg.step(*[a(ee) for a in self.args])
            results[i] = agg.final()


# ---------------------------------------------------------------- compound queries and CTEs


class CompoundPlan(Plan):
    def __init__(self, parts: list[Plan], ops: list[str]) -> None:
        self.parts = parts
        self.ops = ops
        self.columns = parts[0].columns
        self.affinities = parts[0].affinities
        self.collations = parts[0].collations
        self.correlated = any(p.correlated for p in parts)
        self.order: list[OrderKey] = []
        self.limit: Callable[[Env], Value] | None = None
        self.offset: Callable[[Env], Value] | None = None

    def combined(self, env: Env) -> Iterator[Row]:
        def key(r: Row) -> tuple:
            return tuple(sort_key(collate_key(v, c)) for v, c in zip(r, self.collations))

        result: Iterator[Row] = self.parts[0].run(env)
        for op, part in zip(self.ops, self.parts[1:]):
            if op == "UNION ALL":
                result = itertools.chain(result, part.run(env))
            elif op == "UNION":
                result = _dedupe_by(itertools.chain(result, part.run(env)), key)
            elif op == "INTERSECT":
                right = {key(r) for r in part.run(env)}
                result = _dedupe_by((r for r in result if key(r) in right), key)
            else:  # EXCEPT
                right = {key(r) for r in part.run(env)}
                result = _dedupe_by((r for r in result if key(r) not in right), key)
            if op != "UNION ALL" and self.ops[0] == "UNION":
                pass
        return result

    def run(self, env: Env) -> Iterator[Row]:
        rows = self.combined(env)
        limit, offset = CorePlan._limits(self, env)  # type: ignore[arg-type]
        if self.order:
            sorter = make_sort_key(self.order)
            rows = iter(sorted(rows, key=lambda r: sorter(tuple(r[k.column] for k in self.order))))
        if offset:
            rows = itertools.islice(rows, offset, None)
        if limit is not None:
            rows = itertools.islice(rows, limit)
        yield from rows

    def explain(self, depth: int, out: list[tuple[int, str]]) -> None:
        out.append((depth, "COMPOUND QUERY"))
        out.append((depth + 1, "LEFT-MOST SUBQUERY"))
        self.parts[0].explain(depth + 2, out)
        for op, part in zip(self.ops, self.parts[1:]):
            out.append((depth + 1, f"{op}"))
            part.explain(depth + 2, out)
        if self.order:
            out.append((depth, "USE TEMP B-TREE FOR ORDER BY"))


def _dedupe_by(rows: Iterator[Row], key: Callable[[Row], tuple]) -> Iterator[Row]:
    seen = set()
    for r in rows:
        k = key(r)
        if k not in seen:
            seen.add(k)
            yield r


class Working:
    """The rows a recursive CTE's step reads in the current iteration."""

    def __init__(self) -> None:
        self.current: list[Row] = []


class RecursivePlan(Plan):
    def __init__(self, name: str, init: Plan, steps: list[Plan], union_all: bool, working: Working) -> None:
        self.name = name
        self.init = init
        self.steps = steps
        self.union_all = union_all
        self.working = working
        self.columns = init.columns
        self.affinities = init.affinities
        self.collations = init.collations
        self.correlated = init.correlated or any(s.correlated for s in steps)
        self.max_rows = 10_000_000

    def run(self, env: Env) -> Iterator[Row]:
        seen: set | None = None if self.union_all else set()
        queue: list[Row] = []

        def admit(r: Row) -> bool:
            if seen is None:
                return True
            k = tuple(sort_key(v) for v in r)
            if k in seen:
                return False
            seen.add(k)
            return True

        for r in self.init.run(env):
            if admit(r):
                queue.append(r)
        produced = 0
        head = 0
        while head < len(queue):
            row = queue[head]
            head += 1
            produced += 1
            if produced > self.max_rows:
                raise SQLError(f"recursive query {self.name} produced more than {self.max_rows:,} rows; add a stop condition")
            yield row
            saved = self.working.current
            self.working.current = [row]
            try:
                for step in self.steps:
                    for r in step.run(env):
                        if admit(r):
                            queue.append(r)
            finally:
                self.working.current = saved
            if head > 4096 and head * 2 > len(queue):
                del queue[:head]
                head = 0

    def explain(self, depth: int, out: list[tuple[int, str]]) -> None:
        out.append((depth, f"RECURSIVE CTE {self.name}"))
        out.append((depth + 1, "SETUP"))
        self.init.explain(depth + 2, out)
        out.append((depth + 1, "RECURSIVE STEP"))
        for s in self.steps:
            s.explain(depth + 2, out)


class MaterializedPlan(Plan):
    """A non-recursive CTE: run once per statement and reused."""

    def __init__(self, name: str, inner: Plan, columns: list[str] | None) -> None:
        self.name = name
        self.inner = inner
        self.columns = columns or inner.columns
        self.affinities = inner.affinities
        self.collations = inner.collations
        self.correlated = inner.correlated

    def run(self, env: Env) -> Iterator[Row]:
        if self.correlated:
            yield from self.inner.run(env)
            return
        cache = env.ctx.cache
        key = ("cte", id(self))
        if key not in cache:
            cache[key] = list(self.inner.run(env))
        yield from cache[key]

    def explain(self, depth: int, out: list[tuple[int, str]]) -> None:
        out.append((depth, f"MATERIALIZE {self.name}"))
        self.inner.explain(depth + 1, out)


class ValuesPlan(Plan):
    def __init__(self, rows: list[list[Callable[[Env], Value]]], width: int) -> None:
        self.rows = rows
        self.columns = [f"column{i + 1}" for i in range(width)]
        self.affinities = [None] * width
        self.collations = [None] * width

    def run(self, env: Env) -> Iterator[Row]:
        e = Env((), env, env.ctx)
        for fns in self.rows:
            yield tuple(f(e) for f in fns)

    def explain(self, depth: int, out: list[tuple[int, str]]) -> None:
        out.append((depth, f"SCAN {len(self.rows)} CONSTANT ROW{'S' if len(self.rows) != 1 else ''}"))


@dataclass
class CTEDef:
    name: str
    columns: list[str] | None
    select: A.Select
    recursive: bool
    plan: Plan | None = None
    working: Working | None = None
    compiling: bool = False


# ---------------------------------------------------------------- the compiler


class QueryCompiler:
    def __init__(self, catalog: Any, pager: Any, sql: str) -> None:
        self.catalog = catalog
        self.pager = pager
        self.sql = sql
        self.view_depth = 0

    def error(self, message: str, node: A.Node | None = None, hint: str = "") -> SQLError:
        return SQLError(message, self.sql, node.pos if node is not None else -1, hint)

    # ------------------------------------------------------------ SELECT

    def compile_select(self, sel: A.Select, parent: Scope | None) -> Plan:
        ctes: dict[str, CTEDef] = {}
        for cte in sel.ctes:
            key = cte.name.lower()
            if key in ctes:
                raise self.error(f"duplicate WITH table name: {cte.name}", cte)
            ctes[key] = CTEDef(cte.name, cte.columns, cte.select, sel.recursive)
        holder = Scope([], parent, ctes, transparent=True) if ctes else parent
        if len(sel.cores) == 1:
            plan = self.compile_core(sel.cores[0], holder, sel, ctes_scope=holder)
            return plan
        parts = []
        for core in sel.cores:
            parts.append(self.compile_core(core, holder, None, ctes_scope=holder))
        width = len(parts[0].columns)
        for p, core in zip(parts[1:], sel.cores[1:]):
            if len(p.columns) != width:
                raise self.error("SELECTs to the left and right of a compound operator do not have the same number of result columns", core)
        comp = CompoundPlan(parts, sel.ops)
        if sel.order_by:
            comp.order = [self._compound_order(item, parts, sel.cores) for item in sel.order_by]
        lim_scope = Scope([], holder)
        ec = ExprCompiler(self.sql, self)
        if sel.limit is not None:
            comp.limit = ec.fn(sel.limit, lim_scope)
        if sel.offset is not None:
            comp.offset = ec.fn(sel.offset, lim_scope)
        comp.correlated = comp.correlated or lim_scope.correlated
        return comp

    def _compound_order(self, item: A.OrderItem, parts: list[Plan], cores: list[Any]) -> OrderKey:
        e = item.expr
        coll = None
        if isinstance(e, A.Collate):
            coll = e.collation.upper()
            e = e.expr
        idx = None
        if isinstance(e, A.Literal) and isinstance(e.value, int):
            idx = e.value - 1
            if not 0 <= idx < len(parts[0].columns):
                raise self.error(f"ORDER BY term out of range - should be between 1 and {len(parts[0].columns)}", item)
        else:
            for core in cores:
                if isinstance(core, A.SelectCore):
                    for k, rc in enumerate(core.columns):
                        if rc.alias and isinstance(e, A.Column) and e.table is None and rc.alias.lower() == e.name.lower():
                            idx = k
                            break
                        if rc.expr is not None and rc.expr.text.lower() == e.text.lower():
                            idx = k
                            break
                if idx is not None:
                    break
            if idx is None and isinstance(e, A.Column):
                for k, name in enumerate(parts[0].columns):
                    if name.lower() == e.name.lower():
                        idx = k
                        break
        if idx is None:
            raise self.error("ORDER BY term does not match any column in the result set", item)
        return OrderKey(None, idx, item.descending, item.nulls_first, coll or parts[0].collations[idx])

    def compile_core(
        self, core: A.SelectCore | A.Values, parent: Scope | None, sel: A.Select | None, ctes_scope: Scope | None = None
    ) -> Plan:
        if isinstance(core, A.Values):
            ec = ExprCompiler(self.sql, self)
            vscope = Scope([], parent)
            rows = [[ec.fn(e, vscope) for e in row] for row in core.rows]
            vp = ValuesPlan(rows, len(core.rows[0]))
            vp.correlated = vscope.correlated
            if sel is not None and (sel.order_by or sel.limit is not None):
                # treat as SELECT * FROM (VALUES ...)
                wrapper = A.SelectCore([A.ResultColumn(None, star=True)], A.SubqueryRef(A.Select([core]), None))
                return self.compile_core(wrapper, parent, sel)
            return vp
        plan = CorePlan()
        scope = Scope([], parent)
        ec = ExprCompiler(self.sql, self)
        conjuncts_on: list[tuple[int, A.Expr]] = []
        # ---- FROM
        items = self._flatten(core.from_) if core.from_ is not None else []
        if core.from_ is None:
            plan.no_from = True
        sources: list[Source] = []
        level_info: list[tuple[str, Any, Any]] = []  # (join kind, table/plan, item)
        offset = 0
        for join_kind, item, on, using, natural in items:
            src, table, subplan, working = self._source(item, scope, parent)
            src.offset = offset
            offset += src.width
            if any(s.name.lower() == src.name.lower() for s in sources):
                raise self.error(f"ambiguous table name {src.name}: give one of them an alias", item)
            if join_kind == "LEFT":
                src.nullable = True
            # USING / NATURAL turn into equality conditions
            if natural or using:
                names = using or [
                    c
                    for k, c in enumerate(src.columns)
                    if k not in src.hidden
                    and any(c.lower() == pc.lower() for s in sources for kk, pc in enumerate(s.columns) if kk not in s.hidden)
                ]
                for name in names:
                    left_src = next((s for s in sources if any(c.lower() == name.lower() for c in s.columns)), None)
                    if left_src is None or name.lower() not in [c.lower() for c in src.columns]:
                        raise self.error(f"cannot join using column {name} - column not present in both tables", item)
                    cond = A.Binary("=", A.Column(left_src.name, name), A.Column(src.name, name))
                    cond.pos = item.pos
                    conjuncts_on.append((len(sources), cond))
                    src.using_hidden.add(name.lower())
            if on is not None:
                for c in _split_and(on):
                    conjuncts_on.append((len(sources), c))
            sources.append(src)
            level_info.append((join_kind, table, subplan, working))
        scope.sources = sources
        plan.width = offset
        # SQLite resolves result aliases in WHERE / GROUP BY / HAVING when no column matches
        for rc in core.columns:
            if rc.alias and rc.expr is not None and not find_windows(rc.expr):
                scope.aliases.setdefault(rc.alias.lower(), rc.expr)
        # ---- WHERE placement
        where = _split_and(core.where) if core.where is not None else []
        for w in where:
            if find_aggregates(w):
                raise self.error(f"misuse of aggregate function {find_aggregates(w)[0].name}()", w)
            if find_windows(w):
                raise self.error("misuse of window function in WHERE", w)
        pending_where = list(where)
        levels: list[Level] = []
        for i, (src, (join_kind, table, subplan, working)) in enumerate(zip(sources, level_info)):
            on_here = [c for lvl_i, c in conjuncts_on if lvl_i == i]
            if join_kind == "LEFT":
                # ON conditions decide the match; WHERE conditions run after null-extension
                lookup_pool = on_here
            else:
                pending_where.extend(on_here)
                lookup_pool = [c for c in pending_where]
            usable = [c for c in lookup_pool if self._refs(c, scope) is not None and max(self._refs(c, scope) or {-1}) == i]
            lookup = self._choose_lookup(i, src, table, subplan, working, usable, scope, ec, join_kind, sources)
            lvl = Level(src, join_kind if i else "FIRST", lookup, table, subplan, working=working)
            if join_kind == "LEFT":
                lvl.on_filters = [ec.condition(c, scope) for c in on_here]
                # WHERE conditions on earlier levels only can still run here, after the null-extension
                ready = [c for c in pending_where if self._ready(c, scope, i)]
            else:
                ready = [c for c in pending_where if self._ready(c, scope, i)]
            for c in ready:
                pending_where.remove(c)
            lvl.filters = [ec.condition(c, scope) for c in ready]
            levels.append(lvl)
        plan.levels = levels
        plan.fetchers = [Fetcher(lvl, self.pager) for lvl in levels]
        plan.final_filters = [ec.condition(c, scope) for c in pending_where]
        # ---- result columns
        result: list[tuple[str, A.Expr | None, int | None, Source | None]] = []  # name, expr, star column index
        for rc in core.columns:
            if rc.star:
                matched = False
                for src in sources:
                    if rc.star_table is not None and src.name.lower() != rc.star_table.lower():
                        continue
                    matched = True
                    for k, col in enumerate(src.columns):
                        if k in src.hidden:
                            continue
                        if rc.star_table is None and col.lower() in src.using_hidden:
                            continue
                        result.append((col, None, src.offset + k, src))
                if rc.star_table is not None and not matched:
                    raise self.error(f"no such table: {rc.star_table}", rc)
                if not sources and rc.star_table is None:
                    raise self.error("no tables specified", rc, "SELECT * needs a FROM clause")
            else:
                assert rc.expr is not None
                name = rc.alias or (rc.expr.name if isinstance(rc.expr, A.Column) else rc.expr.text)
                result.append((name, rc.expr, None, None))
        # ---- aggregates and grouping
        order_items = sel.order_by if sel is not None else []
        agg_nodes = []
        for _name, e, _k, _s in result:
            agg_nodes += find_aggregates(e)
        if core.having is not None:
            agg_nodes += find_aggregates(core.having)
        for item in order_items:
            agg_nodes += find_aggregates(item.expr)
        plan.grouped = bool(core.group_by or agg_nodes)
        if core.having is not None and not plan.grouped:
            raise self.error("a GROUP BY clause is required before HAVING", core.having)
        if plan.grouped:
            for g in core.group_by:
                if find_aggregates(g):
                    raise self.error("aggregate functions are not allowed in the GROUP BY clause", g)
                if isinstance(g, A.Literal) and isinstance(g.value, int):
                    k = g.value - 1
                    if not 0 <= k < len(result):
                        raise self.error(f"GROUP BY term out of range - should be between 1 and {len(result)}", g)
                    gexpr = result[k][1]
                    if gexpr is None:
                        comp = Compiled(_getter(result[k][2]))  # type: ignore[arg-type]
                    else:
                        comp = ec.compile(gexpr, scope)
                else:
                    comp = ec.compile(g, scope)
                plan.group_fns.append(comp.fn)
                plan.group_colls.append(comp.collation)
            slots: dict[int, int] = {}
            unique_aggs: list[A.Func] = []
            for f in agg_nodes:
                if id(f) in slots:
                    continue
                for a in f.args:
                    if find_aggregates(a):
                        raise self.error(f"misuse of aggregate function {find_aggregates(a)[0].name}()", a, "aggregates cannot be nested")
                slots[id(f)] = plan.width + len(unique_aggs)
                unique_aggs.append(f)
                used = f.args[:1] if f.name in ("count", "sum", "avg", "total", "min", "max") else f.args
                compiled = [ec.compile(a, scope) for a in used]
                args = [c.fn for c in compiled]
                flt = ec.condition(f.filter, scope) if f.filter is not None else None
                plan.aggs.append((f, args, flt))
                plan.agg_colls.append(compiled[0].collation if compiled else None)
            plan.minmax_rep = len(unique_aggs) == 1 and unique_aggs[0].name in ("min", "max")
            ec.agg_slots = slots
            if core.having is not None:
                plan.having = ec.condition(core.having, scope)
            row_width = plan.width + len(unique_aggs)
            plan.notes.append("USE TEMP B-TREE FOR GROUP BY" if core.group_by else "AGGREGATE")
        else:
            row_width = plan.width
        # ---- window functions
        win_nodes = []
        for _name, e, _k, _s in result:
            win_nodes += find_windows(e)
        for item in order_items:
            win_nodes += find_windows(item.expr)
        for f in win_nodes:
            if id(f) in ec.win_slots:
                continue
            spec = self._window_spec(f, core)
            args = [ec.fn(a, scope) for a in f.args]
            partition = [ec.fn(p, scope) for p in spec.partition_by]
            order = [self._order_key(o, ec, scope) for o in spec.order_by]
            frame_fns: tuple = (None, None)
            if spec.frame is not None:
                lo, hi = spec.frame[1], spec.frame[2]
                frame_fns = (
                    ec.fn(lo[1], scope) if isinstance(lo[1], A.Node) else None,
                    ec.fn(hi[1], scope) if isinstance(hi[1], A.Node) else None,
                )
            if f.filter is not None:
                raise NotSupported("FILTER on window functions is not supported yet", self.sql, f.pos)
            plan.windows.append(WindowPlan(f, args, partition, order, spec.frame, frame_fns))
            ec.win_slots[id(f)] = row_width
            row_width += 1
        if plan.windows:
            plan.notes.append(f"WINDOW ({len(plan.windows)} function{'s' if len(plan.windows) > 1 else ''})")
        # ---- outputs
        names, affs, colls = [], [], []
        for name, e, k, src in result:
            names.append(name)
            if e is None:
                assert k is not None and src is not None
                plan.outputs.append(_getter(k))
                affs.append(src.affinities[k - src.offset])
                colls.append(src.collations[k - src.offset])
            else:
                comp = ec.compile(e, scope)
                plan.outputs.append(comp.fn)
                affs.append(comp.affinity if isinstance(e, (A.Column, A.Cast)) or (isinstance(e, A.Subquery)) else None)
                colls.append(comp.collation)
        plan.columns, plan.affinities, plan.collations = names, affs, colls
        plan.distinct = core.distinct
        if core.distinct:
            plan.notes.append("USE TEMP B-TREE FOR DISTINCT")
        # ---- ORDER BY / LIMIT
        if sel is not None:
            for item in sel.order_by:
                plan.order.append(self._result_order(item, result, names, colls, ec, scope))
            if sel.order_by:
                plan.notes.append("USE TEMP B-TREE FOR ORDER BY")
            lim_scope = Scope([], parent)
            if sel.limit is not None:
                plan.limit = ec.fn(sel.limit, lim_scope)
            if sel.offset is not None:
                plan.offset = ec.fn(sel.offset, lim_scope)
        plan.correlated = scope.correlated or any(lvl.plan is not None and lvl.plan.correlated for lvl in levels)
        return plan

    # ------------------------------------------------------------ helpers

    def _window_spec(self, f: A.Func, core: A.SelectCore) -> A.WindowSpec:
        over = f.over
        if isinstance(over, str):
            spec = core.windows.get(over)
            if spec is None:
                raise self.error(f"no such window: {over}", f)
            return spec
        assert isinstance(over, A.WindowSpec)
        if over.base:
            base = core.windows.get(over.base)
            if base is None:
                raise self.error(f"no such window: {over.base}", f)
            return A.WindowSpec(base.partition_by, over.order_by or base.order_by, over.frame or base.frame)
        return over

    def _order_key(self, item: A.OrderItem, ec: ExprCompiler, scope: Scope) -> OrderKey:
        comp = ec.compile(item.expr, scope)
        return OrderKey(comp.fn, -1, item.descending, item.nulls_first, comp.collation)

    def _result_order(
        self, item: A.OrderItem, result: list, names: list[str], colls: list[str | None], ec: ExprCompiler, scope: Scope
    ) -> OrderKey:
        e = item.expr
        coll = None
        explicit = False
        if isinstance(e, A.Collate):
            coll = e.collation.upper()
            explicit = True
            e = e.expr
        if isinstance(e, A.Literal) and isinstance(e.value, int):
            k = e.value - 1
            if not 0 <= k < len(names):
                raise self.error(f"ORDER BY term out of range - should be between 1 and {len(names)}", item)
            return OrderKey(None, k, item.descending, item.nulls_first, coll if explicit else colls[k])
        if isinstance(e, A.Column) and e.table is None:
            for k, name in enumerate(names):
                if name.lower() == e.name.lower():
                    return OrderKey(None, k, item.descending, item.nulls_first, coll if explicit else colls[k])
        comp = ec.compile(item.expr, scope)
        return OrderKey(comp.fn, -1, item.descending, item.nulls_first, comp.collation)

    def _flatten(self, item: A.FromItem) -> list[tuple[str, A.FromItem, A.Expr | None, list[str] | None, bool]]:
        if isinstance(item, A.Join):
            left = self._flatten(item.left)
            if isinstance(item.right, A.Join):
                raise NotSupported(
                    "parenthesised joins on the right side are not supported yet",
                    self.sql,
                    item.right.pos,
                    "rewrite the joins left to right",
                )
            kind = "INNER" if item.kind == "CROSS" else item.kind
            return left + [(kind, item.right, item.on, item.using, item.natural)]
        return [("FIRST", item, None, None, False)]

    def _source(self, item: A.FromItem, scope: Scope, parent: Scope | None) -> tuple[Source, TableInfo | None, Plan | None, Working | None]:
        if isinstance(item, A.SubqueryRef):
            sub = self.compile_select(item.select, parent)
            alias = item.alias or f"(subquery-{item.pos})"
            src = Source(alias, list(sub.columns), list(sub.affinities), list(sub.collations))
            return src, None, sub, None
        assert isinstance(item, A.TableRef)
        name = item.name
        lname = name.lower()
        cte = scope.find_cte(name)
        if cte is not None:
            plan, working = self._cte_plan(cte, item, parent)
            cols = list(cte.columns) if cte.columns else list(plan.columns)
            if len(cols) != len(plan.columns):
                raise self.error(f"table {cte.name} has {len(plan.columns)} values for {len(cols)} columns", item)
            src = Source(item.alias or cte.name, cols, list(plan.affinities), list(plan.collations))
            return src, None, plan, working
        if lname in ("quarry_schema", "sqlite_schema", "sqlite_master", "quarry_master"):
            vp = _SchemaPlan(self.catalog)
            src = Source(item.alias or name, list(vp.columns), [None] * 5, [None] * 5)
            return src, None, vp, None
        table = self.catalog.table(name)
        if table is not None:
            cols = [c.name for c in table.columns]
            hidden = set()
            rowid_name = next((r for r in ROWID_NAMES if r not in [c.lower() for c in cols]), "rowid")
            cols.append(rowid_name)
            hidden.add(len(cols) - 1)
            affs = [c.affinity for c in table.columns] + [INTEGER]
            colls = [c.collation for c in table.columns] + [None]
            src = Source(item.alias or table.name, cols, affs, colls, table=table, rowid_col=len(cols) - 1, hidden=hidden)
            return src, table, None, None
        view = self.catalog.views.get(lname)
        if view is not None:
            if self.view_depth > 32:
                raise self.error(f"view {view.name} refers to itself", item)
            self.view_depth += 1
            try:
                saved_sql = self.sql
                self.sql = view.sql
                vplan = self.compile_select(view.select, None)
            finally:
                self.sql = saved_sql
                self.view_depth -= 1
            cols = list(view.columns) if view.columns else list(vplan.columns)
            src = Source(item.alias or view.name, cols, list(vplan.affinities), list(vplan.collations))
            return src, None, vplan, None
        from .expr import closest

        known = list(self.catalog.tables) + list(self.catalog.views)
        guess = closest(name, known)
        raise self.error(f"no such table: {name}", item, f"did you mean {guess}?" if guess else "")

    def _cte_plan(self, cte: CTEDef, item: A.Node, parent: Scope | None) -> tuple[Plan, Working | None]:
        if cte.compiling:
            if cte.working is None:
                raise self.error(f"circular reference: {cte.name}", item)
            # a reference from inside the recursive step
            assert cte.plan is not None
            return cte.plan, cte.working
        if cte.plan is not None and cte.working is None:
            return cte.plan, None
        sel = cte.select
        self_ref = cte.recursive and self._references(sel, cte.name)
        if not self_ref:
            cte.compiling = True
            try:
                inner = self.compile_select(sel, _scope_for_cte(parent, cte))
            finally:
                cte.compiling = False
            cte.plan = MaterializedPlan(cte.name, inner, cte.columns)
            return cte.plan, None
        # recursive: initial cores do not reference the CTE; the rest are the step
        if sel.order_by or sel.limit is not None:
            raise NotSupported("ORDER BY and LIMIT on a recursive CTE are not supported yet", self.sql, sel.pos)
        split = next((k for k, c in enumerate(sel.cores) if self._references(c, cte.name)), None)
        if split is None or split == 0:
            raise self.error(f"recursive CTE {cte.name} needs a starting SELECT before UNION", sel)
        ops = sel.ops
        if any(op not in ("UNION", "UNION ALL") for op in ops[split - 1 :]):
            raise self.error("a recursive CTE must use UNION or UNION ALL", sel)
        init_sel = A.Select(sel.cores[:split], ops[: split - 1])
        init = self.compile_select(init_sel, _scope_for_cte(parent, cte))
        working = Working()
        cte.working = working
        placeholder = _WorkingPlan(init.columns if not cte.columns else cte.columns, init.affinities, init.collations, working)
        cte.plan = placeholder
        cte.compiling = True
        try:
            steps = [self.compile_core(c, _scope_for_cte(parent, cte), None) for c in sel.cores[split:]]
        finally:
            cte.compiling = False
        for s in steps:
            if len(s.columns) != len(init.columns):
                raise self.error("SELECTs to the left and right of UNION do not have the same number of result columns", sel)
        rec = RecursivePlan(cte.name, init, steps, ops[split - 1] == "UNION ALL", working)
        if cte.columns:
            rec.columns = list(cte.columns)
        cte.plan = rec
        cte.working = None
        return rec, None

    def _references(self, node: Any, name: str) -> bool:
        lname = name.lower()
        for n in walk(node, into_subqueries=True):
            if isinstance(n, A.TableRef) and n.name.lower() == lname:
                return True
            if isinstance(n, (A.SelectCore, A.Select)):
                pass
        # walk() does not descend into FROM items of cores: check them directly
        return _from_references(node, lname)

    def _refs(self, expr: A.Expr, scope: Scope) -> set[int] | None:
        """Indexes of the sources an expression reads at this query level."""
        refs: set[int] = set()
        for n in walk(expr):
            if isinstance(n, (A.Subquery, A.Exists, A.InSelect)):
                return set(range(len(scope.sources)))  # conservative: run after every join
            if isinstance(n, A.Column):
                hits = scope.lookup(n.table, n.name)
                if hits:
                    src = hits[0][0]
                    refs.add(scope.sources.index(src))
                elif n.table is None and n.name.lower() in scope.aliases:
                    return set(range(len(scope.sources)))
        return refs

    def _ready(self, expr: A.Expr, scope: Scope, level: int) -> bool:
        refs = self._refs(expr, scope)
        return refs is not None and all(r <= level for r in refs)

    def _choose_lookup(
        self,
        i: int,
        src: Source,
        table: TableInfo | None,
        subplan: Plan | None,
        working: Working | None,
        usable: list[A.Expr],
        scope: Scope,
        ec: ExprCompiler,
        join_kind: str,
        sources: list[Source],
    ) -> Lookup:
        if working is not None:
            return Lookup("working")
        candidates = []  # (score, Lookup)
        if table is not None:
            for c in usable:
                info = self._analyze(c, i, src, scope, ec)
                if info is None:
                    continue
                col, op, other, other_comp, items = info
                is_rowid = col == src.rowid_col or (table.rowid_alias is not None and col == table.rowid_alias)
                if is_rowid:
                    if op == "=":
                        candidates.append((100, Lookup("rowid_eq", "rowid=?", eq=[other_comp.fn])))
                    elif op == "IN":
                        candidates.append((90, Lookup("rowid_in", f"rowid IN ({len(items)} values)", in_values=[x.fn for x in items])))
                    elif op in ("<", "<=", ">", ">="):
                        lk = Lookup("rowid_range", f"rowid{op}?")
                        if op in (">", ">="):
                            lk.lo, lk.lo_incl = other_comp.fn, op == ">="
                        else:
                            lk.hi, lk.hi_incl = other_comp.fn, op == "<="
                        candidates.append((40, lk))
                    elif op == "BETWEEN":
                        lo_c, hi_c = items
                        candidates.append((45, Lookup("rowid_range", "rowid>? AND rowid<?", lo=lo_c.fn, hi=hi_c.fn)))
            for ix in table.indexes:
                lk = self._index_lookup(ix, usable, i, src, scope, ec)
                if lk is not None:
                    candidates.append(lk)
        if candidates:
            candidates.sort(key=lambda p: -p[0])
            return candidates[0][1]
        # no index: an equality join with an earlier table can use a hash table
        if i > 0 and (table is not None or subplan is not None):
            for c in usable:
                if not isinstance(c, A.Binary) or c.op != "=":
                    continue
                lr = self._refs(c.left, scope)
                rr = self._refs(c.right, scope)
                if lr is None or rr is None:
                    continue
                if lr == {i} and rr and max(rr) < i:
                    inner_e, outer_e = c.left, c.right
                elif rr == {i} and lr and max(lr) < i:
                    inner_e, outer_e = c.right, c.left
                else:
                    continue
                inner_c = ec.compile(inner_e, scope)
                outer_c = ec.compile(outer_e, scope)
                ai, ao = comparison_affinity(inner_c.affinity, outer_c.affinity)
                coll = ec._collation(inner_c, outer_c)
                lk = Lookup(
                    "hash", f"{inner_e.text}={outer_e.text}", hash_inner=inner_c.fn, hash_outer=outer_c.fn, hash_conv=(ai, ao, coll)
                )
                lk.build_filters = [ec.condition(f, scope) for f in usable if self._refs(f, scope) == {i}]
                return lk
        if subplan is not None:
            return Lookup("plan")
        return Lookup("scan")

    def _analyze(self, c: A.Expr, i: int, src: Source, scope: Scope, ec: ExprCompiler) -> tuple | None:
        """Recognise ``column op expr`` where the column belongs to source ``i`` and expr does not."""

        def col_of(e: A.Expr) -> int | None:
            if isinstance(e, A.Column):
                hits = scope.lookup(e.table, e.name)
                if hits and hits[0][0] is src:
                    return hits[0][1]
            return None

        def independent(e: A.Expr) -> bool:
            refs = self._refs(e, scope)
            return refs is not None and all(r < i for r in refs)

        if isinstance(c, A.Binary) and c.op in ("=", "<", "<=", ">", ">="):
            for col_e, other, flip in ((c.left, c.right, False), (c.right, c.left, True)):
                k = col_of(col_e)
                if k is not None and independent(other):
                    col_c = ec.compile(col_e, scope)
                    other_c = ec.compile(other, scope)
                    if not self._index_safe(col_c, other_c):
                        return None
                    op = c.op
                    if flip:
                        op = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}.get(op, op)
                    return k, op, other, self._probe(col_c, other_c), None
            return None
        if isinstance(c, A.InList) and not c.negated and c.items:
            k = col_of(c.expr)
            if k is not None and all(independent(x) for x in c.items):
                col_c = ec.compile(c.expr, scope)
                items = [ec.compile(x, scope) for x in c.items]
                if not all(self._index_safe(col_c, x) for x in items):
                    return None
                return k, "IN", None, None, [self._probe(col_c, x) for x in items]
            return None
        if isinstance(c, A.Between) and not c.negated:
            k = col_of(c.expr)
            if k is not None and independent(c.low) and independent(c.high):
                col_c = ec.compile(c.expr, scope)
                lo_c, hi_c = ec.compile(c.low, scope), ec.compile(c.high, scope)
                if not (self._index_safe(col_c, lo_c) and self._index_safe(col_c, hi_c)):
                    return None
                return k, "BETWEEN", None, None, [self._probe(col_c, lo_c), self._probe(col_c, hi_c)]
        return None

    @staticmethod
    def _index_safe(col_c: Compiled, other_c: Compiled) -> bool:
        """An index on the column can serve this comparison: no conversion of the column, binary collation."""
        al, _ar = comparison_affinity(col_c.affinity, other_c.affinity)
        if al is not None:
            return False
        coll = (
            col_c.collation
            if col_c.explicit_collation
            else (other_c.collation if other_c.explicit_collation else col_c.collation or other_c.collation)
        )
        return coll in (None, "BINARY")

    @staticmethod
    def _probe(col_c: Compiled, other_c: Compiled) -> Compiled:
        """The probe expression converted the way the comparison converts it."""
        _al, ar = comparison_affinity(col_c.affinity, other_c.affinity)
        if ar is None:
            return other_c
        f = other_c.fn
        return Compiled(lambda env: (lambda v: v if v is None else apply_affinity(v, ar))(f(env)), constant=other_c.constant)

    def _index_lookup(
        self, ix: Any, usable: list[A.Expr], i: int, src: Source, scope: Scope, ec: ExprCompiler
    ) -> tuple[int, Lookup] | None:
        infos = [self._analyze(c, i, src, scope, ec) for c in usable]
        infos = [x for x in infos if x is not None]
        eq_fns = []
        parts = []
        for col in ix.col_idx:
            eq = next((x for x in infos if x[0] == col and x[1] == "="), None)
            if eq is None:
                break
            eq_fns.append(eq[3].fn)
            parts.append(f"{src.columns[col]}=?")
        n_eq = len(eq_fns)
        next_col = ix.col_idx[n_eq] if n_eq < len(ix.col_idx) else None
        lo = hi = None
        lo_incl = hi_incl = True
        if next_col is not None:
            for x in infos:
                if x[0] != next_col:
                    continue
                if x[1] in (">", ">=") and lo is None:
                    lo, lo_incl = x[3].fn, x[1] == ">="
                    parts.append(f"{src.columns[next_col]}{x[1]}?")
                elif x[1] in ("<", "<=") and hi is None:
                    hi, hi_incl = x[3].fn, x[1] == "<="
                    parts.append(f"{src.columns[next_col]}{x[1]}?")
                elif x[1] == "BETWEEN" and lo is None and hi is None:
                    lo, hi = x[4][0].fn, x[4][1].fn
                    parts.append(f"{src.columns[next_col]}>? AND {src.columns[next_col]}<?")
        if n_eq == 0 and lo is None and hi is None:
            first = ix.col_idx[0]
            inx = next((x for x in infos if x[0] == first and x[1] == "IN"), None)
            if inx is None:
                return None
            lk = Lookup(
                "index_in", f"{src.columns[first]} IN ({len(inx[4])} values)", index=ix, in_values=[x.fn for x in inx[4]], eq_aff=[None]
            )
            return 60, lk
        if n_eq == 0:
            lk = Lookup("index_range", " AND ".join(parts), index=ix, lo=lo, lo_incl=lo_incl, hi=hi, hi_incl=hi_incl)
            return 30, lk
        full = n_eq == len(ix.col_idx)
        score = 50 + n_eq * 5 + (30 if full and ix.unique else 0) + (3 if lo is not None or hi is not None else 0)
        lk = Lookup(
            "index_eq", " AND ".join(parts), index=ix, eq=eq_fns, eq_aff=[None] * n_eq, lo=lo, lo_incl=lo_incl, hi=hi, hi_incl=hi_incl
        )
        return score, lk


def rc_alias_match(result_entry: tuple, name: str) -> bool:
    rname, rexpr, _k, _s = result_entry
    return rname.lower() == name.lower()


def _getter(k: int) -> Callable[[Env], Value]:
    def get(env: Env) -> Value:
        return env.row[k]

    return get


def _split_and(e: A.Expr | None) -> list[A.Expr]:
    if e is None:
        return []
    if isinstance(e, A.Binary) and e.op == "AND":
        return _split_and(e.left) + _split_and(e.right)
    return [e]


def _scope_for_cte(parent: Scope | None, cte: CTEDef) -> Scope | None:
    return parent


def _from_references(node: Any, lname: str) -> bool:
    if isinstance(node, A.Select):
        return any(_from_references(c, lname) for c in node.cores) or any(_from_references(c.select, lname) for c in node.ctes)
    if isinstance(node, A.SelectCore):
        if node.from_ is not None and _from_references(node.from_, lname):
            return True
        for n in walk([rc.expr for rc in node.columns] + [node.where, node.having] + list(node.group_by), into_subqueries=True):
            if isinstance(n, (A.Subquery, A.Exists)) and _from_references(n.select, lname):
                return True
            if isinstance(n, A.InSelect) and _from_references(n.select, lname):
                return True
        return False
    if isinstance(node, A.Join):
        return _from_references(node.left, lname) or _from_references(node.right, lname)
    if isinstance(node, A.TableRef):
        return node.name.lower() == lname
    if isinstance(node, A.SubqueryRef):
        return _from_references(node.select, lname)
    return False


class _WorkingPlan(Plan):
    def __init__(self, columns: list[str], affinities: list, collations: list, working: Working) -> None:
        self.columns = list(columns)
        self.affinities = list(affinities)
        self.collations = list(collations)
        self.working = working

    def run(self, env: Env) -> Iterator[Row]:
        yield from self.working.current

    def explain(self, depth: int, out: list[tuple[int, str]]) -> None:
        out.append((depth, "SCAN recursive step"))


class _SchemaPlan(Plan):
    columns = ["type", "name", "tbl_name", "rootpage", "sql"]
    affinities = [None] * 5
    collations = [None] * 5

    def __init__(self, catalog: Any) -> None:
        self.catalog = catalog

    def run(self, env: Env) -> Iterator[Row]:
        yield from self.catalog.schema_rows()

    def explain(self, depth: int, out: list[tuple[int, str]]) -> None:
        out.append((depth, "SCAN quarry_schema"))


__all__ = ["QueryCompiler", "Plan", "CorePlan", "explain_lines", "table_row_reader", "Context", "Env", "is_true"]
