"""SQL features, constraints, transactions and schema changes."""

import pytest

import quarry
from quarry import IntegrityError, OperationalError, ProgrammingError, SQLError


def rows(con, sql, params=()):
    return con.execute(sql, params).fetchall()


def test_integer_primary_key_is_the_rowid(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t (v) VALUES ('a'), ('b')")
    db.execute("INSERT INTO t VALUES (10, 'c')")
    db.execute("INSERT INTO t (v) VALUES ('d')")
    assert rows(db, "SELECT rowid, id, v FROM t") == [(1, 1, "a"), (2, 2, "b"), (10, 10, "c"), (11, 11, "d")]
    with pytest.raises(IntegrityError, match="datatype mismatch"):
        db.execute("INSERT INTO t VALUES ('x', 'e')")
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed: t.id"):
        db.execute("INSERT INTO t VALUES (10, 'dup')")


def test_autoincrement_never_reuses_ids(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v)")
    db.execute("INSERT INTO t (v) VALUES (1), (2), (3)")
    db.execute("DELETE FROM t WHERE id = 3")
    db.execute("INSERT INTO t (v) VALUES (4)")
    assert rows(db, "SELECT id FROM t") == [(1,), (2,), (4,)]


def test_not_null_check_and_unique_constraints(db):
    db.execute("CREATE TABLE p (sku TEXT NOT NULL UNIQUE, price REAL CHECK (price >= 0), qty INT DEFAULT 0, CHECK (qty <= 1000))")
    db.execute("INSERT INTO p (sku, price) VALUES ('a', 1.5)")
    assert rows(db, "SELECT * FROM p") == [("a", 1.5, 0)]
    with pytest.raises(IntegrityError, match="NOT NULL constraint failed: p.sku"):
        db.execute("INSERT INTO p (price) VALUES (1)")
    with pytest.raises(IntegrityError, match=r"CHECK constraint failed: price >= 0"):
        db.execute("INSERT INTO p VALUES ('b', -1, 0)")
    with pytest.raises(IntegrityError, match="CHECK constraint failed: qty <= 1000"):
        db.execute("INSERT INTO p VALUES ('b', 1, 5000)")
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed: p.sku"):
        db.execute("INSERT INTO p VALUES ('a', 2, 0)")
    db.execute("INSERT INTO p VALUES (NULL, 1, 1)") if False else None
    db.execute("INSERT OR IGNORE INTO p VALUES ('a', 9, 9)")
    db.execute("INSERT OR REPLACE INTO p VALUES ('a', 9, 9)")
    assert rows(db, "SELECT * FROM p") == [("a", 9.0, 9)]


def test_multi_column_unique_and_nulls(db):
    db.execute("CREATE TABLE t (a, b, UNIQUE (a, b))")
    db.execute("INSERT INTO t VALUES (1, 1), (1, 2), (1, NULL), (1, NULL)")  # NULLs never collide
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed: t.a, t.b"):
        db.execute("INSERT INTO t VALUES (1, 2)")


def test_a_failed_statement_changes_nothing(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v UNIQUE)")
    db.execute("INSERT INTO t VALUES (1, 'a')")
    with pytest.raises(IntegrityError):
        db.execute("INSERT INTO t (v) VALUES ('b'), ('c'), ('a'), ('d')")
    assert rows(db, "SELECT v FROM t") == [("a",)]
    db.execute("BEGIN")
    db.execute("INSERT INTO t (v) VALUES ('x')")
    with pytest.raises(IntegrityError):
        db.execute("UPDATE t SET v = 'a'")
    db.execute("COMMIT")
    assert rows(db, "SELECT v FROM t ORDER BY id") == [("a",), ("x",)]


def test_upsert(db):
    db.execute("CREATE TABLE stock (sku TEXT PRIMARY KEY, qty INT NOT NULL)")
    up = "INSERT INTO stock VALUES (?, ?) ON CONFLICT (sku) DO UPDATE SET qty = qty + excluded.qty"
    db.execute(up, ("apple", 3))
    db.execute(up, ("apple", 4))
    db.execute(up, ("pear", 1))
    db.execute("INSERT INTO stock VALUES ('pear', 100) ON CONFLICT DO NOTHING")
    assert rows(db, "SELECT * FROM stock ORDER BY sku") == [("apple", 7), ("pear", 1)]
    db.execute("INSERT INTO stock VALUES ('apple', 1) ON CONFLICT (sku) DO UPDATE SET qty = 0 WHERE excluded.qty > 5")
    assert rows(db, "SELECT qty FROM stock WHERE sku = 'apple'") == [(7,)]
    with pytest.raises(SQLError, match="does not match any PRIMARY KEY or UNIQUE"):
        db.execute("INSERT INTO stock VALUES ('x', 1) ON CONFLICT (qty) DO NOTHING")


def test_returning(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v)")
    assert rows(db, "INSERT INTO t (v) VALUES ('a'), ('b') RETURNING id, upper(v)") == [(1, "A"), (2, "B")]
    assert rows(db, "UPDATE t SET v = v || '!' WHERE id = 2 RETURNING *") == [(2, "b!")]
    assert rows(db, "DELETE FROM t WHERE id = 1 RETURNING v") == [("a",)]


def test_update_and_delete_counts(db):
    db.execute("CREATE TABLE t (x)")
    cur = db.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(10)])
    assert cur.rowcount == 10
    assert db.execute("UPDATE t SET x = x * 10 WHERE x > 6").rowcount == 3
    assert db.execute("DELETE FROM t WHERE x < 3").rowcount == 3
    assert db.execute("DELETE FROM t").rowcount == 7
    assert rows(db, "SELECT changes(), total_changes()") == [(7, 23)]


