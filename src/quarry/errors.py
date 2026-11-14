"""Exceptions raised by Quarry.

The names follow PEP 249 (the Python database API), so code written for ``sqlite3`` catches the same kinds of
errors. Every error carries a plain-English message. SQL errors also carry the position in the statement and,
where possible, a hint.
"""

from __future__ import annotations


class Warning(Exception):  # noqa: A001 - PEP 249 name
    """Important warnings (PEP 249). Quarry does not raise it today."""


class Error(Exception):
    """Base class of every Quarry error."""


class InterfaceError(Error):
    """The API was used wrongly, for example a closed connection."""


class DatabaseError(Error):
    """An error that concerns the database."""


class DataError(DatabaseError):
    """A value is out of range or has the wrong type."""


class OperationalError(DatabaseError):
    """A problem with the database file, a transaction or a missing object."""


class IntegrityError(DatabaseError):
    """A constraint failed: UNIQUE, NOT NULL, CHECK or PRIMARY KEY."""


class InternalError(DatabaseError):
    """Quarry found an internal inconsistency. Please report it."""


class ProgrammingError(DatabaseError):
    """The SQL is wrong: a syntax error, an unknown column, a wrong number of parameters."""


class NotSupportedError(DatabaseError):
    """The SQL is valid but Quarry does not support this feature yet."""


class CorruptDatabaseError(DatabaseError):
    """The database file is damaged or is not a Quarry database."""


class SQLError(ProgrammingError):
    """A SQL error with a position in the statement text."""

    def __init__(self, message: str, sql: str = "", pos: int = -1, hint: str = "") -> None:
        self.message = message
        self.sql = sql
        self.pos = pos
        self.hint = hint
        super().__init__(self.render())

    def render(self) -> str:
        text = self.message
        if self.sql and 0 <= self.pos <= len(self.sql):
            line_start = self.sql.rfind("\n", 0, self.pos) + 1
            line_end = self.sql.find("\n", self.pos)
            if line_end < 0:
                line_end = len(self.sql)
            line = self.sql[line_start:line_end]
            line_no = self.sql.count("\n", 0, self.pos) + 1
            col = self.pos - line_start
            text += f"\n  at line {line_no}, column {col + 1}:\n    {line}\n    {' ' * col}^"
        if self.hint:
            text += f"\n  hint: {self.hint}"
        return text


class NotSupported(SQLError, NotSupportedError):
    """Valid SQL that Quarry does not implement yet."""
