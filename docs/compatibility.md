# Compatibility with SQLite

Quarry aims to give the same answer as SQLite for every query it accepts. The test suite checks this on several
hundred queries, and a fuzzer checked thousands more during development. This page lists what is missing and where
answers can differ.

## Not supported yet

Quarry raises `NotSupportedError` (a subclass of `quarry.DatabaseError`) with a hint for:

- `RIGHT JOIN` and `FULL JOIN` (swap the tables and use `LEFT JOIN`)
- triggers, virtual tables, table-valued functions
- partial indexes (`CREATE INDEX ... WHERE`), expression indexes, and indexes with a collation other than `BINARY`
- generated columns, `WITHOUT ROWID` tables, `STRICT` tables
- row values such as `(a, b) = (1, 2)` and multi-column `SET (a, b) = ...`
- `UPDATE ... FROM`
- `ORDER BY` inside an aggregate call, `GROUPS` frames, `EXCLUDE` in frames, `RANGE` frames with numeric offsets
- JSON functions and the `->` / `->>` operators, `REGEXP`, `MATCH`
- `ATTACH DATABASE`

Foreign key clauses parse but are not enforced. That matches SQLite with `PRAGMA foreign_keys` off, its default.

## Known differences

**REAL to TEXT.** When a REAL turns into text (`CAST(x AS TEXT)`, `||`, `quote()`, `printf('%s')`), Quarry uses the
shortest form among 15 or 17 significant digits that reads back as the same number. SQLite's own digit generation
sometimes produces a different 16th or 17th digit, for example `0.33333333333333332` where Quarry prints
`0.3333333333333333`. Older SQLite versions print 15 digits. Both forms read back as the same number.

**Huge REALs in `%`.** For REAL operands at or beyond 2⁶³, `%` depends on how SQLite's C code converts doubles to
integers, which C leaves undefined. Quarry clamps to the 64-bit range.

**Ties.** Where SQL leaves order undefined, the engines can differ: the order of rows without `ORDER BY`, the order
of rows with equal sort keys, and which spelling a `NOCASE` group or `DISTINCT` shows when several spellings fall
into it. Quarry outputs `GROUP BY` groups sorted by key, which matches what SQLite usually does.

**Errors.** Quarry's error messages are its own. They name the same problem as SQLite's, with a position and often
a hint. Exception classes follow PEP 249: constraint failures raise `IntegrityError`, bad SQL raises
`ProgrammingError` (as `SQLError`), and file or transaction problems raise `OperationalError`.

**Concurrency.** SQLite lets several processes read while one writes. Quarry allows one connection per file.

**File format.** Quarry files are not SQLite files, and the two cannot open each other's databases. Use `.dump` in
the Quarry shell, or `iterdump()` in Python, to move data in SQL form.

## Version notes

Some answers changed between SQLite releases. Examples are `LIKE` on blobs that contain NUL bytes, and the sign of
some zero results. Quarry follows SQLite 3.53. The comparison tests avoid those cases so they pass against any
recent SQLite.
