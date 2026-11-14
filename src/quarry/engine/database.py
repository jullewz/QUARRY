"""The database engine: runs statements, keeps the schema and manages transactions."""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..errors import (
    IntegrityError,
    InterfaceError,
    NotSupported,
    OperationalError,
    ProgrammingError,
    SQLError,
)
from ..sql import ast as A
from ..sql.parser import Parser, split_statements, tokenize
from ..storage.btree import BTree
from ..storage.encoding import encode_record
from ..storage.pager import Pager
from ..storage.pages import INTERIOR, LEAF, OverflowRef
from ..values import (
    INT64_MAX,
    INTEGER,
    Value,
    affinity_of,
    apply_affinity,
    is_true,
    normalize,
    sort_key,
)
from .catalog import (
    Catalog,
    IndexInfo,
    TableInfo,
    ViewInfo,
    check_name_free,
    quote_ident,
    table_from_ast,
)
from .expr import Context, Env, ExprCompiler, Scope, Source
from .query import QueryCompiler, explain_lines, table_row_reader


@dataclass
class Result:
    columns: list[str] | None = None
    rows: list[tuple] = field(default_factory=list)
    rowcount: int = -1
    lastrowid: int | None = None


@dataclass
class _Prepared:
    stmt: A.Node
    nparams: int
    names: dict[str, int]


MISSING = object()


