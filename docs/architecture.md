# Architecture

This page follows a statement from text to rows, then follows a commit from memory to disk.

## 1. Parsing (`sql/parser.py`, `sql/ast.py`)

A regular-expression tokenizer turns the text into tokens: words, quoted identifiers (`"x"`, `` `x` ``, `[x]`),
strings, numbers (including `0x1F` hex), blobs (`x'00ff'`), parameters (`?`, `?2`, `:name`, `@name`, `$name`)
and operators. Comments (`--` and `/* */`) are skipped.

A recursive-descent parser builds dataclass nodes. Expression precedence follows SQLite, from loosest to
tightest: `OR`, `AND`, `NOT`, equality (`=`, `IS`, `IN`, `LIKE`, `BETWEEN`, ...), comparison, bitwise, `+ -`,
`* / %`, `||`, unary operators and `COLLATE`. Every node records its start position and its source text. The
position lets errors print a caret under the problem. The text becomes the column name of an unaliased
expression, as in SQLite.

Some words such as `KEY` or `PLAN` stay usable as identifiers. Reserved words need double quotes, and the
error message says so.

## 2. Values and types (`values.py`)

Values are Python `None`, `int`, `float`, `str` and `bytes`. Quarry copies SQLite's dynamic typing:

- A column's declared type gives it an *affinity* (`INTEGER`, `REAL`, `NUMERIC`, `TEXT` or `BLOB`), chosen by
  SQLite's five substring rules.
- On insert, affinity converts values where that loses nothing: `'12'` becomes `12` in an `INTEGER` column,
  `3.0` becomes `3`, and `12` becomes `'12'` in a `TEXT` column.
- Comparisons apply affinity too. A numeric column compared with a string literal converts the literal. A `TEXT`
  column compared with an expression that has no affinity converts the expression to text. An untyped column
  (BLOB affinity) is not "no affinity", so it does not get converted. These rules decide whether `x = '12'` matches.
- Across types, values sort NULL < numbers < text < blobs. Integers and reals compare by value.
- Integer arithmetic overflows into REAL, division truncates toward zero, and division by zero gives NULL.

## 3. Name resolution and expression compilation (`engine/expr.py`)

Each query level has a `Scope`: its FROM sources side by side, with column names, affinities and collations,
and a link to the enclosing scope. A row at run time is one flat tuple of all those columns. A column reference
compiles to "read index *k* of the current row", or of an outer row for correlated subqueries.

Expressions compile once into Python closures: `fn(env) -> value`. That removes tree walking from the inner loop.
The compiler also tracks each expression's affinity and collation, so `compare` closures apply exactly the right
conversions. A column has a collation even when it is the default `BINARY`, so the left operand of a
comparison wins, as in SQLite.

Uncorrelated subqueries run once per statement and are cached. Correlated ones run per outer row.

## 4. Planning (`engine/query.py`)

For each `SELECT`:

1. The FROM clause flattens into a left-deep list of sources. `USING` and `NATURAL` become equality conditions.
2. `WHERE` (and `ON` for inner joins) splits into AND-ed conjuncts. Each conjunct goes to the first join level
   where every column it reads is available, so filters run as early as possible.
3. For each table level, the planner looks at the conjuncts that compare one of its columns with something
   already known (a constant, a parameter, or a column of an earlier table):
   - `rowid = x`, `rowid IN (...)` and rowid ranges use the table's own B+tree.
   - Equality on a prefix of an index, optionally followed by a range on the next column, uses the index.
   - With neither, an equality join with an earlier table builds a **hash table** of this table once per
     statement.
   - Otherwise the level scans.
   An index is used only when the comparison would not convert the column's values and uses `BINARY`
   collation. Then the index order matches the comparison's meaning.
4. Lookups only narrow the search. Every condition still runs on every candidate row. A lookup can return
   extra rows but never miss one, so the planner cannot change an answer, only its speed.

`EXPLAIN QUERY PLAN` prints these decisions in SQLite's format:

```text
QUERY PLAN
├── SEARCH customers AS c USING INTEGER PRIMARY KEY (rowid=?)
└── SEARCH orders AS o USING INDEX orders_customer (customer_id=?)
```

## 5. Execution

A compiled query is a pipeline of Python generators:

