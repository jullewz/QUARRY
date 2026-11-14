"""Syntax tree for Quarry's SQL dialect."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

# ---------------------------------------------------------------- expressions


@dataclass(eq=False)
class Node:
    pos: int = field(default=-1, kw_only=True, compare=False)
    text: str = field(default="", kw_only=True, compare=False)  # source text, for result column names


@dataclass(eq=False)
class Literal(Node):
    value: Any


@dataclass(eq=False)
class Column(Node):
    table: str | None
    name: str
    quoted: bool = False  # written as "name": SQLite treats it as a string if no column matches


@dataclass(eq=False)
class Param(Node):
    index: int  # 1-based position for ?, ?NNN and named parameters
    name: str | None = None


@dataclass(eq=False)
class Unary(Node):
    op: str  # '-', '+', '~', 'NOT'
    operand: Expr


@dataclass(eq=False)
class Binary(Node):
    op: str
    left: Expr
    right: Expr


@dataclass(eq=False)
class Like(Node):
    op: str  # LIKE or GLOB
    negated: bool
    left: Expr
    pattern: Expr
    escape: Expr | None = None


@dataclass(eq=False)
class Between(Node):
    negated: bool
    expr: Expr
    low: Expr
    high: Expr


@dataclass(eq=False)
class InList(Node):
    negated: bool
    expr: Expr
    items: list[Expr]


@dataclass(eq=False)
class InSelect(Node):
    negated: bool
    expr: Expr
    select: Select


@dataclass(eq=False)
class IsNull(Node):
    negated: bool
    expr: Expr


@dataclass(eq=False)
class Case(Node):
    operand: Expr | None
    whens: list[tuple[Expr, Expr]]
    otherwise: Expr | None


@dataclass(eq=False)
class Cast(Node):
    expr: Expr
    type_name: str


@dataclass(eq=False)
class Collate(Node):
    expr: Expr
    collation: str


@dataclass(eq=False)
class WindowSpec(Node):
    partition_by: list[Expr]
    order_by: list[OrderItem]
    frame: tuple[str, Any, Any] | None = None  # ('ROWS'|'RANGE', start, end)
    base: str | None = None  # a named window this one extends


@dataclass(eq=False)
class Func(Node):
    name: str  # lower case
    args: list[Expr]
    distinct: bool = False
    star: bool = False  # count(*)
    filter: Expr | None = None
    over: WindowSpec | str | None = None


@dataclass(eq=False)
class Subquery(Node):
    select: Select


@dataclass(eq=False)
class Exists(Node):
    select: Select


Expr = Union[Literal, Column, Param, Unary, Binary, Like, Between, InList, InSelect, IsNull, Case, Cast, Collate, Func, Subquery, Exists]

# ---------------------------------------------------------------- queries


@dataclass(eq=False)
class ResultColumn(Node):
    expr: Expr | None  # None for * and t.*
    alias: str | None = None
    star_table: str | None = None  # for t.*
    star: bool = False


@dataclass(eq=False)
class TableRef(Node):
    name: str
    alias: str | None = None


@dataclass(eq=False)
class SubqueryRef(Node):
    select: Select
    alias: str | None = None


@dataclass(eq=False)
class Join(Node):
    kind: str  # INNER, LEFT, CROSS
    left: FromItem
    right: FromItem
    on: Expr | None = None
    using: list[str] | None = None
    natural: bool = False


FromItem = Union[TableRef, SubqueryRef, Join]


@dataclass(eq=False)
class OrderItem(Node):
    expr: Expr
    descending: bool = False
    nulls_first: bool | None = None


@dataclass(eq=False)
class SelectCore(Node):
    columns: list[ResultColumn]
    from_: FromItem | None = None
    where: Expr | None = None
    group_by: list[Expr] = field(default_factory=list)
    having: Expr | None = None
    distinct: bool = False
    windows: dict[str, WindowSpec] = field(default_factory=dict)


@dataclass(eq=False)
class Values(Node):
    rows: list[list[Expr]]


@dataclass(eq=False)
class CTE(Node):
    name: str
    columns: list[str] | None
    select: Select


@dataclass(eq=False)
class Select(Node):
    cores: list[SelectCore | Values]
    ops: list[str] = field(default_factory=list)  # UNION, UNION ALL, INTERSECT, EXCEPT between cores
    order_by: list[OrderItem] = field(default_factory=list)
    limit: Expr | None = None
    offset: Expr | None = None
    ctes: list[CTE] = field(default_factory=list)
    recursive: bool = False


# ---------------------------------------------------------------- statements


@dataclass(eq=False)
class Upsert(Node):
    target: list[str] | None
    action: str  # NOTHING or UPDATE
    assignments: list[tuple[list[str], Expr]] = field(default_factory=list)
    where: Expr | None = None


@dataclass(eq=False)
class Insert(Node):
    table: str
    columns: list[str] | None
    source: Select | None  # None means DEFAULT VALUES
    on_conflict: str = "ABORT"  # ABORT, REPLACE, IGNORE, FAIL, ROLLBACK
    upsert: Upsert | None = None
    returning: list[ResultColumn] | None = None
    alias: str | None = None


@dataclass(eq=False)
class Update(Node):
    table: str
    assignments: list[tuple[list[str], Expr]]
    where: Expr | None = None
    on_conflict: str = "ABORT"
    returning: list[ResultColumn] | None = None
    alias: str | None = None
    from_: FromItem | None = None


@dataclass(eq=False)
class Delete(Node):
    table: str
    where: Expr | None = None
    returning: list[ResultColumn] | None = None
    alias: str | None = None


@dataclass(eq=False)
class ColumnDef(Node):
    name: str
    type_name: str = ""
    primary_key: bool = False
    pk_desc: bool = False
    autoincrement: bool = False
    not_null: bool = False
    unique: bool = False
    default: Expr | None = None
    check: list[Expr] = field(default_factory=list)
    collation: str | None = None
    references: tuple[str, list[str]] | None = None


@dataclass(eq=False)
class TableConstraint(Node):
    kind: str  # PRIMARY KEY, UNIQUE, CHECK, FOREIGN KEY
    columns: list[str] = field(default_factory=list)
    check: Expr | None = None
    name: str | None = None
    references: tuple[str, list[str]] | None = None


@dataclass(eq=False)
class CreateTable(Node):
    name: str
    columns: list[ColumnDef]
    constraints: list[TableConstraint] = field(default_factory=list)
    if_not_exists: bool = False
    as_select: Select | None = None
    without_rowid: bool = False


@dataclass(eq=False)
class CreateIndex(Node):
    name: str
    table: str
    columns: list[str]
    unique: bool = False
    if_not_exists: bool = False
    where: Expr | None = None


@dataclass(eq=False)
class CreateView(Node):
    name: str
    columns: list[str] | None
    select: Select
    if_not_exists: bool = False


@dataclass(eq=False)
class Drop(Node):
    kind: str  # TABLE, INDEX, VIEW
    name: str
    if_exists: bool = False


@dataclass(eq=False)
class AlterTable(Node):
    table: str
    action: str  # RENAME, RENAME COLUMN, ADD COLUMN, DROP COLUMN
    new_name: str | None = None
    old_column: str | None = None
    column: ColumnDef | None = None


@dataclass(eq=False)
class Transaction(Node):
    action: str  # BEGIN, COMMIT, ROLLBACK, SAVEPOINT, RELEASE
    savepoint: str | None = None
    mode: str | None = None  # DEFERRED, IMMEDIATE, EXCLUSIVE


@dataclass(eq=False)
class Explain(Node):
    statement: Statement
    query_plan: bool = True


@dataclass(eq=False)
class Pragma(Node):
    name: str
    value: Any = None
    argument: str | None = None  # pragma name(argument)


@dataclass(eq=False)
class Vacuum(Node):
    pass


@dataclass(eq=False)
class Analyze(Node):
    pass


Statement = Union[
    Select, Insert, Update, Delete, CreateTable, CreateIndex, CreateView, Drop, AlterTable, Transaction, Explain, Pragma, Vacuum, Analyze
]