class Database:
    """A Quarry database: one file (plus its ``-wal`` log) or ``:memory:``."""

    def __init__(self, path: str | os.PathLike[str] = ":memory:", page_size: int = 4096, cache_size: int = 2000, sync: bool = True) -> None:
        self.path = os.fspath(path)
        self._page_size = page_size
        self._cache_size = cache_size
        self._sync = sync
        self._open()
        self.explicit = False  # inside BEGIN ... COMMIT
        self.savepoints: list[tuple[str, int]] = []
        self.savepoint_began_txn = False
        self.last_rowid = 0
        self.last_changes = 0
        self.total_changes = 0
        self._prepared: dict[str, _Prepared] = {}
        self._plans: dict[str, tuple[int, Any]] = {}  # SQL text -> (schema cookie, compiled plan)
        self._table_cache: dict[tuple[str, str, int], Any] = {}
        self.closed = False

    def _open(self) -> None:
        self.pager = Pager(None if self.path == ":memory:" else self.path, self._page_size, self._cache_size, self._sync)
        self.catalog = Catalog(self.pager)
        self.pager.on_rollback.append(self.catalog.refresh)

    # ------------------------------------------------------------ public API

    @property
    def in_transaction(self) -> bool:
        return self.explicit or bool(self.savepoints)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.pager.close()

    def _check_open(self) -> None:
        if self.closed:
            raise InterfaceError("the database is closed")

    def prepare(self, sql: str) -> _Prepared:
        p = self._prepared.get(sql)
        if p is not None:
            return p
        parser = Parser(sql)
        statements = parser.parse_script()
        if not statements:
            raise ProgrammingError("the SQL text is empty")
        if len(statements) > 1:
            raise SQLError(
                "you can only execute one statement at a time", sql, statements[1].pos, "use executescript() for several statements"
            )
        p = _Prepared(statements[0], parser.param_count, dict(parser.named))
        if len(self._prepared) > 256:
            self._prepared.clear()
        self._prepared[sql] = p
        return p

    def bind(self, prepared: _Prepared, params: Sequence[Any] | dict[str, Any] | None) -> tuple:
        n = prepared.nparams
        if params is None:
            params = ()
        if isinstance(params, dict):
            values: list[Value] = [None] * n
            if n and len(prepared.names) != n:
                raise ProgrammingError("this statement mixes ? and named parameters: pass a sequence")
            for name, idx in prepared.names.items():
                if name not in params:
                    raise ProgrammingError(f"you did not supply a value for parameter :{name}")
                values[idx - 1] = normalize(params[name])
            return tuple(values)
        if isinstance(params, (str, bytes)):
            raise ProgrammingError("parameters must be a sequence or a dict, not a single string")
        params = tuple(params)
        if len(params) != n:
            raise ProgrammingError(f"incorrect number of parameters: the statement uses {n}, and {len(params)} were supplied")
        return tuple(normalize(v) for v in params)

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> Result:
        self._check_open()
        prepared = self.prepare(sql)
        bound = self.bind(prepared, params)
        self.catalog.refresh()
        return self.run(prepared.stmt, bound, sql)

    def executescript(self, sql: str) -> None:
        for text in split_statements(sql):
            self.execute(text)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> list[tuple]:
        return self.execute(sql, params).rows

    # ------------------------------------------------------------ dispatch

    def run(self, stmt: A.Node, params: tuple, sql: str) -> Result:
        if isinstance(stmt, A.Select):
            return self._select(stmt, params, sql)
        if isinstance(stmt, A.Explain):
            return self._explain(stmt, params, sql)
        if isinstance(stmt, A.Transaction):
            return self._transaction(stmt)
        if isinstance(stmt, A.Pragma):
            return self._pragma(stmt, params, sql)
        if isinstance(stmt, A.Vacuum):
            self.vacuum()
            return Result()
        if isinstance(stmt, A.Analyze):
            return Result()
        return self._write(stmt, params, sql)

    def _write(self, stmt: A.Node, params: tuple, sql: str) -> Result:
        if self.in_transaction:
            depth = self.pager.savepoint()
            try:
                result = self._dispatch_write(stmt, params, sql)
            except BaseException:
                self.pager.rollback_to(depth)
                self.pager.release(depth)
                raise
            self.pager.release(depth)
            return result
        self.pager.begin()
        try:
            result = self._dispatch_write(stmt, params, sql)
        except BaseException:
            self.pager.rollback()
            raise
        self.pager.commit()
        return result

    def _dispatch_write(self, stmt: A.Node, params: tuple, sql: str) -> Result:
        if isinstance(stmt, A.Insert):
            return self._insert(stmt, params, sql)
        if isinstance(stmt, A.Update):
            return self._update(stmt, params, sql)
        if isinstance(stmt, A.Delete):
            return self._delete(stmt, params, sql)
        if isinstance(stmt, A.CreateTable):
            return self._create_table(stmt, params, sql)
        if isinstance(stmt, A.CreateIndex):
            return self._create_index(stmt, sql)
        if isinstance(stmt, A.CreateView):
            return self._create_view(stmt, sql)
        if isinstance(stmt, A.Drop):
            return self._drop(stmt)
        if isinstance(stmt, A.AlterTable):
            return self._alter(stmt, sql)
        raise NotSupported(f"{type(stmt).__name__} is not supported", sql, stmt.pos)

    # ------------------------------------------------------------ SELECT

    def _context(self, params: tuple) -> Context:
        return Context(params, self)

    def _select(self, stmt: A.Select, params: tuple, sql: str) -> Result:
        cookie = self.pager.header.schema_cookie
        cached = self._plans.get(sql)
        if cached is not None and cached[0] == cookie:
            plan = cached[1]
        else:
            plan = QueryCompiler(self.catalog, self.pager, sql).compile_select(stmt, None)
            if len(self._plans) > 256:
                self._plans.clear()
            self._plans[sql] = (cookie, plan)
        ctx = self._context(params)
        rows = list(plan.run(Env((), None, ctx)))
        return Result(list(plan.columns), rows, -1)

    def plan_select(self, sql: str) -> Any:
        prepared = self.prepare(sql)
        assert isinstance(prepared.stmt, A.Select)
        return QueryCompiler(self.catalog, self.pager, sql).compile_select(prepared.stmt, None)

    def _explain(self, stmt: A.Explain, params: tuple, sql: str) -> Result:
        inner = stmt.statement
        qc = QueryCompiler(self.catalog, self.pager, sql)
        lines: list[tuple[int, str]] = []
        if isinstance(inner, A.Select):
            lines = explain_lines(qc.compile_select(inner, None))
        elif isinstance(inner, (A.Update, A.Delete)):
            table = self._table(inner.table, sql, inner.pos)
            lines = explain_lines(qc.compile_select(self._target_select(table, inner.alias, inner.where), None))
            verb = "UPDATE" if isinstance(inner, A.Update) else "DELETE FROM"
            lines.insert(0, (0, f"{verb} {table.name}"))
            lines = [(0, t) if k == 0 else (d + 1, t) for k, (d, t) in enumerate(lines)]
        elif isinstance(inner, A.Insert):
            lines = [(0, f"INSERT INTO {inner.table}")]
            if inner.source is not None:
                lines += [(d + 1, t) for d, t in explain_lines(qc.compile_select(inner.source, None))]
        else:
            lines = [(0, f"{type(inner).__name__.upper()} (no query plan)")]
        # id, parent, notused, detail like SQLite's EXPLAIN QUERY PLAN
        rows = []
        stack: list[tuple[int, int]] = []
        for i, (depth, text) in enumerate(lines, 1):
            while stack and stack[-1][0] >= depth:
                stack.pop()
            parent = stack[-1][1] if stack else 0
            rows.append((i, parent, 0, text))
            stack.append((depth, i))
        return Result(["id", "parent", "notused", "detail"], rows)

    # ------------------------------------------------------------ helpers

    def _table(self, name: str, sql: str, pos: int) -> TableInfo:
        table = self.catalog.table(name)
        if table is None:
            if name.lower() in self.catalog.views:
                raise SQLError(f"cannot modify {name} because it is a view", sql, pos)
            from .expr import closest

            guess = closest(name, list(self.catalog.tables))
            raise SQLError(f"no such table: {name}", sql, pos, f"did you mean {guess}?" if guess else "")
        return table

    @staticmethod
    def _target_select(table: TableInfo, alias: str | None, where: A.Expr | None) -> A.Select:
        ref = A.TableRef(table.name, alias)
        rowid = A.Column(alias or table.name, _rowid_name(table))
        core = A.SelectCore([A.ResultColumn(None, star=True), A.ResultColumn(rowid)], ref, where)
        return A.Select([core])

    def _row_scope(self, table: TableInfo, alias: str | None = None, extra: list[Source] | None = None) -> Scope:
        cols = [c.name for c in table.columns] + [_rowid_name(table)]
        src = Source(
            alias or table.name,
            cols,
            [c.affinity for c in table.columns] + [INTEGER],
            [c.collation for c in table.columns] + [None],
            table=table,
            rowid_col=len(cols) - 1,
            hidden={len(cols) - 1},
        )
        sources = [src]
        offset = src.width
        for s in extra or []:
            s.offset = offset
            offset += s.width
            sources.append(s)
        return Scope(sources)

    def _per_table(self, kind: str, table: TableInfo, build: Any) -> Any:
        """Compiled DEFAULT/CHECK code, reused until the schema changes."""
        key = (kind, table.name.lower(), self.pager.header.schema_cookie)
        hit = self._table_cache.get(key)
        if hit is None:
            if len(self._table_cache) > 512:
                self._table_cache.clear()
            hit = self._table_cache[key] = build()
        return hit

    def _defaults(self, table: TableInfo, sql: str) -> list[Any]:
        return self._per_table("defaults", table, lambda: self._build_defaults(table, sql))

    def _build_defaults(self, table: TableInfo, sql: str) -> list[Any]:
        ec = ExprCompiler(sql, QueryCompiler(self.catalog, self.pager, sql))
        out: list[Any] = []
        for c in table.columns:
            out.append(ec.fn(c.default, Scope([])) if c.default is not None else None)
        return out

    def _checks(self, table: TableInfo, sql: str) -> list[tuple[str, Any]]:
        return self._per_table("checks", table, lambda: self._build_checks(table, sql))

    def _build_checks(self, table: TableInfo, sql: str) -> list[tuple[str, Any]]:
        ec = ExprCompiler(table.sql, QueryCompiler(self.catalog, self.pager, table.sql))
        scope = self._row_scope(table)
        checks = []
        for c in table.columns:
            for e in c.checks:
                checks.append((e.text, ec.fn(e, scope)))
        for e in table.checks:
            checks.append((e.text, ec.fn(e, scope)))
        return checks

    def _returning(self, cols: list[A.ResultColumn] | None, table: TableInfo, sql: str) -> tuple[list[str], list[Any]] | None:
        if not cols:
            return None
        qc = QueryCompiler(self.catalog, self.pager, sql)
        ec = ExprCompiler(sql, qc)
        scope = self._row_scope(table)
        names: list[str] = []
        fns: list[Any] = []
        for rc in cols:
            if rc.star:
                for i, c in enumerate(table.columns):
                    names.append(c.name)
                    fns.append(_getter(i))
                continue
            assert rc.expr is not None
            names.append(rc.alias or (rc.expr.name if isinstance(rc.expr, A.Column) else rc.expr.text))
            fns.append(ec.fn(rc.expr, scope))
        return names, fns

    def _index_key(self, ix: IndexInfo, row: Sequence[Value], rowid: int) -> tuple:
        return tuple(row[i] if i < len(row) else rowid for i in ix.col_idx) + (rowid,)

    def _conflicts(self, table: TableInfo, row: Sequence[Value], rowid: int, old_rowid: int | None) -> list[tuple[str, int]]:
        """Rows that a write would collide with: (constraint description, rowid)."""
        out = []
        tree = BTree(self.pager, table.root)
        if rowid != old_rowid and tree.get((rowid,)) is not None:
            col = table.columns[table.rowid_alias].name if table.rowid_alias is not None else "rowid"
            out.append((f"{table.name}.{col}", rowid))
        for ix in table.indexes:
            if not ix.unique:
                continue
            key = self._index_key(ix, row, rowid)[:-1]
            if any(v is None for v in key):
                continue
            for k, _v in BTree(self.pager, ix.root).scan(key, True, key, True, prefix=True):
                other = k[-1]
                if other != old_rowid:
                    desc = ", ".join(f"{table.name}.{c}" for c in ix.columns)
                    out.append((desc, other))
        return out

    def _write_row(self, table: TableInfo, row: list[Value], rowid: int) -> None:
        stored = list(row)
        if table.rowid_alias is not None:
            stored[table.rowid_alias] = None
        BTree(self.pager, table.root).insert((rowid,), encode_record(stored))
        for ix in table.indexes:
            BTree(self.pager, ix.root).insert(self._index_key(ix, row, rowid), b"")

    def _delete_row(self, table: TableInfo, row: Sequence[Value], rowid: int) -> None:
        BTree(self.pager, table.root).delete((rowid,))
        for ix in table.indexes:
            BTree(self.pager, ix.root).delete(self._index_key(ix, row, rowid))

    def _read_row(self, table: TableInfo, rowid: int) -> list[Value] | None:
        raw = BTree(self.pager, table.root).get((rowid,))
        if raw is None:
            return None
        return list(table_row_reader(table)((rowid,), raw)[:-1])

    def _new_rowid(self, table: TableInfo) -> int:
        last = BTree(self.pager, table.root).last_key()
        top = last[0] if last else 0
        if table.autoincrement:
            top = max(top, self.catalog.sequences.get(table.name.lower(), 0))
        if top >= INT64_MAX:
            if table.autoincrement:
                raise OperationalError("database or disk is full: AUTOINCREMENT reached the largest rowid")
            import random

            tree = BTree(self.pager, table.root)
            for _ in range(100):
                candidate = random.randint(1, INT64_MAX)
                if tree.get((candidate,)) is None:
                    return candidate
            raise OperationalError("database or disk is full: no free rowid found")
        return top + 1

    def _validate(
        self, table: TableInfo, row: list[Value], checks: list[tuple[str, Any]], action: str, defaults: list[Any], ctx: Context, rowid: int
    ) -> bool:
        """NOT NULL and CHECK constraints. Returns False when the row should be skipped (OR IGNORE)."""
        for i, c in enumerate(table.columns):
            if c.not_null and row[i] is None and i != table.rowid_alias:
                if action == "IGNORE":
                    return False
                if action == "REPLACE" and defaults[i] is not None:
                    row[i] = apply_affinity(defaults[i](Env((), None, ctx)), c.affinity)
                    if row[i] is not None:
                        continue
                raise IntegrityError(f"NOT NULL constraint failed: {table.name}.{c.name}")
        if checks:
            env = Env(tuple(row) + (rowid,), None, ctx)
            for text, fn in checks:
                v = fn(env)
                if v is not None and not is_true(v):
                    if action == "IGNORE":
                        return False
                    raise IntegrityError(f"CHECK constraint failed: {text}")
        return True

    # ------------------------------------------------------------ INSERT

    def _insert(self, stmt: A.Insert, params: tuple, sql: str) -> Result:
        if getattr(stmt, "ctes", None):
            raise NotSupported("WITH before INSERT is not supported yet", sql, stmt.pos, "move the WITH clause into the SELECT")
        table = self._table(stmt.table, sql, stmt.pos)
        ncols = len(table.columns)
        if stmt.columns is not None:
            targets = []
            for name in stmt.columns:
                i = table.require_column(name, sql, stmt.pos)
                if i in targets:
                    raise SQLError(f"column {name} is listed twice", sql, stmt.pos)
                targets.append(i)
        else:
            targets = list(range(ncols))
        ctx = self._context(params)
        qc = QueryCompiler(self.catalog, self.pager, sql)
        if stmt.source is None:
            source_rows: list[tuple] = [()]
            width = 0
            targets = []
        else:
            plan = qc.compile_select(stmt.source, None)
            width = len(plan.columns)
            source_rows = list(plan.run(Env((), None, ctx)))
        if stmt.source is not None and width != len(targets):
            if stmt.columns is None:
                raise SQLError(f"table {table.name} has {ncols} columns but {width} values were supplied", sql, stmt.pos)
            raise SQLError(f"{width} values for {len(targets)} columns", sql, stmt.pos)
        defaults = self._defaults(table, sql)
        checks = self._checks(table, sql)
        returning = self._returning(stmt.returning, table, sql)
        out_rows: list[tuple] = []
        upsert = self._compile_upsert(stmt, table, sql) if stmt.upsert is not None else None
        count = 0
        seq_max = 0
        for src in source_rows:
            row: list[Value] = [MISSING] * ncols  # type: ignore[list-item]
            rowid: Value = None
            for t, v in zip(targets, src):
                if t == ncols:
                    rowid = v
                else:
                    row[t] = v
            denv = Env((), None, ctx)
            for i, c in enumerate(table.columns):
                if row[i] is MISSING:
                    row[i] = defaults[i](denv) if defaults[i] is not None else None
                row[i] = apply_affinity(row[i], c.affinity)
            if table.rowid_alias is not None and row[table.rowid_alias] is not None:
                rowid = row[table.rowid_alias]
            if rowid is not None:
                rowid = apply_affinity(rowid, INTEGER)
                if not isinstance(rowid, int):
                    raise IntegrityError("datatype mismatch: the INTEGER PRIMARY KEY must be an integer")
            else:
                rowid = self._new_rowid(table)
            if table.rowid_alias is not None:
                row[table.rowid_alias] = rowid
            if not self._validate(table, row, checks, stmt.on_conflict, defaults, ctx, rowid):
                continue
            conflicts = self._conflicts(table, row, rowid, None)
            if conflicts:
                if upsert is not None and upsert.matches(conflicts):
                    if upsert.action == "NOTHING":
                        continue
                    updated = self._upsert_update(table, upsert, conflicts[0][1], row, rowid, ctx, checks, defaults)
                    if updated is not None:
                        count += 1
                        if returning:
                            renv = Env(tuple(updated[0]) + (updated[1],), None, ctx)
                            out_rows.append(tuple(f(renv) for f in returning[1]))
                    continue
                action = stmt.on_conflict
                if action == "IGNORE":
                    continue
                if action == "REPLACE":
                    for _desc, other in {c[1]: c for c in conflicts}.values():
                        old = self._read_row(table, other)
                        if old is not None:
                            self._delete_row(table, old, other)
                else:
                    raise IntegrityError(f"UNIQUE constraint failed: {conflicts[0][0]}")
            self._write_row(table, row, rowid)  # type: ignore[arg-type]
            seq_max = max(seq_max, rowid)  # type: ignore[type-var]
            count += 1
            self.last_rowid = rowid  # type: ignore[assignment]
            if returning:
                renv = Env(tuple(row) + (rowid,), None, ctx)
                out_rows.append(tuple(f(renv) for f in returning[1]))
        if table.autoincrement and seq_max > self.catalog.sequences.get(table.name.lower(), 0):
            self.catalog.set_sequence(table.name, seq_max)
        self._changed(count)
        if returning:
            return Result(returning[0], out_rows, count, self.last_rowid)
        return Result(None, [], count, self.last_rowid)

    def _compile_upsert(self, stmt: A.Insert, table: TableInfo, sql: str) -> _Upsert:
        up = stmt.upsert
        assert up is not None
        target_desc = None
        if up.target:
            cols = [table.columns[table.require_column(c, sql, up.pos)].name for c in up.target]
            target_desc = ", ".join(f"{table.name}.{c}" for c in cols)
            valid = {", ".join(f"{table.name}.{c}" for c in ix.columns) for ix in table.indexes if ix.unique}
            if table.rowid_alias is not None:
                valid.add(f"{table.name}.{table.columns[table.rowid_alias].name}")
            if target_desc.lower() not in {v.lower() for v in valid}:
                raise SQLError("ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint", sql, up.pos)
        result = _Upsert(up.action, target_desc)
        if up.action == "UPDATE":
            excluded = Source(
                "excluded",
                [c.name for c in table.columns] + ["rowid"],
                [c.affinity for c in table.columns] + [INTEGER],
                [c.collation for c in table.columns] + [None],
            )
            scope = self._row_scope(table, stmt.alias, [excluded])
            ec = ExprCompiler(sql, QueryCompiler(self.catalog, self.pager, sql))
            for names, expr in up.assignments:
                if len(names) != 1:
                    raise NotSupported("row-value assignments are not supported yet", sql, up.pos)
                result.assignments.append((table.require_column(names[0], sql, up.pos), ec.fn(expr, scope)))
            if up.where is not None:
                result.where = ec.condition(up.where, scope)
        return result

    def _upsert_update(
        self,
        table: TableInfo,
        up: _Upsert,
        target_rowid: int,
        new_row: list[Value],
        new_rowid: int,
        ctx: Context,
        checks: list,
        defaults: list,
    ) -> tuple[list[Value], int] | None:
        old = self._read_row(table, target_rowid)
        assert old is not None
        env = Env(tuple(old) + (target_rowid,) + tuple(new_row) + (new_rowid,), None, ctx)
        if up.where is not None and not up.where(env):
            return None
        row = list(old)
        rowid = target_rowid
        for i, fn in up.assignments:
            v = fn(env)
            if i == len(table.columns):
                rowid = apply_affinity(v, INTEGER)  # type: ignore[assignment]
            else:
                row[i] = apply_affinity(v, table.columns[i].affinity)
        if table.rowid_alias is not None:
            if row[table.rowid_alias] != old[table.rowid_alias]:
                rowid = row[table.rowid_alias]  # type: ignore[assignment]
            row[table.rowid_alias] = rowid
        if not isinstance(rowid, int):
            raise IntegrityError("datatype mismatch: the INTEGER PRIMARY KEY must be an integer")
        self._validate(table, row, checks, "ABORT", defaults, ctx, rowid)
        conflicts = self._conflicts(table, row, rowid, target_rowid)
        if conflicts:
            raise IntegrityError(f"UNIQUE constraint failed: {conflicts[0][0]}")
        self._delete_row(table, old, target_rowid)
        self._write_row(table, row, rowid)
        return row, rowid

    # ------------------------------------------------------------ UPDATE

    def _update(self, stmt: A.Update, params: tuple, sql: str) -> Result:
        if stmt.from_ is not None:
            raise NotSupported("UPDATE ... FROM is not supported yet", sql, stmt.pos, "use a subquery in SET or WHERE")
        table = self._table(stmt.table, sql, stmt.pos)
        ncols = len(table.columns)
        ctx = self._context(params)
        qc = QueryCompiler(self.catalog, self.pager, sql)
        plan = qc.compile_select(self._target_select(table, stmt.alias, stmt.where), None)
        targets = list(plan.run(Env((), None, ctx)))
        scope = self._row_scope(table, stmt.alias)
        ec = ExprCompiler(sql, qc)
        assigns: list[tuple[int, Any]] = []
        for names, expr in stmt.assignments:
            if len(names) != 1:
                raise NotSupported("row-value assignments are not supported yet", sql, stmt.pos)
            assigns.append((table.require_column(names[0], sql, expr.pos), ec.fn(expr, scope)))
        checks = self._checks(table, sql)
        defaults = self._defaults(table, sql)
        returning = self._returning(stmt.returning, table, sql)
        out_rows = []
        count = 0
        for full in targets:
            old_rowid = full[-1]
            old = list(full[:ncols])
            if BTree(self.pager, table.root).get((old_rowid,)) is None:
                continue  # removed by an earlier OR REPLACE in this statement
            env = Env(tuple(old) + (old_rowid,), None, ctx)
            row = list(old)
            rowid = old_rowid
            for i, fn in assigns:
                v = fn(env)
                if i == ncols:
                    rowid = v
                else:
                    row[i] = apply_affinity(v, table.columns[i].affinity)
            if table.rowid_alias is not None and row[table.rowid_alias] != old[table.rowid_alias]:
                rowid = row[table.rowid_alias]
            if rowid is None:
                raise IntegrityError(
                    f"NOT NULL constraint failed: {table.name}.rowid"
                    if table.rowid_alias is None
                    else f"datatype mismatch: {table.name}.{table.columns[table.rowid_alias].name} cannot be NULL"
                )
            rowid = apply_affinity(rowid, INTEGER)
            if not isinstance(rowid, int):
                raise IntegrityError("datatype mismatch: the INTEGER PRIMARY KEY must be an integer")
            if table.rowid_alias is not None:
                row[table.rowid_alias] = rowid
            if not self._validate(table, row, checks, stmt.on_conflict, defaults, ctx, rowid):
                continue
            conflicts = self._conflicts(table, row, rowid, old_rowid)
            if conflicts:
                if stmt.on_conflict == "IGNORE":
                    continue
                if stmt.on_conflict == "REPLACE":
                    for _desc, other in {c[1]: c for c in conflicts}.values():
                        o = self._read_row(table, other)
                        if o is not None and other != old_rowid:
                            self._delete_row(table, o, other)
                else:
                    raise IntegrityError(f"UNIQUE constraint failed: {conflicts[0][0]}")
            self._delete_row(table, old, old_rowid)
            self._write_row(table, row, rowid)
            count += 1
            if returning:
                renv = Env(tuple(row) + (rowid,), None, ctx)
                out_rows.append(tuple(f(renv) for f in returning[1]))
        self._changed(count)
        if returning:
            return Result(returning[0], out_rows, count)
        return Result(None, [], count)

    # ------------------------------------------------------------ DELETE

    def _delete(self, stmt: A.Delete, params: tuple, sql: str) -> Result:
        table = self._table(stmt.table, sql, stmt.pos)
        ctx = self._context(params)
        returning = self._returning(stmt.returning, table, sql)
        if stmt.where is None and not returning:
            count = BTree(self.pager, table.root).count()
            BTree(self.pager, table.root).clear()
            for ix in table.indexes:
                BTree(self.pager, ix.root).clear()
            self._changed(count)
            return Result(None, [], count)
        qc = QueryCompiler(self.catalog, self.pager, sql)
        plan = qc.compile_select(self._target_select(table, stmt.alias, stmt.where), None)
        targets = list(plan.run(Env((), None, ctx)))
        ncols = len(table.columns)
        out_rows = []
        for full in targets:
            rowid = full[-1]
            row = full[:ncols]
            self._delete_row(table, row, rowid)
            if returning:
                renv = Env(tuple(row) + (rowid,), None, ctx)
                out_rows.append(tuple(f(renv) for f in returning[1]))
        self._changed(len(targets))
        if returning:
            return Result(returning[0], out_rows, len(targets))
        return Result(None, [], len(targets))

    def _changed(self, n: int) -> None:
        self.last_changes = n
        self.total_changes += n

    # ------------------------------------------------------------ DDL

    def _create_table(self, stmt: A.CreateTable, params: tuple, sql: str) -> Result:
        if self.catalog.exists(stmt.name):
            if stmt.if_not_exists and self.catalog.table(stmt.name) is not None:
                return Result()
            check_name_free(self.catalog, stmt.name, sql, stmt.pos)
        check_name_free(self.catalog, stmt.name, sql, stmt.pos)
        if stmt.as_select is not None:
            plan = QueryCompiler(self.catalog, self.pager, sql).compile_select(stmt.as_select, None)
            types = {"INTEGER": "INT", "REAL": "REAL", "TEXT": "TEXT", "NUMERIC": "NUM"}
            cols = []
            seen: set[str] = set()
            for name, aff in zip(plan.columns, plan.affinities):
                base = name
                k = 1
                while name.lower() in seen:
                    name = f"{base}:{k}"
                    k += 1
                seen.add(name.lower())
                cols.append(f"{quote_ident(name)} {types.get(aff or '', '')}".rstrip())
            create_sql = f"CREATE TABLE {quote_ident(stmt.name)} ({', '.join(cols)})"
            parser = Parser(create_sql)
            created = parser.parse_script()[0]
            assert isinstance(created, A.CreateTable)
            self._create_table(created, params, create_sql)
            table = self.catalog.table(stmt.name)
            assert table is not None
            ctx = self._context(params)
            for r in plan.run(Env((), None, ctx)):
                row = [apply_affinity(v, c.affinity) for v, c in zip(r, table.columns)]
                self._write_row(table, row, self._new_rowid(table))
            return Result()
        root = BTree.create(self.pager)
        table = table_from_ast(stmt, root, sql)
        # validate DEFAULT and CHECK expressions now, not at the first INSERT
        ec = ExprCompiler(sql, QueryCompiler(self.catalog, self.pager, sql))
        scope = self._row_scope(table)
        for c in table.columns:
            if c.default is not None:
                ec.fn(c.default, Scope([]))
            for e in c.checks:
                ec.fn(e, scope)
        for e in table.checks:
            ec.fn(e, scope)
        self.catalog.add_table(table)
        for n, cols in enumerate(Catalog.constraint_groups(table), 1):
            ix = IndexInfo(f"quarry_autoindex_{table.name}_{n}", table.name, cols, True, BTree.create(self.pager), None)
            self.catalog.add_index(table, ix)
        return Result()

    def _create_index(self, stmt: A.CreateIndex, sql: str) -> Result:
        if stmt.where is not None:
            raise NotSupported("partial indexes (CREATE INDEX ... WHERE) are not supported yet", sql, stmt.where.pos)
        if self.catalog.exists(stmt.name):
            if stmt.if_not_exists and stmt.name.lower() in self.catalog.indexes:
                return Result()
        check_name_free(self.catalog, stmt.name, sql, stmt.pos)
        table = self._table(stmt.table, sql, stmt.pos)
        for c in stmt.columns:
            i = table.require_column(c, sql, stmt.pos)
            if i == len(table.columns):
                raise SQLError("cannot index the rowid: it is already the table's key", sql, stmt.pos)
        ix = IndexInfo(stmt.name, table.name, list(stmt.columns), stmt.unique, BTree.create(self.pager), sql)
        self.catalog.add_index(table, ix)
        self._fill_index(table, ix)
        return Result()

    def _fill_index(self, table: TableInfo, ix: IndexInfo) -> None:
        reader = table_row_reader(table)
        itree = BTree(self.pager, ix.root)
        for k, raw in BTree(self.pager, table.root).scan():
            row = reader(k, raw)
            key = self._index_key(ix, row[:-1], k[0])
            if ix.unique and all(v is not None for v in key[:-1]):
                for _other in itree.scan(key[:-1], True, key[:-1], True, prefix=True):
                    cols = ", ".join(f"{table.name}.{c}" for c in ix.columns)
                    raise IntegrityError(f"UNIQUE constraint failed: {cols}")
            itree.insert(key, b"")

    def _create_view(self, stmt: A.CreateView, sql: str) -> Result:
        if self.catalog.exists(stmt.name):
            if stmt.if_not_exists and stmt.name.lower() in self.catalog.views:
                return Result()
        check_name_free(self.catalog, stmt.name, sql, stmt.pos)
        plan = QueryCompiler(self.catalog, self.pager, sql).compile_select(stmt.select, None)
        if stmt.columns and len(stmt.columns) != len(plan.columns):
            raise SQLError(f"expected {len(plan.columns)} columns for view {stmt.name} but got {len(stmt.columns)}", sql, stmt.pos)
        self.catalog.add_view(ViewInfo(stmt.name, sql, stmt.select, stmt.columns))
        return Result()

    def _drop(self, stmt: A.Drop) -> Result:
        name = stmt.name
        kind = self.catalog.exists(name)
        if kind is None or kind != stmt.kind.lower():
            if stmt.if_exists:
                return Result()
            if kind is not None:
                raise OperationalError(f"use DROP {kind.upper()} to drop {kind} {name}")
            raise OperationalError(f"no such {stmt.kind.lower()}: {name}")
        if kind == "table":
            table = self.catalog.tables[name.lower()]
            BTree(self.pager, table.root).drop()
            for ix in table.indexes:
                BTree(self.pager, ix.root).drop()
            self.catalog.drop_table(table)
        elif kind == "index":
            ix = self.catalog.indexes[name.lower()]
            if ix.auto:
                raise OperationalError("an index created by a UNIQUE or PRIMARY KEY constraint cannot be dropped")
            BTree(self.pager, ix.root).drop()
            self.catalog.drop_index(ix)
        else:
            self.catalog.drop_view(self.catalog.views[name.lower()])
        return Result()

    def _alter(self, stmt: A.AlterTable, sql: str) -> Result:
        table = self._table(stmt.table, sql, stmt.pos)
        old_name = table.name
        if stmt.action == "RENAME":
            assert stmt.new_name is not None
            if stmt.new_name.lower() != old_name.lower():
                check_name_free(self.catalog, stmt.new_name, sql, stmt.pos)
            for ix in table.indexes:
                if ix.auto:
                    new_ix = f"quarry_autoindex_{stmt.new_name}_{ix.name.rsplit('_', 1)[1]}"
                    self.catalog.indexes.pop(ix.name.lower(), None)
                    ix.name = new_ix
                    self.catalog.indexes[new_ix.lower()] = ix
            self.catalog._delete_rows(lambda r: r[0] == "index" and r[2].lower() == old_name.lower() and r[4] is None)
            table.name = stmt.new_name
            self.catalog.rewrite_table(old_name, table)
            for ix in table.indexes:
                if ix.auto:
                    self.catalog._add_row("index", ix.name, table.name, ix.root, None)
            return Result()
        if stmt.action == "RENAME COLUMN":
            assert stmt.old_column is not None and stmt.new_name is not None
            i = table.require_column(stmt.old_column, sql, stmt.pos)
            if i == len(table.columns):
                raise SQLError("cannot rename the rowid", sql, stmt.pos)
            if table.column_index(stmt.new_name) not in (-1, len(table.columns), i):
                raise SQLError(f"duplicate column name: {stmt.new_name}", sql, stmt.pos)
            old = table.columns[i].name
            new = stmt.new_name
            table.columns[i].name = new

            def ren(names: list[str]) -> list[str]:
                return [new if n.lower() == old.lower() else n for n in names]

            table.pk_columns = ren(table.pk_columns)
            table.constraint_uniques = [ren(u) for u in table.constraint_uniques]
            for ix in table.indexes:
                ix.columns = ren(ix.columns)
            for c in table.columns:
                c.checks = [_rename_expr(e, old, new) for e in c.checks]
            table.checks = [_rename_expr(e, old, new) for e in table.checks]
            self.catalog.rewrite_table(old_name, table)
            return Result()
        if stmt.action == "ADD COLUMN":
            cd = stmt.column
            assert cd is not None
            if table.column_index(cd.name) not in (-1, len(table.columns)):
                raise SQLError(f"duplicate column name: {cd.name}", sql, stmt.pos)
            if cd.primary_key:
                raise SQLError("cannot add a PRIMARY KEY column", sql, stmt.pos)
            if cd.unique:
                raise SQLError("cannot add a UNIQUE column", sql, stmt.pos)
            if (
                cd.default is not None
                and not isinstance(cd.default, A.Literal)
                and not (isinstance(cd.default, A.Unary) and isinstance(cd.default.operand, A.Literal))
            ):
                raise SQLError("cannot add a column with a non-constant default", sql, stmt.pos)
            if isinstance(cd.default, A.Unary):
                cd.default = A.Literal(-cd.default.operand.value if cd.default.op == "-" else cd.default.operand.value)  # type: ignore[union-attr]
                cd.default.text = str(cd.default.value)
            if cd.not_null and (cd.default is None or (isinstance(cd.default, A.Literal) and cd.default.value is None)):
                raise SQLError("cannot add a NOT NULL column with default value NULL", sql, stmt.pos)
            from .catalog import ColumnInfo

            col = ColumnInfo(
                cd.name,
                cd.type_name,
                affinity_of(cd.type_name),
                cd.not_null,
                cd.default,
                cd.default.text if cd.default is not None else None,
                False,
                cd.collation,
                list(cd.check),
            )
            if isinstance(cd.default, A.Literal):
                cd.default.value = apply_affinity(cd.default.value, col.affinity)
            table.columns.append(col)
            self.catalog.rewrite_table(old_name, table)
            return Result()
        # DROP COLUMN
        assert stmt.old_column is not None
        i = table.require_column(stmt.old_column, sql, stmt.pos)
        if i == len(table.columns):
            raise SQLError("cannot drop the rowid", sql, stmt.pos)
        cname = table.columns[i].name
        if any(c.lower() == cname.lower() for c in table.pk_columns):
            raise SQLError(f'cannot drop PRIMARY KEY column: "{cname}"', sql, stmt.pos)
        if any(cname.lower() in [c.lower() for c in u] for u in table.constraint_uniques):
            raise SQLError(f'cannot drop UNIQUE column: "{cname}"', sql, stmt.pos)
        if any(cname.lower() in [c.lower() for c in ix.columns] for ix in table.indexes):
            raise SQLError(f'cannot drop column "{cname}": it is indexed', sql, stmt.pos, "drop the index first")
        if len(table.columns) == 1:
            raise SQLError("cannot drop the only column of a table", sql, stmt.pos)
        reader = table_row_reader(table)
        rows = [(k, reader(k, raw)) for k, raw in BTree(self.pager, table.root).scan()]
        del table.columns[i]
        if table.rowid_alias is not None and table.rowid_alias > i:
            table.rowid_alias -= 1
        tree = BTree(self.pager, table.root)
        for k, row in rows:
            new = list(row[:-1])
            del new[i]
            if table.rowid_alias is not None:
                new[table.rowid_alias] = None
            tree.insert(k, encode_record(new))
        for ix in table.indexes:
            ix.col_idx = [table.require_column(c) for c in ix.columns]
        self.catalog.rewrite_table(old_name, table)
        return Result()

    # ------------------------------------------------------------ transactions

    def _transaction(self, stmt: A.Transaction) -> Result:
        action = stmt.action
        if action == "BEGIN":
            if self.in_transaction:
                raise OperationalError("cannot start a transaction within a transaction")
            self.pager.begin()
            self.explicit = True
        elif action == "COMMIT":
            if not self.in_transaction:
                raise OperationalError("cannot commit - no transaction is active")
            self.pager.commit()
            self.explicit = False
            self.savepoints.clear()
        elif action == "ROLLBACK" and stmt.savepoint is None:
            if not self.in_transaction:
                raise OperationalError("cannot rollback - no transaction is active")
            self.pager.rollback()
            self.explicit = False
            self.savepoints.clear()
        elif action == "SAVEPOINT":
            if not self.in_transaction:
                self.pager.begin()
                self.savepoint_began_txn = True
            depth = self.pager.savepoint()
            self.savepoints.append((stmt.savepoint or "", depth))
        else:
            name = (stmt.savepoint or "").lower()
            k = next((j for j in range(len(self.savepoints) - 1, -1, -1) if self.savepoints[j][0].lower() == name), None)
            if k is None:
                raise OperationalError(f"no such savepoint: {stmt.savepoint}")
            depth = self.savepoints[k][1]
            if action == "RELEASE":
                self.pager.release(depth)
                del self.savepoints[k:]
                if not self.savepoints and not self.explicit:
                    self.pager.commit()
                    self.savepoint_began_txn = False
            else:  # ROLLBACK TO
                self.pager.rollback_to(depth)
                del self.savepoints[k + 1 :]
        self.catalog.refresh()
        return Result()

    def commit(self) -> None:
        if self.in_transaction:
            self.pager.commit()
        self.explicit = False
        self.savepoints.clear()

    def rollback(self) -> None:
        if self.in_transaction:
            self.pager.rollback()
        self.explicit = False
        self.savepoints.clear()

    # ------------------------------------------------------------ PRAGMA

    def _pragma(self, stmt: A.Pragma, params: tuple, sql: str) -> Result:
        name = stmt.name
        arg = stmt.argument
        h = self.pager.header
        if name == "table_info" or name == "table_xinfo":
            table = self._table(arg or "", sql, stmt.pos)
            rows = []
            for i, c in enumerate(table.columns):
                pk = 0
                if any(p.lower() == c.name.lower() for p in table.pk_columns):
                    pk = [p.lower() for p in table.pk_columns].index(c.name.lower()) + 1
                rows.append((i, c.name, c.type_name, int(c.not_null), c.default_sql, pk))
            return Result(["cid", "name", "type", "notnull", "dflt_value", "pk"], rows)
        if name == "index_list":
            table = self._table(arg or "", sql, stmt.pos)
            rows = [
                (i, ix.name, int(ix.unique), "c" if ix.sql else ("pk" if ix.columns == table.pk_columns else "u"), 0)
                for i, ix in enumerate(table.indexes)
            ]
            return Result(["seq", "name", "unique", "origin", "partial"], rows)
        if name == "index_info":
            ix = self.catalog.indexes.get((arg or "").lower())
            if ix is None:
                return Result(["seqno", "cid", "name"], [])
            return Result(["seqno", "cid", "name"], [(i, cid, c) for i, (cid, c) in enumerate(zip(ix.col_idx, ix.columns))])
        if name in ("integrity_check", "quick_check"):
            problems = self.integrity_check()
            return Result([name], [(p,) for p in problems] or [("ok",)])
        if name == "page_size":
            if stmt.value is not None:
                if h.page_count > 2 or self.catalog.tables:
                    return Result(["page_size"], [(self.pager.page_size,)])
            return Result(["page_size"], [(self.pager.page_size,)])
        if name == "page_count":
            return Result(["page_count"], [(h.page_count,)])
        if name == "freelist_count":
            return Result(["freelist_count"], [(h.free_count,)])
        if name == "schema_version":
            return Result(["schema_version"], [(h.schema_cookie,)])
        if name == "user_version":
            if stmt.value is not None:
                v = int(stmt.value)  # type: ignore[arg-type]
                self._write_header(lambda hh: setattr(hh, "user_version", v))
                return Result()
            return Result(["user_version"], [(h.user_version,)])
        if name == "journal_mode":
            return Result(["journal_mode"], [("wal" if self.path != ":memory:" else "memory",)])
        if name == "synchronous":
            if stmt.value is not None:
                on = str(stmt.value).upper() not in ("0", "OFF")
                if hasattr(self.pager.backend, "sync"):
                    self.pager.backend.sync = on  # type: ignore[union-attr]
                return Result()
            sync = getattr(self.pager.backend, "sync", False)
            return Result(["synchronous"], [(2 if sync else 0,)])
        if name == "wal_checkpoint":
            frames = self.pager.backend.wal_frames()
            if self.in_transaction:
                return Result(["busy", "log", "checkpointed"], [(1, frames, 0)])
            self.pager.checkpoint()
            return Result(["busy", "log", "checkpointed"], [(0, frames, frames)])
        if name == "foreign_keys":
            return Result(["foreign_keys"], [(0,)])
        if name == "cache_size":
            if stmt.value is not None:
                self.pager.cache_size = max(16, abs(int(stmt.value)))  # type: ignore[arg-type]
                return Result()
            return Result(["cache_size"], [(self.pager.cache_size,)])
        if name == "database_list":
            return Result(["seq", "name", "file"], [(0, "main", "" if self.path == ":memory:" else os.path.abspath(self.path))])
        if name == "table_list":
            rows = [("main", t.name, "table", len(t.columns), 0, 0) for t in self.catalog.tables.values()]
            rows += [("main", v.name, "view", 0, 0, 0) for v in self.catalog.views.values()]
            return Result(["schema", "name", "type", "ncol", "wr", "strict"], rows)
        if name == "function_list":
            from .functions import AGGREGATES, SCALARS, WINDOW_ONLY

            rows = [(n, "s") for n in sorted(SCALARS)] + [(n, "a") for n in sorted(AGGREGATES)] + [(n, "w") for n in sorted(WINDOW_ONLY)]
            return Result(["name", "type"], rows)
        raise OperationalError(f"unknown pragma: {name}")

    def _write_header(self, change: Any) -> None:
        def apply() -> Result:
            self.pager.mark_dirty(0)
            change(self.pager.header)
            return Result()

        if self.in_transaction:
            apply()
        else:
            self.pager.begin()
            try:
                apply()
            except BaseException:
                self.pager.rollback()
                raise
            self.pager.commit()

    # ------------------------------------------------------------ maintenance

    def integrity_check(self) -> list[str]:
        problems: list[str] = []
        pages: set[int] = {0}
        trees: list[tuple[str, int]] = [("schema", self.pager.header.schema_root)]
        for t in self.catalog.tables.values():
            trees.append((f"table {t.name}", t.root))
            for ix in t.indexes:
                trees.append((f"index {ix.name}", ix.root))
        for label, root in trees:
            tree = BTree(self.pager, root)
            for p in tree.check():
                problems.append(f"{label}: {p}")
            for pg in tree.pages_seen:
                if pg in pages:
                    problems.append(f"{label}: page {pg} is used twice")
                pages.add(pg)
                node = self.pager.get(pg)
                if node.kind == LEAF:
                    for v in node.values:
                        if isinstance(v, OverflowRef):
                            nxt = v.first
                            while nxt:
                                if nxt in pages:
                                    problems.append(f"{label}: overflow page {nxt} is used twice")
                                    break
                                pages.add(nxt)
                                nxt = self.pager.get(nxt).next
                elif node.kind != INTERIOR:
                    problems.append(f"{label}: page {pg} has the wrong type")
        for t in self.catalog.tables.values():
            reader = table_row_reader(t)
            n = 0
            for k, raw in BTree(self.pager, t.root).scan():
                n += 1
                row = reader(k, raw)
                for ix in t.indexes:
                    key = self._index_key(ix, row[:-1], k[0])
                    if BTree(self.pager, ix.root).get(key) is None:
                        problems.append(f"row {k[0]} of {t.name} is missing from index {ix.name}")
            for ix in t.indexes:
                m = BTree(self.pager, ix.root).count()
                if m != n:
                    problems.append(f"index {ix.name} has {m} entries but table {t.name} has {n} rows")
        free = self.pager.free_pages()
        for pg in free:
            if pg in pages:
                problems.append(f"page {pg} is both in use and on the free list")
            pages.add(pg)
        missing = set(range(self.pager.header.page_count)) - pages
        if missing:
            problems.append(f"{len(missing)} page(s) are neither used nor free, for example page {min(missing)}")
        return problems

    def vacuum(self) -> None:
        """Rebuild the database compactly, dropping free pages."""
        if self.in_transaction:
            raise OperationalError("cannot VACUUM from within a transaction")
        target_path = ":memory:" if self.path == ":memory:" else self.path + ".vacuum"
        if target_path != ":memory:":
            for p in (target_path, target_path + "-wal"):
                if os.path.exists(p):
                    os.remove(p)
        fresh = Database(target_path, self.pager.page_size, self._cache_size, self._sync)
        self.copy_into(fresh)
        if self.path == ":memory:":
            self.pager = fresh.pager
            self.catalog = fresh.catalog
            fresh.closed = True
            return
        fresh.close()
        self.pager.close()
        os.replace(target_path, self.path)
        wal = self.path + "-wal"
        if os.path.exists(wal):
            os.remove(wal)
        self._open()

    def copy_into(self, other: Database) -> None:
        """Copy every table, index, view and row into another (empty) database."""
        other.execute("BEGIN")
        for t in self.catalog.tables.values():
            other.execute(t.sql)
            target = other.catalog.table(t.name)
            assert target is not None
            tree = BTree(other.pager, target.root)
            for k, raw in BTree(self.pager, t.root).scan():
                tree.insert(k, raw)
            for ix in target.indexes:
                other._fill_index(target, ix)
            if t.name.lower() in self.catalog.sequences:
                other.catalog.set_sequence(t.name, self.catalog.sequences[t.name.lower()])
        for ix in self.catalog.indexes.values():
            if ix.sql:
                other.execute(ix.sql)
        for v in self.catalog.views.values():
            other.execute(v.sql)
        other.pager.mark_dirty(0)
        other.pager.header.user_version = self.pager.header.user_version
        other.execute("COMMIT")

    def dump(self) -> Iterator[str]:
        """SQL text that recreates the database."""
        from .functions import f_quote

        yield "BEGIN TRANSACTION;"
        for t in self.catalog.tables.values():
            yield t.sql + ";"
            reader = table_row_reader(t)
            for k, raw in BTree(self.pager, t.root).scan():
                row = reader(k, raw)
                values = list(row[:-1])
                names = [quote_ident(c.name) for c in t.columns]
                if t.rowid_alias is None:
                    names = ["rowid"] + names
                    values = [row[-1]] + values
                yield f"INSERT INTO {quote_ident(t.name)}({', '.join(names)}) VALUES({', '.join(str(f_quote(v)) for v in values)});"
        for ix in self.catalog.indexes.values():
            if ix.sql:
                yield ix.sql + ";"
        for v in self.catalog.views.values():
            yield v.sql + ";"
        yield "COMMIT;"


