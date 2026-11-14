# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-15

First public release.

### Storage

- Single-file databases made of fixed-size pages (512 to 65,536 bytes, 4,096 by default)
- B+trees for tables (keyed by rowid) and indexes (keyed by values and rowid), with overflow pages for large values
  and a free list for reuse
- Write-ahead log with chained CRC-32 checksums, commit markers, recovery on open and automatic checkpoints
- Transactions, nested savepoints and per-statement atomicity
- `VACUUM`, `PRAGMA integrity_check` with page accounting and index cross checks
- One connection per file, enforced with a file lock on Linux, macOS and Windows

### SQL

- SQLite's dynamic types, affinity, comparison, collation and NULL rules
- `SELECT` with joins (inner, left, cross, `USING`, `NATURAL`), subqueries, `WITH` and `WITH RECURSIVE`,
  compound queries, `GROUP BY`/`HAVING`, window functions and named windows, `ORDER BY ... NULLS FIRST/LAST`,
  `LIMIT`/`OFFSET`
- `INSERT`/`UPDATE`/`DELETE` with `RETURNING`, `OR IGNORE/REPLACE`, and upserts (`ON CONFLICT DO NOTHING/UPDATE`)
- `CREATE/DROP TABLE`, `CREATE TABLE AS`, indexes, views, `ALTER TABLE` (rename, rename column, add column,
  drop column)
- Constraints: primary keys, `INTEGER PRIMARY KEY` rowid aliases, `AUTOINCREMENT`, `NOT NULL`, `UNIQUE`, `CHECK`,
  `DEFAULT`
- 66 scalar functions, 8 aggregates, 11 window functions, and SQLite's date and time functions
- A query planner using rowid lookups, index equality and range scans, and hash joins, shown by
  `EXPLAIN QUERY PLAN`
- Error messages with the position in the statement and "did you mean" hints

### Tools

- PEP 249 Python API shaped like `sqlite3`, with `quarry.Row`, `iterdump()` and `backup()`
- `quarry` shell with box, table, markdown, csv, tsv, json, line and list output, `.import`, `.export`,
  `.dump`, `.plan`, `.check` and `.stats`
- Benchmark script, kill-the-writer crash test, and screenshot generator

[0.1.0]: https://github.com/jullewz/quarry/releases/tag/v0.1.0
