"""Name resolution and compilation of SQL expressions into Python closures.

A compiled expression is a function ``fn(env) -> value``. ``env.row`` is the current row: one flat tuple with
the columns of every source in the FROM clause side by side. ``env.outer`` is the row of the enclosing query,
which correlated subqueries read.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..errors import SQLError
from ..sql import ast as A
from ..values import (
    NUMERIC,
    TEXT,
    Value,
    add,
    apply_affinity,
    bitop,
    cast,
    comparison_affinity,
    concat,
    div,
    glob,
    is_true,
    like,
    mod,
    mul,
    negate,
    sort_key,
    sub,
    to_integer,
    to_number,
    to_text,
)
from .functions import SCALARS, WINDOW_ONLY, check_arity, is_aggregate_call

if TYPE_CHECKING:
    from .catalog import TableInfo

Fn = Callable[["Env"], Value]


class Context:
    """State shared by one execution of a statement."""

    __slots__ = ("params", "db", "cache")

    def __init__(self, params: tuple, db: Any) -> None:
        self.params = params
        self.db = db
        self.cache: dict[int, Any] = {}


class Env:
    __slots__ = ("row", "outer", "ctx")

    def __init__(self, row: tuple, outer: Env | None, ctx: Context) -> None:
        self.row = row
        self.outer = outer
        self.ctx = ctx


@dataclass
class Source:
    """One item of a FROM clause, as seen by name resolution."""

    name: str  # the name used to qualify columns: alias or table name
    columns: list[str]
    affinities: list[str | None]
    collations: list[str | None]
    offset: int = 0
    table: TableInfo | None = None
    rowid_col: int | None = None  # index of the hidden rowid column (tables only)
    hidden: set[int] = field(default_factory=set)  # columns left out of *
    nullable: bool = False  # right side of a LEFT JOIN
    using_hidden: set[str] = field(default_factory=set)  # columns merged by USING/NATURAL, shown once

    @property
    def width(self) -> int:
        return len(self.columns)


class Scope:
    def __init__(
        self, sources: list[Source], parent: Scope | None = None, ctes: dict[str, Any] | None = None, transparent: bool = False
    ) -> None:
        self.sources = sources
        self.transparent = transparent  # holds WITH tables only; no row of its own at run time
        self.parent = parent
        self.ctes = ctes or {}
        self.correlated = False  # set when a name resolves to a parent scope
        self.aliases: dict[str, A.Expr] = {}  # output aliases usable in WHERE/GROUP BY like SQLite

    def width(self) -> int:
        return sum(s.width for s in self.sources)

    def find_cte(self, name: str) -> Any:
        s: Scope | None = self
        key = name.lower()
        while s is not None:
            if key in s.ctes:
                return s.ctes[key]
            s = s.parent
        return None

    def lookup(self, table: str | None, name: str) -> list[tuple[Source, int]]:
        hits = []
        lname = name.lower()
        for src in self.sources:
            if table is not None and src.name.lower() != table.lower():
                continue
            for i, col in enumerate(src.columns):
                if col.lower() == lname:
                    hits.append((src, i))
                    break
        if len(hits) > 1 and table is None:
            # columns merged by USING appear in both sources; prefer the left one
            unmerged = [h for h in hits if lname not in h[0].using_hidden]
            hits = unmerged[:1] if unmerged else hits[:1]
        return hits

    def all_names(self) -> list[str]:
        out = []
        s: Scope | None = self
        while s is not None:
            for src in s.sources:
                out += [c for i, c in enumerate(src.columns) if i not in src.hidden]
                out += [f"{src.name}.{c}" for i, c in enumerate(src.columns) if i not in src.hidden]
            s = s.parent
        return out


@dataclass
class Compiled:
    fn: Fn
    affinity: str | None = None  # None means "no affinity"
    collation: str | None = None
    explicit_collation: bool = False
    constant: bool = False


def closest(name: str, candidates: list[str]) -> str | None:
    best, best_d = None, 3
    for c in candidates:
        d = _distance(name.lower(), c.lower())
        if d < best_d:
            best, best_d = c, d
    return best


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def collate_key(v: Value, collation: str | None) -> Value:
    if collation and isinstance(v, str):
        if collation == "NOCASE":
            return "".join(c.lower() if c.isascii() else c for c in v)
        if collation == "RTRIM":
            return v.rstrip(" ")
    return v


def compare_values(a: Value, b: Value, collation: str | None = None) -> int:
    a = collate_key(a, collation)
    b = collate_key(b, collation)
    ka, kb = sort_key(a), sort_key(b)
    return (ka > kb) - (ka < kb)


def _aff(a: str | None) -> str | None:
    """Column affinity as used in comparisons. BLOB is kept: it differs from "no affinity"."""
    return a


class ExprCompiler:
    """Compiles expressions for one query level."""

    def __init__(self, sql: str, query_compiler: Any) -> None:
        self.sql = sql
        self.qc = query_compiler
        self.agg_slots: dict[int, int] | None = None  # id(Func) -> row index, after grouping
        self.win_slots: dict[int, int] = {}
        self.group_exprs: dict[str, int] = {}  # GROUP BY expression text -> row index (unused fallback)
        self.allow_aggregates = False
        self.allow_windows = False
        self.where_clause = ""

    def error(self, message: str, node: A.Node, hint: str = "") -> SQLError:
        return SQLError(message, self.sql, node.pos, hint)

    # ------------------------------------------------------------ entry points

    def compile(self, node: A.Expr, scope: Scope) -> Compiled:
        method = getattr(self, "c_" + type(node).__name__)
        return method(node, scope)

    def fn(self, node: A.Expr, scope: Scope) -> Fn:
        return self.compile(node, scope).fn

    def condition(self, node: A.Expr, scope: Scope) -> Callable[[Env], bool]:
        f = self.fn(node, scope)

        def cond(env: Env) -> bool:
            return bool(is_true(f(env)))

        return cond

    # ------------------------------------------------------------ leaves

    def c_Literal(self, node: A.Literal, scope: Scope) -> Compiled:
        v = node.value
        return Compiled(lambda env: v, constant=True)

    def c_Param(self, node: A.Param, scope: Scope) -> Compiled:
        i = node.index - 1
        return Compiled(lambda env: env.ctx.params[i])

    def c_Column(self, node: A.Column, scope: Scope) -> Compiled:
        depth = 0
        crossed: list[Scope] = []
        s: Scope | None = scope
        while s is not None:
            hits = s.lookup(node.table, node.name)
            if hits:
                src, i = hits[0]
                idx = src.offset + i
                for mark in crossed:
                    mark.correlated = True
                aff = src.affinities[i]
                # a column always has a collation (BINARY by default); it outranks a later operand's
                coll = src.collations[i] or "BINARY"
                if depth == 0:
                    fn = _row_getter(idx)
                elif depth == 1:

                    def fn(env: Env, idx: int = idx) -> Value:
                        return env.outer.row[idx]  # type: ignore[union-attr]

                else:

                    def fn(env: Env, idx: int = idx, depth: int = depth) -> Value:
                        e = env
                        for _ in range(depth):
                            e = e.outer  # type: ignore[assignment]
                        return e.row[idx]

                return Compiled(fn, _aff(aff), coll)
            # SQLite lets WHERE, GROUP BY and HAVING refer to result column aliases
            if depth == 0 and node.table is None and node.name.lower() in s.aliases:
                alias_expr = s.aliases.pop(node.name.lower())
                try:
                    return self.compile(alias_expr, scope)
                finally:
                    s.aliases[node.name.lower()] = alias_expr
            if not s.transparent:
                depth += 1
                crossed.append(s)
            s = s.parent
        if node.quoted and node.table is None:
            text = node.name  # SQLite compatibility: an unmatched "identifier" is a string literal
            return Compiled(lambda env: text, constant=True)
        name = f"{node.table}.{node.name}" if node.table else node.name
        if node.table is not None and not any(src.name.lower() == node.table.lower() for sc in _chain(scope) for src in sc.sources):
            raise self.error(f"no such table: {node.table}", node, "check the FROM clause and its aliases")
        guess = closest(name, scope.all_names())
        hint = f"did you mean {guess}?" if guess else ""
        raise self.error(f"no such column: {name}", node, hint)

    # ------------------------------------------------------------ operators

    def c_Unary(self, node: A.Unary, scope: Scope) -> Compiled:
        inner = self.compile(node.operand, scope)
        f = inner.fn
        if node.op == "-":
            return Compiled(lambda env: negate(f(env)), constant=inner.constant)
        if node.op == "+":
            return Compiled(f, inner.affinity, inner.collation, constant=inner.constant)
        if node.op == "~":

            def bitnot(env: Env) -> Value:
                v = f(env)
                if v is None:
                    return None
                n = to_integer(to_number(v))
                return ~n  # type: ignore[operator]

            return Compiled(bitnot, constant=inner.constant)

        def not_(env: Env) -> Value:
            t = is_true(f(env))
            return None if t is None else int(not t)

        return Compiled(not_, constant=inner.constant)

    def _collation(self, left: Compiled, right: Compiled) -> str | None:
        if left.explicit_collation:
            return left.collation
        if right.explicit_collation:
            return right.collation
        return left.collation or right.collation

    def comparator(self, left: Compiled, right: Compiled) -> Callable[[Value, Value], int | None]:
        """A function comparing two raw operand values with SQL affinity and collation rules."""
        al, ar = comparison_affinity(left.affinity, right.affinity)
        coll = self._collation(left, right)

        def cmp(a: Value, b: Value) -> int | None:
            if a is None or b is None:
                return None
            if al:
                a = apply_affinity(a, al)
            if ar:
                b = apply_affinity(b, ar)
            return compare_values(a, b, coll)

        return cmp

    def c_Binary(self, node: A.Binary, scope: Scope) -> Compiled:
        op = node.op
        left = self.compile(node.left, scope)
        right = self.compile(node.right, scope)
        lf, rf = left.fn, right.fn
        const = left.constant and right.constant
        if op == "AND":

            def and_(env: Env) -> Value:
                a = is_true(lf(env))
                if a is False:
                    return 0
                b = is_true(rf(env))
                if b is False:
                    return 0
                if a is None or b is None:
                    return None
                return 1

            return Compiled(and_, constant=const)
        if op == "OR":

            def or_(env: Env) -> Value:
                a = is_true(lf(env))
                if a:
                    return 1
                b = is_true(rf(env))
                if b:
                    return 1
                if a is None or b is None:
                    return None
                return 0

            return Compiled(or_, constant=const)
        if op in ("=", "!=", "<", "<=", ">", ">=", "IS", "IS NOT"):
            cmp = self.comparator(left, right)
            test = {
                "=": lambda c: c == 0,
                "!=": lambda c: c != 0,
                "<": lambda c: c < 0,
                "<=": lambda c: c <= 0,
                ">": lambda c: c > 0,
                ">=": lambda c: c >= 0,
            }.get(op)
            if op in ("IS", "IS NOT"):
                want = op == "IS"

                def is_(env: Env) -> Value:
                    a, b = lf(env), rf(env)
                    if a is None or b is None:
                        return int((a is None and b is None) == want)
                    return int((cmp(a, b) == 0) == want)

                return Compiled(is_, constant=const)
            assert test is not None

            def compare(env: Env) -> Value:
                c = cmp(lf(env), rf(env))
                return None if c is None else int(test(c))

            return Compiled(compare, constant=const)
        arith = {"+": add, "-": sub, "*": mul, "/": div, "%": mod, "||": concat}.get(op)
        if arith is not None:
            return Compiled(lambda env: arith(lf(env), rf(env)), constant=const)
        if op in ("&", "|", "<<", ">>"):
            return Compiled(lambda env: bitop(op, lf(env), rf(env)), constant=const)
        raise self.error(f"unknown operator {op}", node)

    def c_Like(self, node: A.Like, scope: Scope) -> Compiled:
        lf = self.fn(node.left, scope)
        pf = self.fn(node.pattern, scope)
        ef = self.fn(node.escape, scope) if node.escape is not None else None
        neg = node.negated
        is_like = node.op == "LIKE"

        def fn(env: Env) -> Value:
            text, pattern = lf(env), pf(env)
            if text is None or pattern is None:
                return None
            if is_like:
                esc = None
                if ef is not None:
                    e = ef(env)
                    esc = to_text(e) if e is not None else None
                    if esc is not None and len(esc) != 1:
                        raise SQLError("ESCAPE expression must be a single character")
                r = like(to_text(pattern) or "", to_text(text) or "", esc)
            else:
                r = glob(to_text(pattern) or "", to_text(text) or "")
            return int(r != neg)

        return Compiled(fn)

    def c_Between(self, node: A.Between, scope: Scope) -> Compiled:
        x = self.compile(node.expr, scope)
        lo = self.compile(node.low, scope)
        hi = self.compile(node.high, scope)
        cmp_lo = self.comparator(x, lo)
        cmp_hi = self.comparator(x, hi)
        xf, lf, hf = x.fn, lo.fn, hi.fn
        neg = node.negated

        def fn(env: Env) -> Value:
            v = xf(env)
            a = cmp_lo(v, lf(env))
            b = cmp_hi(v, hf(env))
            t1 = None if a is None else a >= 0
            t2 = None if b is None else b <= 0
            if t1 is False or t2 is False:
                r: bool | None = False
            elif t1 is None or t2 is None:
                r = None
            else:
                r = True
            if r is None:
                return None
            return int(r != neg)

        return Compiled(fn)

    def c_InList(self, node: A.InList, scope: Scope) -> Compiled:
        x = self.compile(node.expr, scope)
        items = [self.compile(i, scope) for i in node.items]
        cmps = [self.comparator(x, i) for i in items]
        fns = [i.fn for i in items]
        xf = x.fn
        neg = node.negated
        if not items:
            return Compiled(lambda env: int(neg))
        # fast path: constant items without affinity changes
        if all(i.constant for i in items) and not x.collation and x.affinity is None:
            consts: list[Value] = []
            for f in fns:
                consts.append(f(None))  # type: ignore[arg-type]
            keys = {sort_key(v) for v in consts if v is not None}
            has_null = any(v is None for v in consts)

            def fast(env: Env) -> Value:
                v = xf(env)
                if v is None:
                    return None
                if sort_key(v) in keys:
                    return int(not neg)
                return None if has_null else int(neg)

            return Compiled(fast)

        def fn(env: Env) -> Value:
            v = xf(env)
            if v is None:
                return None
            saw_null = False
            for f, c in zip(fns, cmps):
                r = c(v, f(env))
                if r is None:
                    saw_null = True
                elif r == 0:
                    return int(not neg)
            return None if saw_null else int(neg)

        return Compiled(fn)

    def c_IsNull(self, node: A.IsNull, scope: Scope) -> Compiled:
        f = self.fn(node.expr, scope)
        if node.negated:
            return Compiled(lambda env: int(f(env) is not None))
        return Compiled(lambda env: int(f(env) is None))

    def c_Case(self, node: A.Case, scope: Scope) -> Compiled:
        whens = [(self.compile(c, scope), self.fn(r, scope)) for c, r in node.whens]
        other = self.fn(node.otherwise, scope) if node.otherwise is not None else (lambda env: None)
        if node.operand is not None:
            base = self.compile(node.operand, scope)
            bf = base.fn
            tests = [(self.comparator(base, c), c.fn, r) for c, r in whens]

            def case_eq(env: Env) -> Value:
                v = bf(env)
                for cmp, cf, rf in tests:
                    if cmp(v, cf(env)) == 0:
                        return rf(env)
                return other(env)

            return Compiled(case_eq)

        conds = [(c.fn, r) for c, r in whens]

        def case(env: Env) -> Value:
            for cf, rf in conds:
                if is_true(cf(env)):
                    return rf(env)
            return other(env)

        return Compiled(case)

    def c_Cast(self, node: A.Cast, scope: Scope) -> Compiled:
        from ..values import affinity_of

        f = self.fn(node.expr, scope)
        t = node.type_name
        return Compiled(lambda env: cast(f(env), t), _aff(affinity_of(t)))

    def c_Collate(self, node: A.Collate, scope: Scope) -> Compiled:
        inner = self.compile(node.expr, scope)
        coll = node.collation.upper()
        if coll not in ("BINARY", "NOCASE", "RTRIM"):
            raise self.error(f"no such collation sequence: {node.collation}", node, "use BINARY, NOCASE or RTRIM")
        return Compiled(inner.fn, inner.affinity, None if coll == "BINARY" else coll, True, inner.constant)

    # ------------------------------------------------------------ functions

    def c_Func(self, node: A.Func, scope: Scope) -> Compiled:
        name = node.name
        if node.over is not None:
            if id(node) in self.win_slots:
                idx = self.win_slots[id(node)]
                return Compiled(_row_getter(idx))
            raise self.error(f"misuse of window function {name}()", node, "window functions are allowed in the SELECT list and ORDER BY")
        if name in WINDOW_ONLY:
            raise self.error(f"{name}() needs an OVER clause", node, f"write {name}() OVER (ORDER BY ...)")
        nargs = len(node.args)
        check_arity(name, nargs, self.sql, node.pos)
        if is_aggregate_call(name, nargs, node.star):
            if self.agg_slots is not None and id(node) in self.agg_slots:
                return Compiled(_row_getter(self.agg_slots[id(node)]))
            raise self.error(
                f"misuse of aggregate function {name}()", node, "aggregates are allowed in the SELECT list, HAVING and ORDER BY"
            )
        if node.distinct or node.star:
            raise self.error(f"DISTINCT and * are only allowed in aggregate functions, not {name}()", node)
        if node.filter is not None:
            raise self.error(f"FILTER is only allowed on aggregate functions, not {name}()", node)
        special = self._special(name, node, scope)
        if special is not None:
            return special
        if name not in SCALARS:
            known = list(SCALARS) + ["count", "sum", "avg", "total", "group_concat"]
            guess = closest(name, known)
            raise self.error(f"no such function: {name}", node, f"did you mean {guess}()?" if guess else "")
        impl = SCALARS[name][0]
        args = [self.compile(a, scope) for a in node.args]
        fns = [a.fn for a in args]
        deterministic = SCALARS[name][3]
        const = deterministic and all(a.constant for a in args)
        if len(fns) == 1:
            f0 = fns[0]
            return Compiled(lambda env: impl(f0(env)), constant=const)
        if len(fns) == 2:
            f0, f1 = fns
            return Compiled(lambda env: impl(f0(env), f1(env)), constant=const)
        return Compiled(lambda env: impl(*[f(env) for f in fns]), constant=const)

    def _special(self, name: str, node: A.Func, scope: Scope) -> Compiled | None:
        """Functions that need the connection, or evaluate their arguments lazily."""
        if name == "last_insert_rowid":
            return Compiled(lambda env: env.ctx.db.last_rowid)
        if name == "changes":
            return Compiled(lambda env: env.ctx.db.last_changes)
        if name == "total_changes":
            return Compiled(lambda env: env.ctx.db.total_changes)
        if name in ("sqlite_version", "quarry_version"):
            from .. import __version__

            return Compiled(lambda env: __version__)
        if name in ("coalesce", "ifnull"):
            if len(node.args) < 2:
                raise self.error(f"{name}() needs at least two arguments", node)
            fns = [self.fn(a, scope) for a in node.args]

            def coalesce(env: Env) -> Value:
                for f in fns:
                    v = f(env)
                    if v is not None:
                        return v
                return None

            return Compiled(coalesce)
        if name == "iif" and len(node.args) == 3:
            c, a, b = (self.fn(x, scope) for x in node.args)
            return Compiled(lambda env: a(env) if is_true(c(env)) else b(env))
        if name in ("min", "max") and len(node.args) >= 2:
            args = [self.compile(a, scope) for a in node.args]
            coll = next((a.collation for a in args if a.explicit_collation), None) or next((a.collation for a in args if a.collation), None)
            if coll:
                fns = [a.fn for a in args]
                want_max = name == "max"

                def minmax(env: Env) -> Value:
                    vals = [f(env) for f in fns]
                    if any(v is None for v in vals):
                        return None
                    best = vals[0]
                    for v in vals[1:]:
                        c = compare_values(v, best, coll)
                        if (want_max and c > 0) or (not want_max and c <= 0):
                            best = v
                    return best

                return Compiled(minmax, collation=coll)
            return None
        if name == "likelihood" or name == "likely" or name == "unlikely":
            return self.compile(node.args[0], scope)
        return None

    # ------------------------------------------------------------ subqueries

    def c_Subquery(self, node: A.Subquery, scope: Scope) -> Compiled:
        plan = self.qc.compile_select(node.select, scope)
        if len(plan.columns) != 1:
            raise self.error(f"sub-select returns {len(plan.columns)} columns - expected 1", node)
        key = id(plan)
        if not plan.correlated:

            def cached(env: Env) -> Value:
                cache = env.ctx.cache
                if key not in cache:
                    first = next(iter(plan.run(env)), None)
                    cache[key] = None if first is None else first[0]
                return cache[key]

            return Compiled(cached, plan.affinities[0])

        def scalar(env: Env) -> Value:
            first = next(iter(plan.run(env)), None)
            return None if first is None else first[0]

        return Compiled(scalar, plan.affinities[0])

    def c_Exists(self, node: A.Exists, scope: Scope) -> Compiled:
        plan = self.qc.compile_select(node.select, scope)
        key = id(plan)
        if not plan.correlated:

            def cached(env: Env) -> Value:
                cache = env.ctx.cache
                if key not in cache:
                    cache[key] = int(next(iter(plan.run(env)), None) is not None)
                return cache[key]

            return Compiled(cached)
        return Compiled(lambda env: int(next(iter(plan.run(env)), None) is not None))

    def c_InSelect(self, node: A.InSelect, scope: Scope) -> Compiled:
        x = self.compile(node.expr, scope)
        plan = self.qc.compile_select(node.select, scope)
        if len(plan.columns) != 1:
            raise self.error(f"sub-select returns {len(plan.columns)} columns - expected 1", node)
        sub_aff = _aff(plan.affinities[0])
        al, ar = comparison_affinity(x.affinity, sub_aff)
        coll = x.collation
        xf = x.fn
        neg = node.negated
        key = id(plan)

        def load(env: Env) -> tuple[set, bool]:
            keys = set()
            has_null = False
            for (v,) in plan.run(env):
                if v is None:
                    has_null = True
                    continue
                if ar:
                    v = apply_affinity(v, ar)
                keys.add(sort_key(collate_key(v, coll)))
            return keys, has_null

        def fn(env: Env) -> Value:
            if plan.correlated:
                keys, has_null = load(env)
            else:
                cache = env.ctx.cache
                if key not in cache:
                    cache[key] = load(env)
                keys, has_null = cache[key]
            v = xf(env)
            if not keys and not has_null:
                return int(neg)
            if v is None:
                return None
            if al:
                v = apply_affinity(v, al)
            if sort_key(collate_key(v, coll)) in keys:
                return int(not neg)
            return None if has_null else int(neg)

        return Compiled(fn)


def _row_getter(idx: int) -> Fn:
    def get(env: Env) -> Value:
        return env.row[idx]

    return get


def _chain(scope: Scope) -> list[Scope]:
    out = []
    s: Scope | None = scope
    while s is not None:
        out.append(s)
        s = s.parent
    return out


def walk(node: Any, into_subqueries: bool = False):  # type: ignore[no-untyped-def]
    """Yield every expression node under ``node``."""
    if node is None:
        return
    if isinstance(node, list):
        for n in node:
            yield from walk(n, into_subqueries)
        return
    if isinstance(node, tuple):
        for n in node:
            yield from walk(n, into_subqueries)
        return
    if not isinstance(node, A.Node):
        return
    yield node
    if isinstance(node, (A.Subquery, A.Exists)) and not into_subqueries:
        return
    if isinstance(node, A.InSelect):
        yield from walk(node.expr, into_subqueries)
        if into_subqueries:
            yield from walk(node.select, into_subqueries)
        return
    if isinstance(node, A.Func):
        yield from walk(node.args, into_subqueries)
        yield from walk(node.filter, into_subqueries)
        if isinstance(node.over, A.WindowSpec):  # evaluated after grouping, like the select list
            yield from walk(node.over.partition_by, into_subqueries)
            yield from walk([o.expr for o in node.over.order_by], into_subqueries)
        return
    for name in getattr(node, "__dataclass_fields__", {}):
        if name in ("pos", "text"):
            continue
        yield from walk(getattr(node, name), into_subqueries)


def find_aggregates(node: Any) -> list[A.Func]:
    """Aggregate calls in an expression, not counting ones inside subqueries or window calls."""
    out = []
    for n in walk(node):
        if isinstance(n, A.Func) and n.over is None and is_aggregate_call(n.name, len(n.args), n.star):
            out.append(n)
    return out


def find_windows(node: Any) -> list[A.Func]:
    return [n for n in walk(node) if isinstance(n, A.Func) and n.over is not None]


__all__ = [
    "Compiled",
    "Context",
    "Env",
    "ExprCompiler",
    "Scope",
    "Source",
    "compare_values",
    "collate_key",
    "walk",
    "find_aggregates",
    "find_windows",
    "NUMERIC",
    "TEXT",
]
