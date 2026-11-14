# SQL reference

Quarry's dialect is SQLite's. This page lists what Quarry implements. For the meaning of each feature, SQLite's
documentation applies, except for the differences listed in [compatibility.md](compatibility.md).

## Statements

```sql
SELECT [DISTINCT] columns FROM ... [WHERE ...] [GROUP BY ...] [HAVING ...] [WINDOW name AS (...)]
       [UNION | UNION ALL | INTERSECT | EXCEPT SELECT ...]
       [ORDER BY expr [ASC|DESC] [NULLS FIRST|LAST], ...] [LIMIT n [OFFSET m]]
WITH [RECURSIVE] name [(columns)] AS (SELECT ...), ... SELECT ...
VALUES (...), (...)

INSERT [OR ABORT|FAIL|IGNORE|REPLACE|ROLLBACK] INTO table [(columns)]
       VALUES (...), ... | SELECT ... | DEFAULT VALUES
       [ON CONFLICT [(columns)] DO NOTHING | DO UPDATE SET col = expr, ... [WHERE ...]]
       [RETURNING expr, ...]
REPLACE INTO table ...
UPDATE [OR ...] table [AS alias] SET col = expr, ... [WHERE ...] [RETURNING ...]
DELETE FROM table [AS alias] [WHERE ...] [RETURNING ...]

CREATE TABLE [IF NOT EXISTS] name (column-definitions, table-constraints)
CREATE TABLE name AS SELECT ...
CREATE [UNIQUE] INDEX [IF NOT EXISTS] name ON table (columns)
CREATE VIEW [IF NOT EXISTS] name [(columns)] AS SELECT ...
DROP TABLE | INDEX | VIEW [IF EXISTS] name
ALTER TABLE name RENAME TO new_name
ALTER TABLE name RENAME [COLUMN] old TO new
ALTER TABLE name ADD [COLUMN] column-definition
ALTER TABLE name DROP [COLUMN] column

BEGIN [DEFERRED|IMMEDIATE|EXCLUSIVE] [TRANSACTION]
COMMIT | END [TRANSACTION]
ROLLBACK [TRANSACTION] [TO [SAVEPOINT] name]
SAVEPOINT name
RELEASE [SAVEPOINT] name

EXPLAIN [QUERY PLAN] statement
PRAGMA name [= value | (argument)]
VACUUM
ANALYZE           -- accepted, does nothing
```

Column definitions take a type name and any of: `PRIMARY KEY [ASC|DESC] [AUTOINCREMENT]`, `NOT NULL`, `NULL`,
`UNIQUE`, `DEFAULT value`, `DEFAULT (expression)`, `CHECK (expression)`, `COLLATE name` and
`REFERENCES table (columns) ...`. Table constraints are `PRIMARY KEY (...)`, `UNIQUE (...)`, `CHECK (...)` and
`FOREIGN KEY (...) REFERENCES ...`, each optionally named with `CONSTRAINT name`. Foreign keys parse but are not
enforced, which matches SQLite's default.

A column declared exactly `INTEGER PRIMARY KEY` becomes the rowid. Every table also has a hidden `rowid`, also
reachable as `oid` and `_rowid_` unless a column has that name.

## Expressions

| Kind | Syntax |
| --- | --- |
| literals | `42`, `-7`, `3.5`, `1e3`, `0x1F`, `'text'`, `x'00ff'`, `NULL`, `TRUE`, `FALSE`, `CURRENT_DATE`, `CURRENT_TIME`, `CURRENT_TIMESTAMP` |
| columns | `name`, `table.name`, `alias.name`; a result alias works in `WHERE`, `GROUP BY`, `HAVING` and `ORDER BY` |
| parameters | `?`, `?NNN`, `:name`, `@name`, `$name` |
| arithmetic | `+ - * / %`, unary `-` and `+` |
| text | `\|\|`, `LIKE pattern [ESCAPE c]`, `GLOB pattern`, `COLLATE BINARY \| NOCASE \| RTRIM` |
| bits | `& \| << >> ~` |
| comparison | `= == != <> < <= > >=`, `IS`, `IS NOT`, `IS [NOT] DISTINCT FROM`, `ISNULL`, `NOTNULL`, `NOT NULL` |
| logic | `AND`, `OR`, `NOT` with SQL's three-valued logic |
| ranges and sets | `BETWEEN a AND b`, `IN (list)`, `IN (SELECT ...)`, `IN table`, `EXISTS (SELECT ...)` |
| subqueries | `(SELECT ...)` anywhere an expression goes, correlated or not |
| conditionals | `CASE WHEN ... THEN ... ELSE ... END`, `CASE x WHEN ...`, `iif(c, a, b)` |
| conversion | `CAST(expr AS type)` |
| windows | `f(...) [FILTER (WHERE ...)] OVER (PARTITION BY ... ORDER BY ... ROWS BETWEEN ... AND ...)` or `OVER name` |