```text
nested-loop join (per level: lookup → ON filters → LEFT JOIN null row → WHERE filters)
  → grouping (hash by group key, then sorted)  → HAVING
  → window functions (partition, sort, frame)  → projection
  → DISTINCT → ORDER BY → OFFSET / LIMIT
```

Rows stream until a step needs all of them (grouping, windows, sorting). So `LIMIT 10` on a plain scan reads only
what it needs. Results go to the caller as a list.

- **Aggregates** keep one state object per group. `count(DISTINCT x)` wraps the state in a set of seen values
  that respects collation. When a query has exactly one `min()` or `max()`, bare columns come from the row that
  produced the extreme, as in SQLite.
- **Window functions** sort each partition once. Frames that start at `UNBOUNDED PRECEDING` run incrementally.
  Other `ROWS` frames recompute per row.
- **Recursive CTEs** keep a queue. Each step sees one row of the working set, and `UNION` (without `ALL`)
  skips rows already produced. Because the plan is a generator, an outer `LIMIT` stops the recursion.

## 6. Writing (`engine/database.py`)

`INSERT`, `UPDATE` and `DELETE` find their target rows with the same planner, then read them all before changing
anything. That prevents the classic "Halloween problem", where an update moves a row ahead of the scan and
updates it twice. For each row:

1. Defaults, affinity and the rowid (`INTEGER PRIMARY KEY` columns *are* the rowid).
2. `NOT NULL` and `CHECK` constraints.
3. Uniqueness: the rowid and every unique index. `OR IGNORE`, `OR REPLACE` and `ON CONFLICT ... DO UPDATE` act
   here.
4. The table entry and every index entry change together.

Each statement runs inside a savepoint. If any row fails, the statement's changes disappear while earlier
statements of the transaction stay.

The schema lives in its own B+tree on page 1, one row per table, index, view or `AUTOINCREMENT` counter, holding
the original `CREATE` statement. On open, Quarry parses those statements to rebuild the catalog. Any schema change
bumps a cookie in the header, which invalidates cached plans.

## 7. Pages and the B+tree (`storage/btree.py`)

Tables are B+trees keyed by `(rowid,)`. Indexes are B+trees keyed by `(values..., rowid)` with empty values.
Keys are tuples of SQL values and compare in SQL order, which is what makes `prefix` scans possible:
`index_eq` looks for every key whose first *n* values equal the probe.

- Leaves hold the data. Interior pages hold separator keys and child page numbers.
- A node splits when its encoded size passes the page size, at the point that halves the bytes.
- The root keeps its page number forever. When it splits, both halves move to new pages, so the catalog never
  needs updating.
- Values over a quarter of a page go to a chain of overflow pages.
- Deleting the last entry of a page frees it and unlinks it from its parent. The tree shrinks when the root has
  one child. Half-empty pages are not merged. `VACUUM` rebuilds everything compactly.
- `check()` verifies key order, separator bounds, child counts and equal leaf depth. `PRAGMA integrity_check`
  combines it with page accounting (every page used once or free) and index-to-table cross checks.

## 8. Transactions and durability (`storage/pager.py`)

The pager hands out decoded pages and tracks which ones changed. B+tree code must call `mark_dirty(page)` before
changing a page. That one rule powers everything else:

- **Rollback** drops the dirty pages from the cache. The next read gets the committed version.
- **Savepoints** keep, per savepoint, a copy of each page as it was when the savepoint started.
  `ROLLBACK TO` puts those copies back. Copying a decoded page is cheap, so every statement gets its own savepoint.
- **Commit** encodes the dirty pages and appends them to the write-ahead log as frames:

  ```text
  frame = page number · db size (non-zero only on the commit frame) · salt · checksum · page bytes
  ```

  The checksum chains CRC-32 over every frame since the log began. The log is flushed with `fsync` before the
  commit returns.

On open, recovery reads the log from the start. It accepts frames while the salt matches and the checksum chain
holds, and applies them only up to the last commit frame. A frame half written by a crash breaks the checksum.
So does a stray byte. Either way the incomplete transaction disappears and every earlier one stays.

A **checkpoint** (after 1,000 frames, on close, or with `PRAGMA wal_checkpoint`) copies the newest version of each
logged page into the main file, flushes it, and only then starts a new log with a new salt. A crash in the middle
of a checkpoint leaves the old log intact, and the next open replays it.

A lock on the database file (`flock` on Unix, `msvcrt.locking` on Windows) keeps a second connection out.