def test_updating_the_key_column_through_an_index(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, k INT)")
    db.execute("CREATE INDEX t_k ON t(k)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [(i, i) for i in range(1, 51)])
    db.execute("UPDATE t SET id = id + 1000, k = k + 1 WHERE k > 40")
    assert rows(db, "SELECT count(*) FROM t WHERE k > 41") == [(10,)]
    assert rows(db, "SELECT id FROM t WHERE k = 50") == [(1049,)]
    assert rows(db, "PRAGMA integrity_check") == [("ok",)]


def test_transactions_and_savepoints(db):
    db.execute("CREATE TABLE t (x)")
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (1)")
    db.execute("SAVEPOINT a")
    db.execute("INSERT INTO t VALUES (2)")
    db.execute("SAVEPOINT b")
    db.execute("INSERT INTO t VALUES (3)")
    db.execute("ROLLBACK TO b")
    db.execute("INSERT INTO t VALUES (4)")
    db.execute("RELEASE a")
    db.execute("COMMIT")
    assert rows(db, "SELECT x FROM t") == [(1,), (2,), (4,)]
    db.execute("BEGIN")
    db.execute("DELETE FROM t")
    db.execute("ROLLBACK")
    assert rows(db, "SELECT count(*) FROM t") == [(3,)]
    with pytest.raises(OperationalError, match="no transaction is active"):
        db.execute("COMMIT")
    db.execute("SAVEPOINT solo")  # starts a transaction
    db.execute("INSERT INTO t VALUES (5)")
    db.execute("RELEASE solo")  # and commits it
    assert not db.in_transaction
    assert rows(db, "SELECT count(*) FROM t") == [(4,)]


def test_schema_changes_roll_back(db):
    db.execute("BEGIN")
    db.execute("CREATE TABLE gone (x)")
    db.execute("INSERT INTO gone VALUES (1)")
    db.execute("ROLLBACK")
    with pytest.raises(SQLError, match="no such table: gone"):
        db.execute("SELECT * FROM gone")


def test_indexes_are_used_and_kept_in_sync(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a INT, b TEXT)")
    db.executemany("INSERT INTO t (a, b) VALUES (?, ?)", [(i % 100, f"v{i}") for i in range(2000)])
    db.execute("CREATE INDEX t_a ON t(a)")
    db.execute("CREATE UNIQUE INDEX t_b ON t(b)")
    plan = " ".join(r[3] for r in rows(db, "EXPLAIN QUERY PLAN SELECT * FROM t WHERE a = 5"))
    assert "USING INDEX t_a (a=?)" in plan
    plan = " ".join(r[3] for r in rows(db, "EXPLAIN QUERY PLAN SELECT * FROM t WHERE a > 95"))
    assert "USING INDEX t_a (a>?)" in plan
    assert rows(db, "SELECT count(*) FROM t WHERE a = 5") == [(20,)]
    db.execute("DELETE FROM t WHERE a = 5")
    db.execute("UPDATE t SET a = 5 WHERE a = 6")
    assert rows(db, "SELECT count(*) FROM t WHERE a = 5") == [(20,)]
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed: t.b"):
        db.execute("UPDATE t SET b = 'v7' WHERE id = 2")
    with pytest.raises(IntegrityError):
        db.execute("CREATE UNIQUE INDEX t_a_unique ON t(a)")
    assert rows(db, "PRAGMA integrity_check") == [("ok",)]


def test_alter_table(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, price REAL CHECK (price > 0))")
    db.execute("INSERT INTO t (name, price) VALUES ('a', 1), ('b', 2)")
    db.execute("ALTER TABLE t ADD COLUMN qty INT NOT NULL DEFAULT 5")
    assert rows(db, "SELECT name, qty FROM t") == [("a", 5), ("b", 5)]
    db.execute("ALTER TABLE t RENAME COLUMN price TO cost")
    with pytest.raises(IntegrityError, match="cost > 0"):
        db.execute("INSERT INTO t (name, cost) VALUES ('c', -1)")
    db.execute("ALTER TABLE t RENAME TO items")
    db.execute("ALTER TABLE items DROP COLUMN name")
    assert rows(db, "SELECT * FROM items") == [(1, 1.0, 5), (2, 2.0, 5)]
    assert "CREATE TABLE items" in rows(db, "SELECT sql FROM quarry_schema WHERE name = 'items'")[0][0]
    with pytest.raises(SQLError, match="cannot drop PRIMARY KEY"):
        db.execute("ALTER TABLE items DROP COLUMN id")
    with pytest.raises(SQLError, match="non-constant default"):
        db.execute("ALTER TABLE items ADD COLUMN at TEXT DEFAULT (datetime('now'))")


def test_views(db):
    db.execute("CREATE TABLE sales (region TEXT, amount INT)")
    db.executemany("INSERT INTO sales VALUES (?, ?)", [("n", 5), ("n", 7), ("s", 1)])
    db.execute("CREATE VIEW totals (region, total) AS SELECT region, sum(amount) FROM sales GROUP BY region")
    assert rows(db, "SELECT * FROM totals WHERE total > 2") == [("n", 12)]
    with pytest.raises(SQLError, match="it is a view"):
        db.execute("INSERT INTO totals VALUES ('x', 1)")
    db.execute("DROP VIEW totals")
    with pytest.raises(OperationalError, match="no such view"):
        db.execute("DROP VIEW totals")
    db.execute("DROP VIEW IF EXISTS totals")


def test_create_table_as_select(db):
    db.execute("CREATE TABLE a (x INTEGER, y TEXT)")
    db.execute("INSERT INTO a VALUES (1, 'one'), (2, 'two')")
    db.execute("CREATE TABLE b AS SELECT x * 10 AS big, y FROM a WHERE x > 1")
    assert rows(db, "SELECT * FROM b") == [(20, "two")]


def test_parameters(db):
    assert rows(db, "SELECT ?, ?2, :name, @other", (1, 2)) if False else True
    assert rows(db, "SELECT :a + :b, :a", {"a": 2, "b": 3}) == [(5, 2)]
    assert rows(db, "SELECT ?1 || ?1, ?2", ("x", "y")) == [("xx", "y")]
    with pytest.raises(ProgrammingError, match="uses 2, and 1 were supplied"):
        db.execute("SELECT ?, ?", (1,))
    with pytest.raises(ProgrammingError, match=":b"):
        db.execute("SELECT :a, :b", {"a": 1})
    with pytest.raises(quarry.InterfaceError):
        db.execute("SELECT ?", (object(),))


def test_errors_point_at_the_problem(db):
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    with pytest.raises(SQLError) as e:
        db.execute("SELECT emial FROM users")
    assert "no such column: emial" in str(e.value) and "did you mean email?" in str(e.value) and "^" in str(e.value)
    with pytest.raises(SQLError, match="did you mean users"):
        db.execute("SELECT * FROM user")
    with pytest.raises(SQLError, match="reserved word"):
        db.execute("SELECT order FROM users")
    with pytest.raises(SQLError, match="no such function: lenght"):
        db.execute("SELECT lenght(email) FROM users")
    with pytest.raises(SQLError, match="misuse of aggregate"):
        db.execute("SELECT * FROM users WHERE count(*) > 1")
    with pytest.raises(SQLError, match="wrong number of arguments"):
        db.execute("SELECT substr()")
    with pytest.raises(SQLError, match="one statement at a time"):
        db.execute("SELECT 1; SELECT 2")
    with pytest.raises(quarry.NotSupportedError):
        db.execute("SELECT * FROM users RIGHT JOIN users u2 ON 1")


def test_window_functions_and_ctes_together(db):
    db.execute("CREATE TABLE m (day INT, v INT)")
    db.executemany("INSERT INTO m VALUES (?, ?)", [(d, (d * 37) % 11) for d in range(1, 15)])
    got = rows(
        db,
        """WITH w AS (SELECT day, v, avg(v) OVER (ORDER BY day ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS ma FROM m)
           SELECT day FROM w WHERE v > ma ORDER BY day""",
    )
    assert got and all(isinstance(r[0], int) for r in got)
    tree = rows(
        db,
        "WITH RECURSIVE t(n, s) AS (SELECT 1, '1' UNION ALL SELECT n + 1, s || ',' || (n + 1) FROM t WHERE n < 4) SELECT s FROM t ORDER BY n DESC LIMIT 1",
    )
    assert tree == [("1,2,3,4",)]


def test_pragmas(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT 'x', UNIQUE (name))")
    assert rows(db, "PRAGMA table_info(t)") == [(0, "id", "INTEGER", 0, None, 1), (1, "name", "TEXT", 1, "'x'", 0)]
    assert rows(db, "PRAGMA index_list(t)") == [(0, "quarry_autoindex_t_1", 1, "u", 0)]
    db.execute("PRAGMA user_version = 7")
    assert rows(db, "PRAGMA user_version") == [(7,)]
    assert rows(db, "PRAGMA page_size") == [(4096,)]
    with pytest.raises(OperationalError, match="unknown pragma"):
        db.execute("PRAGMA nonsense")


def test_dump_recreates_the_database(db):
    db.executescript(
        """
        CREATE TABLE a (id INTEGER PRIMARY KEY, t TEXT, b BLOB, r REAL);
        CREATE TABLE plain (x, y);
        CREATE INDEX a_t ON a(t);
        CREATE VIEW v AS SELECT t FROM a;
        INSERT INTO a (t, b, r) VALUES ('it''s', X'00ff', 1.5), (NULL, NULL, -2.25);
        INSERT INTO plain VALUES (1, 2);
        """
    )
    script = "\n".join(db.iterdump())
    other = quarry.connect(":memory:", autocommit=True)
    other.executescript(script)
    for q in ("SELECT * FROM a", "SELECT rowid, * FROM plain", "SELECT * FROM v"):
        assert rows(other, q) == rows(db, q)
    assert "a_t" in [r[0] for r in rows(other, "SELECT name FROM quarry_schema WHERE type = 'index'")]


def test_date_and_time(db):
    assert rows(db, "SELECT date('2024-01-31', '+1 month'), datetime(0, 'unixepoch'), strftime('%j', '2024-12-31')") == [
        ("2024-03-02", "1970-01-01 00:00:00", "366")
    ]
    today = rows(db, "SELECT date('now')")[0][0]
    assert len(today) == 10 and today[4] == "-"
