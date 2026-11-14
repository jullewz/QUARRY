"""A PEP 249 (DB-API 2.0) interface, shaped like the standard ``sqlite3`` module.

>>> import quarry
>>> con = quarry.connect(":memory:")
>>> con.execute("CREATE TABLE t (x)")  # doctest: +ELLIPSIS
<quarry.dbapi.Cursor object at ...>
>>> con.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])  # doctest: +ELLIPSIS
<quarry.dbapi.Cursor object at ...>
>>> con.execute("SELECT sum(x) FROM t").fetchone()
(3,)
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from .engine.database import Database, Result
from .errors import InterfaceError, ProgrammingError
from .sql import ast as A

apilevel = "2.0"
threadsafety = 1
paramstyle = "qmark"  # named styles (:name, @name, $name) work too


class Cursor:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.arraysize = 1
        self._rows: list[tuple] = []
        self._pos = 0
        self.description: tuple | None = None
        self.rowcount = -1
        self.lastrowid: int | None = None
        self.closed = False

    def _check(self) -> Database:
        if self.closed:
            raise InterfaceError("the cursor is closed")
        return self.connection._db()

    def execute(self, sql: str, parameters: Sequence[Any] | dict[str, Any] = ()) -> Cursor:
        db = self._check()
        self.connection._autobegin(sql)
        result = db.execute(sql, parameters)
        self._load(result, db)
        return self

    def executemany(self, sql: str, seq_of_parameters: Iterable[Sequence[Any] | dict[str, Any]]) -> Cursor:
        db = self._check()
        prepared = db.prepare(sql)
        if not isinstance(prepared.stmt, (A.Insert, A.Update, A.Delete)):
            raise ProgrammingError("executemany() is for INSERT, UPDATE and DELETE statements")
        self.connection._autobegin(sql)
        total = 0
        own_txn = not db.in_transaction
        if own_txn:
            db.execute("BEGIN")
        try:
            for params in seq_of_parameters:
                result = db.run(prepared.stmt, db.bind(prepared, params), sql)
                total += max(result.rowcount, 0)
        except BaseException:
            if own_txn:
                db.rollback()
            raise
        if own_txn:
            db.commit()
        self._rows, self._pos = [], 0
        self.description = None
        self.rowcount = total
        self.lastrowid = db.last_rowid
        return self

    def executescript(self, sql: str) -> Cursor:
        db = self._check()
        db.executescript(sql)
        self._rows, self._pos, self.description = [], 0, None
        return self

    def _load(self, result: Result, db: Database) -> None:
        self._rows = result.rows
        self._pos = 0
        if result.columns is not None:
            self.description = tuple((c, None, None, None, None, None, None) for c in result.columns)
        else:
            self.description = None
        self.rowcount = result.rowcount
        if result.lastrowid is not None:
            self.lastrowid = result.lastrowid

    def fetchone(self) -> tuple | None:
        self._check()
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return self.connection._make_row(self, row)

    def fetchmany(self, size: int | None = None) -> list:
        n = self.arraysize if size is None else size
        out = []
        for _ in range(n):
            row = self.fetchone()
            if row is None:
                break
            out.append(row)
        return out

    def fetchall(self) -> list:
        self._check()
        rows = self._rows[self._pos :]
        self._pos = len(self._rows)
        return [self.connection._make_row(self, r) for r in rows]

    def __iter__(self) -> Iterator:
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def close(self) -> None:
        self.closed = True

    def setinputsizes(self, sizes: Any) -> None:
        pass

    def setoutputsize(self, size: Any, column: Any = None) -> None:
        pass


class Row(tuple):
    """A result row that also allows access by column name: ``row["name"]``."""

    _names: tuple[str, ...]

    def __new__(cls, names: tuple[str, ...], values: tuple) -> Row:
        obj = super().__new__(cls, values)
        obj._names = names
        return obj

    def __getitem__(self, key: Any) -> Any:  # type: ignore[override]
        if isinstance(key, str):
            lower = key.lower()
            for i, n in enumerate(self._names):
                if n.lower() == lower:
                    return tuple.__getitem__(self, i)
            raise IndexError(f"no column named {key!r}")
        return tuple.__getitem__(self, key)

    def keys(self) -> list[str]:
        return list(self._names)


class Connection:
    """A connection to a Quarry database.

    Like ``sqlite3``, a write statement opens a transaction implicitly when ``autocommit`` is False (the default),
    and ``commit()`` ends it. With ``autocommit=True``, each statement commits on its own unless you run BEGIN.
    """

    def __init__(
        self,
        database: str | os.PathLike[str] = ":memory:",
        *,
        autocommit: bool = False,
        page_size: int = 4096,
        cache_size: int = 2000,
        sync: bool = True,
    ) -> None:
        self._database: Database | None = Database(database, page_size=page_size, cache_size=cache_size, sync=sync)
        self.autocommit = autocommit
        self.row_factory: Any = None

    def _db(self) -> Database:
        if self._database is None:
            raise InterfaceError("cannot operate on a closed database")
        return self._database

    @property
    def database(self) -> Database:
        """The underlying engine object."""
        return self._db()

    def _autobegin(self, sql: str) -> None:
        if self.autocommit:
            return
        db = self._db()
        if db.in_transaction:
            return
        stmt = db.prepare(sql).stmt
        if isinstance(stmt, (A.Insert, A.Update, A.Delete)):
            db.execute("BEGIN")

    def _make_row(self, cursor: Cursor, row: tuple) -> Any:
        if self.row_factory is None:
            return row
        if self.row_factory is Row:
            names = tuple(d[0] for d in cursor.description or ())
            return Row(names, row)
        return self.row_factory(cursor, row)

    @property
    def in_transaction(self) -> bool:
        return self._db().in_transaction

    @property
    def total_changes(self) -> int:
        return self._db().total_changes

    def cursor(self) -> Cursor:
        self._db()
        return Cursor(self)

    def execute(self, sql: str, parameters: Sequence[Any] | dict[str, Any] = ()) -> Cursor:
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Iterable[Sequence[Any] | dict[str, Any]]) -> Cursor:
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, sql: str) -> Cursor:
        return self.cursor().executescript(sql)

    def commit(self) -> None:
        self._db().commit()

    def rollback(self) -> None:
        self._db().rollback()

    def close(self) -> None:
        if self._database is not None:
            if not self.autocommit and self._database.in_transaction:
                self._database.rollback()
            self._database.close()
            self._database = None

    def iterdump(self) -> Iterator[str]:
        return self._db().dump()

    def backup(self, target: Connection) -> None:
        """Copy this database into another, empty connection."""
        self._db().copy_into(target._db())

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


def connect(database: str | os.PathLike[str] = ":memory:", **kwargs: Any) -> Connection:
    """Open a database file (created if missing), or ``":memory:"`` for a temporary one."""
    return Connection(database, **kwargs)