A double-quoted word that matches no column is read as a string, as SQLite does for compatibility.

## Functions

**Text:** `length`, `octet_length`, `lower`, `upper`, `substr`/`substring`, `trim`, `ltrim`, `rtrim`, `replace`,
`instr`, `concat`, `concat_ws`, `printf`/`format`, `quote`, `hex`, `unhex`, `char`, `unicode`, `like`, `glob`

**Numbers:** `abs`, `round`, `sign`, `ceil`/`ceiling`, `floor`, `trunc`, `mod`, `pow`/`power`, `sqrt`, `exp`, `ln`,
`log`, `log2`, `log10`, `pi`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `sinh`, `cosh`, `tanh`, `degrees`, `radians`,
`random`, `randomblob`, `zeroblob`

**Logic and NULLs:** `coalesce`, `ifnull`, `nullif`, `iif`, `typeof`, `min(a, b, ...)`, `max(a, b, ...)`

**Date and time:** `date`, `time`, `datetime`, `julianday`, `unixepoch`, `strftime`. Inputs: `YYYY-MM-DD`,
`YYYY-MM-DD HH:MM[:SS[.fff]]` (with `T` or a space, and an optional `Z` or `±HH:MM`), `HH:MM[:SS]`, `now`, Julian
day numbers, and Unix times with the `unixepoch` modifier. Modifiers: `±N days|hours|minutes|seconds|months|years`,
`start of day|month|year`, `weekday N`, `unixepoch`, `localtime`, `utc`. `strftime` formats: `%Y %m %d %e %H %M %S
%f %s %j %J %w %u %W %F %T %%`.

**Connection:** `last_insert_rowid`, `changes`, `total_changes`, `quarry_version` (also `sqlite_version`)

**Aggregates:** `count(*)`, `count(x)`, `sum`, `total`, `avg`, `min`, `max`, `group_concat(x [, separator])`,
`string_agg(x, separator)`. Each accepts `DISTINCT` and `FILTER (WHERE ...)`.

**Window functions:** `row_number`, `rank`, `dense_rank`, `percent_rank`, `cume_dist`, `ntile(n)`,
`lag(x [, offset [, default]])`, `lead(...)`, `first_value`, `last_value`, `nth_value(x, n)`, and every aggregate.
Frames: `ROWS` with `UNBOUNDED PRECEDING`, `n PRECEDING`, `CURRENT ROW`, `n FOLLOWING` and `UNBOUNDED FOLLOWING`.
`RANGE` frames support `UNBOUNDED` bounds and `CURRENT ROW`. Without a frame clause, a window with `ORDER BY` runs
from the start of the partition to the current row and its peers.

## Types

| Declared type contains | Affinity |
| --- | --- |
| `INT` | INTEGER |
| `CHAR`, `CLOB` or `TEXT` | TEXT |
| `BLOB`, or no type at all | BLOB (keeps values as given) |
| `REAL`, `FLOA` or `DOUB` | REAL |
| anything else (`NUMERIC`, `DECIMAL`, `BOOLEAN`, `DATE`, ...) | NUMERIC |

Integers are 64-bit. A value that overflows during arithmetic becomes REAL. Binding a Python integer outside
the 64-bit range raises `DataError`.

## Pragmas

| Pragma | Result |
| --- | --- |
| `table_info(t)` | `cid, name, type, notnull, dflt_value, pk` per column |
| `index_list(t)` | `seq, name, unique, origin, partial` per index (`origin` is `c`, `u` or `pk`) |
| `index_info(i)` | `seqno, cid, name` per indexed column |
| `integrity_check`, `quick_check` | `ok`, or one row per problem |
| `page_size`, `page_count`, `freelist_count` | storage numbers |
| `user_version [= n]` | a number your application can use for schema versions |
| `schema_version` | the schema cookie |
| `wal_checkpoint` | runs a checkpoint, returns `busy, log, checkpointed` |
| `journal_mode` | `wal` (or `memory`) |
| `synchronous [= 0 \| 1]` | turn `fsync` off for speed (not crash safe) or back on |
| `cache_size [= n]` | pages kept in memory |
| `table_list`, `database_list`, `function_list` | listings |
| `foreign_keys` | always 0: foreign keys are not enforced |

The schema is also readable as a table: `SELECT * FROM quarry_schema` (also `sqlite_schema` and `sqlite_master`).
