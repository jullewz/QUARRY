"""The schema: tables, indexes and views, stored as rows of the schema tree on page 1.

Each schema row is ``(type, name, table_name, root_page, sql)``, the same shape as SQLite's ``sqlite_schema``.
On open, Quarry parses the stored SQL to rebuild the in-memory catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import CorruptDatabaseError, OperationalError, SQLError
from ..sql import ast as A
from ..sql.parser import parse_one
from ..storage.btree import BTree
from ..storage.encoding import decode_record, encode_record
from ..storage.pager import Pager
from ..values import INTEGER, affinity_of

ROWID_NAMES = ("rowid", "oid", "_rowid_")


def quote_ident(name: str) -> str:
    plain = name.replace("_", "a").isalnum() and not name[0].isdigit()
    from ..sql.parser import KEYWORDS

    if plain and name.upper() not in KEYWORDS:
        return name
    return '"' + name.replace('"', '""') + '"'


@dataclass
class ColumnInfo:
    name: str
    type_name: str = ""
    affinity: str = "BLOB"
    not_null: bool = False
    default: A.Expr | None = None
    default_sql: str | None = None
    primary_key: bool = False
    collation: str | None = None
    checks: list[A.Expr] = field(default_factory=list)


@dataclass
class IndexInfo:
    name: str
    table: str
    columns: list[str]
    unique: bool
    root: int
    sql: str | None  # None for indexes created by UNIQUE / PRIMARY KEY constraints
    col_idx: list[int] = field(default_factory=list)

    @property
    def auto(self) -> bool:
        return self.sql is None


@dataclass
class TableInfo:
    name: str
    columns: list[ColumnInfo]
    root: int
    sql: str
    rowid_alias: int | None = None  # index of the INTEGER PRIMARY KEY column
    autoincrement: bool = False
    pk_columns: list[str] = field(default_factory=list)
    checks: list[A.Expr] = field(default_factory=list)
    indexes: list[IndexInfo] = field(default_factory=list)
    constraint_uniques: list[list[str]] = field(default_factory=list)

    def column_index(self, name: str) -> int:
        """Index of a column, ``len(columns)`` for the rowid, or -1."""
        lname = name.lower()
        for i, c in enumerate(self.columns):
            if c.name.lower() == lname:
                return i
        if lname in ROWID_NAMES:
            return len(self.columns)
        return -1

    def require_column(self, name: str, sql: str = "", pos: int = -1) -> int:
        i = self.column_index(name)
        if i < 0:
            from .expr import closest

            guess = closest(name, [c.name for c in self.columns])
            raise SQLError(f"table {self.name} has no column named {name}", sql, pos, f"did you mean {guess}?" if guess else "")
        return i


@dataclass
class ViewInfo:
    name: str
    sql: str
    select: A.Select
    columns: list[str] | None


def table_from_ast(stmt: A.CreateTable, root: int, sql: str) -> TableInfo:
    if stmt.without_rowid:
        from ..errors import NotSupported

        raise NotSupported("WITHOUT ROWID tables are not supported", sql, stmt.pos)
    cols: list[ColumnInfo] = []
    seen: set[str] = set()
    pk_cols: list[str] = []
    table = TableInfo(stmt.name, cols, root, sql)
    for cd in stmt.columns:
        if cd.name.lower() in seen:
            raise SQLError(f"duplicate column name: {cd.name}", sql, cd.pos)
        seen.add(cd.name.lower())
        default_sql = cd.default.text if cd.default is not None else None
        ci = ColumnInfo(
            cd.name,
            cd.type_name,
            affinity_of(cd.type_name),
            cd.not_null,
            cd.default,
            default_sql,
            cd.primary_key,
            cd.collation,
            list(cd.check),
        )
        if cd.collation and cd.collation not in ("BINARY", "NOCASE", "RTRIM"):
            raise SQLError(f"no such collation sequence: {cd.collation}", sql, cd.pos)
        cols.append(ci)
        if cd.primary_key:
            pk_cols.append(cd.name)
            if cd.autoincrement:
                table.autoincrement = True
        if cd.unique:
            table.constraint_uniques.append([cd.name])
    for tc in stmt.constraints:
        if tc.kind == "PRIMARY KEY":
            if pk_cols:
                raise SQLError(f'table "{stmt.name}" has more than one primary key', sql, tc.pos)
            pk_cols = list(tc.columns)
        elif tc.kind == "UNIQUE":
            table.constraint_uniques.append(list(tc.columns))
        elif tc.kind == "CHECK" and tc.check is not None:
            table.checks.append(tc.check)
    if len([c for c in stmt.columns if c.primary_key]) > 1:
        raise SQLError(f'table "{stmt.name}" has more than one primary key', sql, stmt.pos)
    for name in pk_cols + [c for u in table.constraint_uniques for c in u]:
        if table.column_index(name) < 0 or table.column_index(name) == len(cols):
            raise SQLError(f"no such column: {name}", sql, stmt.pos)
    table.pk_columns = pk_cols
    if len(pk_cols) == 1:
        i = table.column_index(pk_cols[0])
        col = cols[i]
        if col.type_name.upper() == "INTEGER":
            table.rowid_alias = i
            col.affinity = INTEGER
    if table.autoincrement and table.rowid_alias is None:
        raise SQLError("AUTOINCREMENT is only allowed on an INTEGER PRIMARY KEY", sql, stmt.pos)
    for c in cols:
        if c.primary_key and table.rowid_alias is None:
            pass
    return table


def table_sql(t: TableInfo) -> str:
    """Rebuild a CREATE TABLE statement from the catalog (after ALTER TABLE)."""
    parts = []
    single_pk = len(t.pk_columns) == 1
    for c in t.columns:
        p = quote_ident(c.name)
        if c.type_name:
            p += " " + c.type_name
        if single_pk and c.name.lower() == t.pk_columns[0].lower():
            p += " PRIMARY KEY"
            if t.autoincrement:
                p += " AUTOINCREMENT"
        if c.not_null:
            p += " NOT NULL"
        if c.default_sql is not None:
            p += f" DEFAULT {c.default_sql}" if c.default_sql[:1] != "(" else f" DEFAULT {c.default_sql}"
        if c.collation:
            p += f" COLLATE {c.collation}"
        for chk in c.checks:
            p += f" CHECK ({chk.text})"
        parts.append(p)
    if len(t.pk_columns) > 1:
        parts.append("PRIMARY KEY (" + ", ".join(quote_ident(c) for c in t.pk_columns) + ")")
    for u in t.constraint_uniques:
        parts.append("UNIQUE (" + ", ".join(quote_ident(c) for c in u) + ")")
    for chk in t.checks:
        parts.append(f"CHECK ({chk.text})")
    return f"CREATE TABLE {quote_ident(t.name)} (" + ", ".join(parts) + ")"


def index_sql(ix: IndexInfo) -> str:
    unique = "UNIQUE " if ix.unique else ""
    cols = ", ".join(quote_ident(c) for c in ix.columns)
    return f"CREATE {unique}INDEX {quote_ident(ix.name)} ON {quote_ident(ix.table)} ({cols})"


class Catalog:
    """In-memory view of the schema tree."""

    def __init__(self, pager: Pager) -> None:
        self.pager = pager
        self.tables: dict[str, TableInfo] = {}
        self.views: dict[str, ViewInfo] = {}
        self.indexes: dict[str, IndexInfo] = {}
        self.sequences: dict[str, int] = {}
        self.cookie = -1
        self.load()

    @property
    def tree(self) -> BTree:
        return BTree(self.pager, self.pager.header.schema_root)

    def load(self) -> None:
        self.tables.clear()
        self.views.clear()
        self.indexes.clear()
        self.sequences.clear()
        pending_indexes = []
        for _key, raw in self.tree.scan():
            (kind, name, tbl, root, sql), _ = decode_record(raw)
            try:
                if kind == "table":
                    stmt, _n = parse_one(sql)
                    assert isinstance(stmt, A.CreateTable)
                    self.tables[name.lower()] = table_from_ast(stmt, root, sql)
                elif kind == "index":
                    pending_indexes.append((name, tbl, root, sql))
                elif kind == "view":
                    stmt, _n = parse_one(sql)
                    assert isinstance(stmt, A.CreateView)
                    self.views[name.lower()] = ViewInfo(name, sql, stmt.select, stmt.columns)
                elif kind == "sequence":
                    self.sequences[name.lower()] = root
            except SQLError as e:
                raise CorruptDatabaseError(f"the schema entry for {name!r} cannot be parsed: {e.message}") from e
        for name, tbl, root, sql in pending_indexes:
            table = self.tables.get(tbl.lower())
            if table is None:
                raise CorruptDatabaseError(f"index {name!r} belongs to a missing table {tbl!r}")
            if sql:
                stmt, _n = parse_one(sql)
                assert isinstance(stmt, A.CreateIndex)
                ix = IndexInfo(name, table.name, stmt.columns, stmt.unique, root, sql)
            else:
                ix = self._auto_index_columns(table, name, root)
            self._attach(table, ix)
        self.cookie = self.pager.header.schema_cookie

    def _auto_index_columns(self, table: TableInfo, name: str, root: int) -> IndexInfo:
        # name format: quarry_autoindex_<table>_<n>
        n = int(name.rsplit("_", 1)[1])
        groups = self.constraint_groups(table)
        cols = groups[n - 1]
        return IndexInfo(name, table.name, cols, True, root, None)

    @staticmethod
    def constraint_groups(table: TableInfo) -> list[list[str]]:
        """Column lists that need an automatic unique index, in a stable order."""
        groups = []
        if table.pk_columns and table.rowid_alias is None:
            groups.append(table.pk_columns)
        for u in table.constraint_uniques:
            if not (table.rowid_alias is not None and len(u) == 1 and u[0].lower() == table.columns[table.rowid_alias].name.lower()):
                groups.append(u)
        return groups

    def _attach(self, table: TableInfo, ix: IndexInfo) -> None:
        ix.col_idx = [table.require_column(c) for c in ix.columns]
        table.indexes.append(ix)
        self.indexes[ix.name.lower()] = ix

    def refresh(self) -> None:
        """Reload when another statement (or a rollback) changed the schema."""
        if self.pager.header.schema_cookie != self.cookie:
            self.load()

    # ------------------------------------------------------------ lookups

    def table(self, name: str) -> TableInfo | None:
        return self.tables.get(name.lower())

    def exists(self, name: str) -> str | None:
        key = name.lower()
        if key in self.tables:
            return "table"
        if key in self.views:
            return "view"
        if key in self.indexes:
            return "index"
        return None

    # ------------------------------------------------------------ changes

    def _bump(self) -> None:
        self.pager.mark_dirty(0)
        self.pager.header.schema_cookie += 1
        self.cookie = self.pager.header.schema_cookie

    def _rows(self) -> list[tuple[tuple, tuple]]:
        return [(k, decode_record(v)[0]) for k, v in self.tree.scan()]

    def _add_row(self, kind: str, name: str, tbl: str, root: int, sql: str | None) -> None:
        last = self.tree.last_key()
        rid = (last[0] + 1) if last else 1
        self.tree.insert((rid,), encode_record((kind, name, tbl, root, sql)))

    def _delete_rows(self, predicate) -> None:  # type: ignore[no-untyped-def]
        for key, row in self._rows():
            if predicate(row):
                self.tree.delete(key)

    def _update_rows(self, fn) -> None:  # type: ignore[no-untyped-def]
        for key, row in self._rows():
            new = fn(row)
            if new is not None and new != row:
                self.tree.insert(key, encode_record(new))

    def add_table(self, table: TableInfo) -> None:
        self._add_row("table", table.name, table.name, table.root, table.sql)
        self.tables[table.name.lower()] = table
        self._bump()

    def add_index(self, table: TableInfo, ix: IndexInfo) -> None:
        self._add_row("index", ix.name, table.name, ix.root, ix.sql)
        self._attach(table, ix)
        self._bump()

    def add_view(self, view: ViewInfo) -> None:
        self._add_row("view", view.name, view.name, 0, view.sql)
        self.views[view.name.lower()] = view
        self._bump()

    def drop_table(self, table: TableInfo) -> None:
        key = table.name.lower()
        self._delete_rows(lambda r: (r[0] in ("table", "index") and r[2].lower() == key) or (r[0] == "sequence" and r[1].lower() == key))
        for ix in table.indexes:
            self.indexes.pop(ix.name.lower(), None)
        self.tables.pop(key, None)
        self.sequences.pop(key, None)
        self._bump()

    def drop_index(self, ix: IndexInfo) -> None:
        key = ix.name.lower()
        self._delete_rows(lambda r: r[0] == "index" and r[1].lower() == key)
        table = self.tables[ix.table.lower()]
        table.indexes = [i for i in table.indexes if i.name.lower() != key]
        self.indexes.pop(key, None)
        self._bump()

    def drop_view(self, view: ViewInfo) -> None:
        key = view.name.lower()
        self._delete_rows(lambda r: r[0] == "view" and r[1].lower() == key)
        self.views.pop(key, None)
        self._bump()

    def rewrite_table(self, old_name: str, table: TableInfo) -> None:
        """Store a changed table definition (and its indexes' SQL) after ALTER TABLE."""
        old = old_name.lower()
        table.sql = table_sql(table)

        def fix(row: tuple) -> tuple | None:
            kind, name, tbl, root, sql = row
            if kind == "table" and name.lower() == old:
                return ("table", table.name, table.name, table.root, table.sql)
            if kind == "index" and tbl.lower() == old:
                ix = self.indexes[name.lower()]
                return ("index", ix.name, table.name, ix.root, index_sql(ix) if ix.sql else None)
            if kind == "sequence" and name.lower() == old:
                return ("sequence", table.name, table.name, root, None)
            return None

        for ix in table.indexes:
            ix.table = table.name
            if ix.sql:
                ix.sql = index_sql(ix)
        self._update_rows(fix)
        self.tables.pop(old, None)
        self.tables[table.name.lower()] = table
        if old in self.sequences:
            self.sequences[table.name.lower()] = self.sequences.pop(old)
        self._bump()

    def set_sequence(self, table: str, value: int) -> None:
        key = table.lower()
        if key in self.sequences:
            self._update_rows(lambda r: ("sequence", r[1], r[2], value, None) if r[0] == "sequence" and r[1].lower() == key else None)
        else:
            self._add_row("sequence", self.tables[key].name, self.tables[key].name, value, None)
        self.sequences[key] = value

    def schema_rows(self) -> list[tuple]:
        """Rows of the quarry_schema table (and its alias sqlite_schema)."""
        return [row for _k, row in self._rows() if row[0] != "sequence"]


def check_name_free(catalog: Catalog, name: str, sql: str, pos: int) -> None:
    kind = catalog.exists(name)
    if kind:
        raise OperationalError(f"{kind} {name} already exists")
    if name.lower().startswith(("sqlite_", "quarry_")):
        raise SQLError(f"object name reserved for internal use: {name}", sql, pos)
