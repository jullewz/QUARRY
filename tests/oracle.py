"""Run the same SQL on Quarry and on SQLite (the reference) and compare the answers."""

from __future__ import annotations

import math
import re
import sqlite3

import quarry
from quarry.values import sort_key

SCHEMA = """
CREATE TABLE dept (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, budget REAL, region TEXT);
CREATE TABLE emp (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    dept_id INTEGER,
    salary INTEGER,
    bonus REAL,
    hired TEXT,
    manager_id INTEGER,
    title TEXT COLLATE NOCASE,
    note
);
CREATE INDEX emp_dept ON emp(dept_id);
CREATE INDEX emp_salary ON emp(salary);
CREATE INDEX emp_dept_salary ON emp(dept_id, salary);
CREATE TABLE project (code TEXT PRIMARY KEY, dept_id INT, lead_id INT, cost NUMERIC, started TEXT);
CREATE TABLE assign (emp_id INT, code TEXT, hours INT, PRIMARY KEY (emp_id, code));
CREATE TABLE nums (n INTEGER, r REAL, t TEXT, b BLOB, x);
"""


def seed_rows() -> list[str]:
    import random

    rnd = random.Random(7)
    out = []
    regions = ["north", "south", "east", "west", None]
    for i in range(1, 9):
        budget = "NULL" if i == 5 else str(round(rnd.uniform(1e4, 9e4), 2))
        region = regions[i % 5]
        out.append(f"INSERT INTO dept VALUES ({i}, 'dept{i}', {budget}, {'NULL' if region is None else repr(region)})")
    titles = ["Engineer", "engineer", "Manager", "Analyst", "Intern", None]
    first = ["ana", "ben", "cara", "dev", "eli", "fay", "gus", "hana", "ivo", "jo", "kai", "lea"]
    for i in range(1, 121):
        dept = "NULL" if i % 17 == 0 else str(rnd.randint(1, 9))
        salary = "NULL" if i % 13 == 0 else str(rnd.randint(30, 160) * 1000)
        bonus = "NULL" if i % 4 == 0 else str(round(rnd.uniform(0, 5000), 2))
        hired = f"'20{rnd.randint(10, 24):02d}-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}'"
        mgr = "NULL" if i <= 5 else str(rnd.randint(1, i - 1))
        title = titles[i % 6]
        note = rnd.choice(["NULL", "1", "'1'", "2.5", "'x'", "X'4142'", "'Hello World'"])
        name = first[i % 12] + str(i)
        out.append(
            f"INSERT INTO emp VALUES ({i}, '{name}', {dept}, {salary}, {bonus}, {hired}, {mgr}, {'NULL' if title is None else repr(title)}, {note})"
        )
    for i in range(1, 31):
        cost = rnd.choice(["100", "250.5", "'300'", "NULL", "1e3"])
        out.append(
            f"INSERT INTO project VALUES ('P{i:02d}', {rnd.randint(1, 8)}, {rnd.randint(1, 120)}, {cost}, '2023-{rnd.randint(1, 12):02d}-01')"
        )
    pairs = set()
    while len(pairs) < 200:
        pairs.add((rnd.randint(1, 120), f"P{rnd.randint(1, 30):02d}"))
    for e, c in sorted(pairs):
        out.append(f"INSERT INTO assign VALUES ({e}, '{c}', {rnd.randint(1, 40)})")
    vals = [
        "NULL",
        "0",
        "1",
        "-1",
        "2",
        "10",
        "'10'",
        "'abc'",
        "'ABC'",
        "2.5",
        "-0.5",
        "'2.5'",
        "X'0102'",
        "''",
        "' 7'",
        "1e3",
        "9223372036854775807",
    ]
    for _ in range(60):
        out.append(
            f"INSERT INTO nums VALUES ({rnd.choice(vals)}, {rnd.choice(vals)}, {rnd.choice(vals)}, {rnd.choice(vals)}, {rnd.choice(vals)})"
        )
    return out


class Oracle:
    def __init__(self) -> None:
        self.ref = sqlite3.connect(":memory:")
        self.db = quarry.connect(":memory:", autocommit=True)
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                self.both(stmt)
        for stmt in seed_rows():
            self.both(stmt)

    def both(self, sql: str, params: tuple = ()) -> None:
        self.ref.execute(sql, params)
        self.db.execute(sql, params)

    def compare(self, sql: str, params: tuple = (), ordered: bool | None = None) -> tuple[list, list]:
        expected = self.ref.execute(sql, params).fetchall()
        got = self.db.execute(sql, params).fetchall()
        if ordered is None:
            ordered = "order by" in sql.lower() and "limit" not in sql.lower().split("order by")[0]
        if not same(expected, got, ordered):
            raise AssertionError(f"results differ for:\n  {sql}\n  sqlite: {expected[:12]}\n  quarry: {got[:12]}")
        return expected, got


def norm(v: object) -> object:
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        return round(v, 9) if abs(v) < 1e15 else v
    return v


def same(a: list, b: list, ordered: bool) -> bool:
    if len(a) != len(b):
        return False
    ra = [tuple(norm(v) for v in r) for r in a]
    rb = [tuple(norm(v) for v in r) for r in b]
    if not ordered:
        key = lambda r: tuple(sort_key(v) for v in r)  # noqa: E731
        ra.sort(key=key)
        rb.sort(key=key)
    for x, y in zip(ra, rb):
        if len(x) != len(y):
            return False
        for p, q in zip(x, y):
            if type(p) is not type(q):
                if (
                    isinstance(p, (int, float))
                    and isinstance(q, (int, float))
                    and float(p) == float(q)
                    and (isinstance(p, float) == isinstance(q, float))
                ):
                    continue
                return False
            if isinstance(p, float) and isinstance(q, float):
                if not math.isclose(p, q, rel_tol=1e-9, abs_tol=1e-9):
                    return False
            elif p != q:
                if isinstance(p, str) and isinstance(q, str) and _float_text_equal(p, q):
                    continue
                return False
    return True


_DIGITS = re.compile(r"\d{13,}")


def _float_text_equal(a: str, b: str) -> bool:
    """REAL-to-TEXT conversion prints 15 to 17 digits depending on the SQLite version. Compare 12 of them."""

    def canon(s: str) -> str:
        return _DIGITS.sub(lambda m: m.group(0)[:12], s)

    return canon(a) == canon(b)
