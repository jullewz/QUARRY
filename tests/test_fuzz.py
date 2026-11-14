"""Randomly generated queries, compared against SQLite. The seed is fixed, so failures reproduce."""

import random

import pytest

EMP_COLS = ["e.id", "e.dept_id", "e.salary", "e.bonus", "e.manager_id", "e.name", "e.title", "e.hired", "e.note"]
NUM_COLS = ["n", "r", "t", "b", "x"]
LITERALS = ["0", "1", "2", "-1", "2.5", "'10'", "'abc'", "NULL", "50000", "100000", "'2018-01-01'", "X'01'"]


def expr(rnd, cols, depth=0):
    k = rnd.random()
    if depth > 2 or k < 0.35:
        return rnd.choice(cols) if rnd.random() < 0.7 else rnd.choice(LITERALS)
    if k < 0.55:
        op = rnd.choice(["+", "-", "*", "/", "%", "||"])
        return f"({expr(rnd, cols, depth + 1)} {op} {expr(rnd, cols, depth + 1)})"
    if k < 0.7:
        fn = rnd.choice(
            [
                "abs",
                "length",
                "lower",
                "upper",
                "typeof",
                "coalesce",
                "ifnull",
                "nullif",
                "round",
                "substr",
                "max",
                "min",
                "trim",
                "hex",
                "quote",
            ]
        )
        if fn in ("coalesce", "ifnull", "nullif", "max", "min"):
            return f"{fn}({expr(rnd, cols, depth + 1)}, {expr(rnd, cols, depth + 1)})"
        if fn == "substr":
            return f"substr({expr(rnd, cols, depth + 1)}, {rnd.randint(-3, 3)}, {rnd.randint(0, 4)})"
        return f"{fn}({expr(rnd, cols, depth + 1)})"
    if k < 0.8:
        return f"CASE WHEN {cond(rnd, cols, depth + 1)} THEN {expr(rnd, cols, depth + 1)} ELSE {expr(rnd, cols, depth + 1)} END"
    if k < 0.9:
        return f"CAST({expr(rnd, cols, depth + 1)} AS {rnd.choice(['INTEGER', 'REAL', 'TEXT', 'NUMERIC'])})"
    return f"-{expr(rnd, cols, depth + 1)}"


def cond(rnd, cols, depth=0):
    k = rnd.random()
    if depth < 2 and k < 0.25:
        return f"({cond(rnd, cols, depth + 1)} {rnd.choice(['AND', 'OR'])} {cond(rnd, cols, depth + 1)})"
    if k < 0.3:
        return f"NOT {cond(rnd, cols, depth + 1)}"
    if k < 0.4:
        return f"{expr(rnd, cols, depth + 1)} IS {'NOT ' if rnd.random() < 0.5 else ''}NULL"
    if k < 0.5:
        items = ", ".join(rnd.choice(LITERALS) for _ in range(rnd.randint(1, 4)))
        return f"{rnd.choice(cols)} {'NOT ' if rnd.random() < 0.3 else ''}IN ({items})"
    if k < 0.58:
        return f"{rnd.choice(cols)} BETWEEN {rnd.choice(LITERALS)} AND {rnd.choice(LITERALS)}"
    if k < 0.65:
        # LIKE on BLOB values changed between SQLite versions, so keep blobs out of it
        c = rnd.choice(cols)
        return f"({c} LIKE '{rnd.choice(['a%', '%1', '_e%', '%', 'A%'])}' AND typeof({c}) <> 'blob')"
    op = rnd.choice(["=", "!=", "<", "<=", ">", ">=", "IS", "IS NOT"])
    return f"{expr(rnd, cols, depth + 1)} {op} {expr(rnd, cols, depth + 1)}"


def emp_query(rnd):
    shape = rnd.random()
    where = f" WHERE {cond(rnd, EMP_COLS)}" if rnd.random() < 0.85 else ""
    if shape < 0.4:
        cols = ", ".join(expr(rnd, EMP_COLS) for _ in range(rnd.randint(1, 3)))
        return f"SELECT e.id, {cols} FROM emp e{where}"
    if shape < 0.6:
        g = rnd.choice(["e.dept_id", "e.title IS NULL", "e.salary > 90000", "e.manager_id % 3", "length(e.name)"])
        agg = rnd.choice(
            ["count(*)", "sum(e.salary)", "max(e.bonus)", "min(e.name)", "avg(e.salary)", "count(DISTINCT e.dept_id)", "total(e.bonus)"]
        )
        having = f" HAVING {agg} > {rnd.choice(['1', '5', '100000', '0'])}" if rnd.random() < 0.3 else ""
        return f"SELECT {g}, {agg} FROM emp e{where} GROUP BY 1{having}"
    if shape < 0.8:
        jcond = rnd.choice(["d.id = e.dept_id", "d.id = e.manager_id", "d.region = e.title", "d.budget > e.salary"])
        kind = rnd.choice(["JOIN", "LEFT JOIN"])
        return f"SELECT e.id, d.id, d.name FROM emp e {kind} dept d ON {jcond}{where}"
    if shape < 0.9:
        sub = rnd.choice(
            [
                "e.dept_id IN (SELECT id FROM dept WHERE budget > 30000)",
                "EXISTS (SELECT 1 FROM assign a WHERE a.emp_id = e.id AND a.hours > 20)",
                "e.salary > (SELECT avg(salary) FROM emp x WHERE x.dept_id = e.dept_id)",
                "e.id NOT IN (SELECT lead_id FROM project WHERE lead_id IS NOT NULL)",
            ]
        )
        return f"SELECT e.id, e.name FROM emp e WHERE {sub}" + (f" AND {cond(rnd, EMP_COLS)}" if rnd.random() < 0.5 else "")
    order = rnd.choice(["e.salary", "e.name", "e.bonus DESC", "e.title", "e.hired DESC"])
    return (
        f"SELECT e.id, e.salary FROM emp e{where} ORDER BY {order}, e.id LIMIT {rnd.randint(1, 30)} OFFSET {rnd.randint(0, 5)} -- ordered"
    )


def num_query(rnd):
    cols = ", ".join(expr(rnd, NUM_COLS) for _ in range(rnd.randint(1, 3)))
    return f"SELECT rowid, {cols} FROM nums WHERE {cond(rnd, NUM_COLS)}"


def generate(n=400, seed=2026):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        q = emp_query(rnd) if i % 3 else num_query(rnd)
        out.append(q)
    return out


@pytest.mark.parametrize("sql", generate(), ids=lambda s: str(abs(hash(s)) % 10**8))
def test_random_query_matches_sqlite(oracle, sql):
    import sqlite3

    ordered = sql.endswith("-- ordered")
    sql = sql.removesuffix("-- ordered").strip()
    try:
        oracle.ref.execute(sql).fetchall()
    except (sqlite3.OperationalError, OverflowError):
        pytest.skip("SQLite rejects this generated query")
    oracle.compare(sql, ordered=ordered)
