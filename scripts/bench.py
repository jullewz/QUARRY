"""A small benchmark: bulk insert, point lookups, an indexed range, a join and an aggregate.

Run:  python scripts/bench.py [rows]
Numbers depend on your machine and Python version. Quarry is pure Python, so expect it to be one or two
orders of magnitude slower than SQLite, which is written in C.
"""

from __future__ import annotations

import os
import random
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import quarry  # noqa: E402


def run(connect, label: str, n: int) -> dict[str, float]:
    with tempfile.TemporaryDirectory() as tmp:
        con = connect(os.path.join(tmp, "bench.db"))
        rnd = random.Random(1)
        timings: dict[str, float] = {}

        def step(name, fn):
            t = time.perf_counter()
            fn()
            timings[name] = time.perf_counter() - t

        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, city TEXT, age INT)")
        con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INT, amount REAL)")
        cities = [f"city{i}" for i in range(50)]
        users = [(f"user{i}", rnd.choice(cities), rnd.randint(18, 90)) for i in range(n)]
        orders = [(rnd.randint(1, n), round(rnd.uniform(1, 500), 2)) for _ in range(n * 2)]

        def insert():
            con.executemany("INSERT INTO users (name, city, age) VALUES (?, ?, ?)", users)
            con.executemany("INSERT INTO orders (user_id, amount) VALUES (?, ?)", orders)
            con.commit()

        step(f"insert {n * 3:,} rows", insert)
        step(
            "create 2 indexes",
            lambda: (
                con.execute("CREATE INDEX users_age ON users(age)"),
                con.execute("CREATE INDEX orders_user ON orders(user_id)"),
                con.commit(),
            ),
        )
        ids = [rnd.randint(1, n) for _ in range(2000)]
        step("2,000 primary-key lookups", lambda: [con.execute("SELECT name FROM users WHERE id = ?", (i,)).fetchone() for i in ids])
        step("indexed range (age 30-32)", lambda: con.execute("SELECT count(*) FROM users WHERE age BETWEEN 30 AND 32").fetchall())
        step(
            "join + group by",
            lambda: con.execute(
                "SELECT u.city, count(*), sum(o.amount) FROM orders o JOIN users u ON u.id = o.user_id GROUP BY u.city"
            ).fetchall(),
        )
        step("full scan + sort", lambda: con.execute("SELECT name, age FROM users ORDER BY age DESC, name LIMIT 10").fetchall())
        con.close()
        return timings


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20_000
    q = run(lambda p: quarry.connect(p), "quarry", n)
    s = run(lambda p: sqlite3.connect(p), "sqlite3", n)
    width = max(len(k) for k in q)
    print(f"{'':{width}}  {'quarry':>10}  {'sqlite3':>10}")
    for k in q:
        print(f"{k:{width}}  {q[k] * 1000:>8.0f}ms  {s[k] * 1000:>8.1f}ms")


if __name__ == "__main__":
    main()
