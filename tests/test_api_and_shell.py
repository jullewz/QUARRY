"""The DB-API connection and the quarry shell."""

import io

import pytest

import quarry
from quarry.shell import Shell, main, plan_tree


def test_dbapi_basics():
    con = quarry.connect()
    cur = con.cursor()
    cur.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    cur.executemany("INSERT INTO t VALUES (?, ?)", [(1, "x"), (2, "y"), (3, "z")])
    assert cur.rowcount == 3 and cur.lastrowid == 3
    cur.execute("SELECT a, b FROM t ORDER BY a")
    assert [d[0] for d in cur.description] == ["a", "b"]
    assert cur.fetchone() == (1, "x")
    assert cur.fetchmany(1) == [(2, "y")]
    assert cur.fetchall() == [(3, "z")]
    assert cur.fetchone() is None
    assert quarry.apilevel == "2.0" and quarry.paramstyle == "qmark"


def test_implicit_transactions_like_sqlite3(tmp_path):
    path = tmp_path / "tx.qdb"
    con = quarry.connect(path)
    con.execute("CREATE TABLE t (x)")
    con.execute("INSERT INTO t VALUES (1)")
    assert con.in_transaction
    con.rollback()
    assert con.execute("SELECT count(*) FROM t").fetchone() == (0,)
    with con:
        con.execute("INSERT INTO t VALUES (2)")
    con.close()
    con = quarry.connect(path)
    assert con.execute("SELECT x FROM t").fetchall() == [(2,)]
    con.close()


def test_row_factory():
    con = quarry.connect()
    con.row_factory = quarry.Row
    row = con.execute("SELECT 1 AS one, 'b' AS two").fetchone()
    assert row["one"] == 1 and row["TWO"] == "b" and row[1] == "b" and row.keys() == ["one", "two"]


def test_closed_connection_raises():
    con = quarry.connect()
    con.close()
    with pytest.raises(quarry.InterfaceError):
        con.execute("SELECT 1")


def test_backup_copies_everything():
    a = quarry.connect(autocommit=True)
    a.executescript(
        "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v UNIQUE); INSERT INTO t (v) VALUES (1), (2); CREATE VIEW vv AS SELECT * FROM t;"
    )
    b = quarry.connect(autocommit=True)
    a.backup(b)
    assert b.execute("SELECT * FROM vv").fetchall() == [(1, 1), (2, 2)]
    b.execute("INSERT INTO t (v) VALUES (3)")
    assert b.execute("SELECT max(id) FROM t").fetchone() == (3,)


def run_shell(*commands, mode=None):
    out, err = io.StringIO(), io.StringIO()
    sh = Shell(":memory:", out=out, err=err, color=False)
    if mode:
        sh.printer.mode = mode
    sh.run_script("\n".join(commands))
    sh.close()
    return out.getvalue(), err.getvalue(), sh


def test_shell_box_output_and_counts():
    out, err, _ = run_shell(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);", "INSERT INTO t (name) VALUES ('ada'), (NULL);", "SELECT * FROM t;"
    )
    assert "2 rows inserted" in out
    assert "│ id │ name │" in out and "│  2 │ NULL │" in out and err == ""


def test_shell_multiline_statements_and_errors():
    out, err, sh = run_shell("SELECT", "  1 + 1", "  AS two;", "SELECT nope;")
    assert "two" in out and "2" in out
    assert "error: no such column: nope" in err and sh.failed


def test_shell_modes():
    out, _, _ = run_shell(".mode csv", "SELECT 1 AS a, 'x,y' AS b, NULL AS c;")
    assert out.splitlines() == ["a,b,c", '1,"x,y",']
    out, _, _ = run_shell(".mode json", "SELECT 1 AS a, X'ff' AS b;")
    assert '"a": 1' in out and '"b": "ff"' in out
    out, _, _ = run_shell(".mode line", "SELECT 1 AS a, 2 AS bb;")
    assert " a = 1" in out and "bb = 2" in out
    out, _, _ = run_shell(".mode markdown", "SELECT 1 AS a;")
    assert out.splitlines()[0] == "| a |"


def test_shell_dot_commands(tmp_path):
    csv_path = tmp_path / "people.csv"
    csv_path.write_text("name,age,city\nada,36,london\nbob,25,\n", encoding="utf-8")
    out, err, _ = run_shell(
        f".import {csv_path} people",
        ".tables",
        ".schema people",
        "SELECT typeof(age), sum(age) FROM people;",
        ".plan SELECT * FROM people WHERE rowid = 1",
        ".check",
        f".export {tmp_path / 'out.csv'} SELECT name FROM people ORDER BY name",
        ".mode nonsense",
    )
    assert "imported 2 rows into people" in out
    assert "CREATE TABLE people" in out and "age INTEGER" in out
    assert "integer" in out and "61" in out
    assert "SEARCH people USING INTEGER PRIMARY KEY" in out
    assert "ok: every page" in out
    assert (tmp_path / "out.csv").read_text().splitlines() == ["name", "ada", "bob"]
    assert "unknown mode" in err


def test_plan_tree_drawing():
    lines = plan_tree([(1, 0, 0, "SCAN a"), (2, 0, 0, "COMPOUND"), (3, 2, 0, "SCAN b"), (4, 2, 0, "SCAN c")])
    assert lines == ["QUERY PLAN", "├── SCAN a", "└── COMPOUND", "    ├── SCAN b", "    └── SCAN c"]


def test_command_line(tmp_path, capsys):
    db = str(tmp_path / "cli.qdb")
    assert main([db, "CREATE TABLE t (x)", "INSERT INTO t VALUES (41)", "SELECT x + 1 AS answer FROM t", "--no-color"]) == 0
    out = capsys.readouterr().out
    assert "answer" in out and "42" in out
    assert main([db, "-m", "csv", "--no-header", "SELECT x FROM t"]) == 0
    assert capsys.readouterr().out.strip() == "41"
    assert main([db, "SELECT broken FROM"]) == 1


def test_shell_scripts_allow_comments_and_blank_lines_before_dot_commands():
    out, err, _ = run_shell("CREATE TABLE t (x);", "", "-- a comment", ".mode csv", "SELECT 1 AS a;")
    assert err == "" and out.splitlines() == ["a", "1"]


def test_crash_recovery_with_a_killed_process():
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "examples" / "crash_test.py"
    done = subprocess.run([sys.executable, str(script), "3"], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.count("[ok]") == 3
