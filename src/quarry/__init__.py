"""Quarry: a relational database engine written from scratch in pure Python.

Created by jullewz. Quarry stores tables in B+trees on disk, protects every commit with a write-ahead log,
and runs a large part of SQLite's SQL dialect: joins, subqueries, CTEs (recursive too), window functions,
upserts, views and indexes.

>>> import quarry
>>> db = quarry.connect(":memory:")
>>> _ = db.execute("CREATE TABLE fruit (name TEXT, price REAL)")
>>> _ = db.executemany("INSERT INTO fruit VALUES (?, ?)", [("apple", 0.5), ("mango", 1.75)])
>>> db.execute("SELECT name FROM fruit WHERE price > 1").fetchall()
[('mango',)]
"""

__version__ = "0.1.0"

from .dbapi import Connection, Cursor, Row, apilevel, connect, paramstyle, threadsafety  # noqa: E402
from .engine.database import Database  # noqa: E402
from .errors import (  # noqa: E402
    CorruptDatabaseError,
    DatabaseError,
    DataError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    SQLError,
    Warning,
)

__all__ = [
    "__version__",
    "connect",
    "Connection",
    "Cursor",
    "Row",
    "Database",
    "apilevel",
    "paramstyle",
    "threadsafety",
    "Error",
    "Warning",
    "InterfaceError",
    "DatabaseError",
    "DataError",
    "OperationalError",
    "IntegrityError",
    "InternalError",
    "ProgrammingError",
    "NotSupportedError",
    "CorruptDatabaseError",
    "SQLError",
]