@dataclass
class _Upsert:
    action: str
    target: str | None
    assignments: list[tuple[int, Any]] = field(default_factory=list)
    where: Any = None

    def matches(self, conflicts: list[tuple[str, int]]) -> bool:
        if self.target is None:
            return True
        return any(desc.lower() == self.target.lower() for desc, _r in conflicts)


def _rowid_name(table: TableInfo) -> str:
    names = [c.name.lower() for c in table.columns]
    for n in ("rowid", "oid", "_rowid_"):
        if n not in names:
            return n
    return "rowid"


def _getter(i: int) -> Any:
    return lambda env: env.row[i]


def _rename_expr(e: A.Expr, old: str, new: str) -> A.Expr:
    """Rename a column inside an expression's stored text (used for CHECK constraints)."""
    from ..sql.parser import parse_expr

    out = []
    pos = 0
    for t in tokenize(e.text):
        if t.kind in ("word", "ident") and str(t.value).lower() == old.lower():
            out.append(e.text[pos : t.pos])
            out.append(quote_ident(new))
            pos = t.end
    out.append(e.text[pos:])
    text = "".join(out)
    node = parse_expr(text)
    node.text = text
    return node


def rows_equal(a: Iterable[tuple], b: Iterable[tuple]) -> bool:
    return [tuple(sort_key(v) for v in r) for r in a] == [tuple(sort_key(v) for v in r) for r in b]
