"""The ``quarry`` command: an interactive SQL shell, like ``sqlite3`` with clearer output.

quarry shop.qdb                      open (or create) a database and start the shell
quarry shop.qdb "SELECT * FROM t"    run SQL and exit
quarry shop.qdb < script.sql         run a script from standard input
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any, TextIO

from . import __version__
from .engine.database import Database, Result
from .errors import Error, SQLError
from .sql.parser import split_statements, tokenize
from .values import Value, format_real

MODES = ("box", "table", "csv", "tsv", "json", "line", "list", "markdown")

HELP = """\
.help                    show this help
.tables [PATTERN]        list tables and views
.schema [NAME]           show CREATE statements
.indexes [TABLE]         list indexes
.mode MODE               output: box, table, markdown, csv, tsv, json, line, list
.headers on|off          show column names (csv, tsv, list)
.nullvalue TEXT          how to print NULL (default: empty in csv, NULL elsewhere)
.timer on|off            show how long each statement takes
.plan QUERY              draw the query plan as a tree
.import FILE TABLE       load a CSV file (the first row names the columns)
.export FILE QUERY       write a query's result to a CSV file
.dump [FILE]             print (or save) SQL that recreates the database
.read FILE               run the SQL in a file
.open FILE               close this database and open another
.check                   run an integrity check
.stats                   page counts, free pages and log size
.quit                    leave (Ctrl+D works too)

End SQL statements with a semicolon. A statement can span several lines."""


class Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"\x1b[{code}m{text}\x1b[0m"

    def head(self, t: str) -> str:
        return self("1;38;5;141", t)

    def dim(self, t: str) -> str:
        return self("38;5;245", t)

    def err(self, t: str) -> str:
        return self("38;5;203", t)

    def ok(self, t: str) -> str:
        return self("38;5;114", t)

    def accent(self, t: str) -> str:
        return self("38;5;177", t)


def _supports_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if sys.platform == "win32":
        os.system("")  # turns on ANSI escape handling in Windows 10 consoles
    return True


def render_value(v: Value, null: str = "NULL") -> str:
    if v is None:
        return null
    if isinstance(v, float):
        return format_real(v)
    if isinstance(v, bytes):
        return "x'" + v.hex() + "'"
    return str(v)


def _width(text: str) -> int:
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int, right: bool) -> str:
    gap = " " * max(0, width - _width(text))
    return gap + text if right else text + gap


def _clip(text: str, limit: int) -> str:
    text = text.replace("\n", "\\n").replace("\t", " ")
    if _width(text) <= limit:
        return text
    out = ""
    for ch in text:
        if _width(out + ch) > limit - 1:
            break
        out += ch
    return out + "…"


class Printer:
    def __init__(self, out: TextIO, style: Style) -> None:
        self.out = out
        self.style = style
        self.mode = "box"
        self.headers = True
        self.null: str | None = None

    def write(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def result(self, columns: list[str], rows: list[tuple]) -> None:
        getattr(self, "mode_" + self.mode)(columns, rows)

    def _cells(self, rows: list[tuple], null: str) -> list[list[str]]:
        return [[render_value(v, null) for v in r] for r in rows]

    def _grid(self, columns: list[str], rows: list[tuple]) -> tuple[list[list[str]], list[int], list[bool]]:
        limit = max(12, (shutil.get_terminal_size((120, 24)).columns - 3 * len(columns)) // max(1, len(columns)))
        limit = min(limit, 60)
        cells = [[_clip(c, limit) for c in r] for r in self._cells(rows, "NULL" if self.null is None else self.null)]
        heads = [_clip(c, limit) for c in columns]
        widths = [max([_width(h)] + [_width(r[i]) for r in cells]) for i, h in enumerate(heads)]
        right = [bool(rows) and all(isinstance(r[i], (int, float)) or r[i] is None for r in rows) for i in range(len(columns))]
        return [heads] + cells, widths, right

    def mode_box(self, columns: list[str], rows: list[tuple]) -> None:
        grid, widths, right = self._grid(columns, rows)
        s = self.style
        line = lambda left, mid, right: s.dim(left + mid.join("─" * (w + 2) for w in widths) + right)  # noqa: E731
        self.write(line("╭", "┬", "╮"))
        self.write(s.dim("│") + s.dim("│").join(" " + s.head(_pad(h, w, False)) + " " for h, w in zip(grid[0], widths)) + s.dim("│"))
        self.write(line("├", "┼", "┤"))
        for r, raw in zip(grid[1:], rows):
            cells = []
            for c, w, rt, v in zip(r, widths, right, raw):
                text = _pad(c, w, rt)
                if v is None:
                    text = s.dim(text)
                cells.append(" " + text + " ")
            self.write(s.dim("│") + s.dim("│").join(cells) + s.dim("│"))
        self.write(line("╰", "┴", "╯"))

    def mode_table(self, columns: list[str], rows: list[tuple]) -> None:
        grid, widths, right = self._grid(columns, rows)
        sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
        self.write(sep)
        self.write("|" + "|".join(" " + _pad(h, w, False) + " " for h, w in zip(grid[0], widths)) + "|")
        self.write(sep)
        for r in grid[1:]:
            self.write("|" + "|".join(" " + _pad(c, w, rt) + " " for c, w, rt in zip(r, widths, right)) + "|")
        self.write(sep)

    def mode_markdown(self, columns: list[str], rows: list[tuple]) -> None:
        grid, widths, right = self._grid(columns, rows)
        self.write("| " + " | ".join(_pad(h, w, False) for h, w in zip(grid[0], widths)) + " |")
        self.write("|" + "|".join((":" if False else "-") + "-" * w + (":" if rt else "-") for w, rt in zip(widths, right)) + "|")
        for r in grid[1:]:
            self.write("| " + " | ".join(_pad(c.replace("|", "\\|"), w, rt) for c, w, rt in zip(r, widths, right)) + " |")

    def _delimited(self, columns: list[str], rows: list[tuple], delimiter: str) -> None:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
        if self.headers:
            w.writerow(columns)
        null = "" if self.null is None else self.null
        for r in rows:
            w.writerow([render_value(v, null) for v in r])
        self.out.write(buf.getvalue())

    def mode_csv(self, columns: list[str], rows: list[tuple]) -> None:
        self._delimited(columns, rows, ",")

    def mode_tsv(self, columns: list[str], rows: list[tuple]) -> None:
        self._delimited(columns, rows, "\t")

    def mode_list(self, columns: list[str], rows: list[tuple]) -> None:
        null = "" if self.null is None else self.null
        if self.headers:
            self.write("|".join(columns))
        for r in rows:
            self.write("|".join(render_value(v, null) for v in r))

    def mode_json(self, columns: list[str], rows: list[tuple]) -> None:
        def conv(v: Value) -> Any:
            if isinstance(v, bytes):
                return v.hex()
            if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
                return str(v)
            return v

        data = [{c: conv(v) for c, v in zip(columns, r)} for r in rows]
        self.write(json.dumps(data, ensure_ascii=False, indent=2))

    def mode_line(self, columns: list[str], rows: list[tuple]) -> None:
        width = max((_width(c) for c in columns), default=0)
        null = "NULL" if self.null is None else self.null
        for i, r in enumerate(rows):
            if i:
                self.write()
            for c, v in zip(columns, r):
                self.write(f"{self.style.head(_pad(c, width, True))} = {render_value(v, null)}")


def plan_tree(rows: list[tuple]) -> list[str]:
    """Draw EXPLAIN QUERY PLAN rows (id, parent, notused, detail) as a tree."""
    children: dict[int, list[tuple]] = {}
    for r in rows:
        children.setdefault(r[1], []).append(r)
    lines = ["QUERY PLAN"]

    def draw(parent: int, prefix: str) -> None:
        kids = children.get(parent, [])
        for k, r in enumerate(kids):
            last = k == len(kids) - 1
            lines.append(prefix + ("└── " if last else "├── ") + str(r[3]))
            draw(r[0], prefix + ("    " if last else "│   "))

    draw(0, "")
    return lines


class Shell:
    def __init__(self, path: str, out: TextIO | None = None, err: TextIO | None = None, color: bool | None = None) -> None:
        self.path = path
        self.db = Database(path)
        out = out or sys.stdout
        self.out = out
        self.err = err or sys.stderr
        use_color = _supports_color(out) if color is None else color
        self.style = Style(use_color)
        self.printer = Printer(out, self.style)
        self.timer = False
        self.failed = False
        self.commands: dict[str, Callable[[list[str]], None]] = {
            "help": self.cmd_help,
            "tables": self.cmd_tables,
            "schema": self.cmd_schema,
            "indexes": self.cmd_indexes,
            "indices": self.cmd_indexes,
            "mode": self.cmd_mode,
            "headers": self.cmd_headers,
            "header": self.cmd_headers,
            "nullvalue": self.cmd_nullvalue,
            "timer": self.cmd_timer,
            "plan": self.cmd_plan,
            "import": self.cmd_import,
            "export": self.cmd_export,
            "dump": self.cmd_dump,
            "read": self.cmd_read,
            "open": self.cmd_open,
            "check": self.cmd_check,
            "stats": self.cmd_stats,
        }

    # ------------------------------------------------------------ running

    def run_sql(self, sql: str) -> bool:
        """Run one or more statements. Returns False if any failed."""
        try:
            statements = split_statements(sql)
        except SQLError as e:
            self.report(e)
            return False
        for text in statements:
            start = time.perf_counter()
            try:
                result = self.db.execute(text)
            except Error as e:
                self.report(e)
                return False
            elapsed = time.perf_counter() - start
            self.show(result, text)
            if self.timer:
                self.printer.write(self.style.dim(f"run time: {elapsed * 1000:.2f} ms"))
        return True

    def show(self, result: Result, text: str) -> None:
        if result.columns is None:
            if result.rowcount > 0 and self.printer.mode in ("box", "table", "markdown"):
                verb = text.lstrip().split(None, 1)[0].upper()
                noun = "row" if result.rowcount == 1 else "rows"
                past = {"INSERT": "inserted", "UPDATE": "updated", "DELETE": "deleted", "REPLACE": "inserted"}.get(verb, "changed")
                self.printer.write(self.style.ok(f"{result.rowcount} {noun} {past}"))
            return
        if result.columns == ["id", "parent", "notused", "detail"] and text.lstrip().upper().startswith("EXPLAIN"):
            for line in plan_tree(result.rows):
                self.printer.write(self.style.accent(line) if line == "QUERY PLAN" else line)
            return
        if not result.rows and self.printer.mode in ("box", "table", "markdown"):
            if result.columns:
                self.printer.result(result.columns, [])
            return
        self.printer.result(result.columns, result.rows)

    def report(self, e: Exception) -> None:
        self.failed = True
        text = str(e)
        first, _, rest = text.partition("\n")
        self.err.write(self.style.err("error: " + first) + ("\n" + rest if rest else "") + "\n")

    def dot(self, line: str) -> bool:
        """Run a dot command. Returns False for .quit."""
        import shlex

        try:
            parts = shlex.split(line[1:], posix=os.name != "nt")
        except ValueError as e:
            self.report(Error(f"cannot parse the command: {e}"))
            return True
        if not parts:
            return True
        name, args = parts[0].lower(), parts[1:]
        if name in ("quit", "exit", "q"):
            return False
        fn = self.commands.get(name)
        if fn is None:
            self.report(Error(f"unknown command .{name}: type .help to see the commands"))
            return True
        try:
            fn(args)
        except Error as e:
            self.report(e)
        except OSError as e:
            self.report(Error(f"{e.strerror or e}: {getattr(e, 'filename', '')}"))
        return True

    # ------------------------------------------------------------ dot commands

    def cmd_help(self, args: list[str]) -> None:
        self.printer.write(HELP)

    def cmd_tables(self, args: list[str]) -> None:
        import fnmatch

        names = sorted([t.name for t in self.db.catalog.tables.values()] + [v.name for v in self.db.catalog.views.values()], key=str.lower)
        if args:
            pat = args[0].replace("%", "*")
            names = [n for n in names if fnmatch.fnmatch(n.lower(), pat.lower())]
        if names:
            self.printer.write("  ".join(names))

    def cmd_schema(self, args: list[str]) -> None:
        want = args[0].lower() if args else None
        cat = self.db.catalog
        for t in cat.tables.values():
            if want and t.name.lower() != want:
                continue
            self.printer.write(t.sql + ";")
            for ix in t.indexes:
                if ix.sql:
                    self.printer.write(ix.sql + ";")
        for v in cat.views.values():
            if not want or v.name.lower() == want:
                self.printer.write(v.sql + ";")

    def cmd_indexes(self, args: list[str]) -> None:
        rows = []
        for t in self.db.catalog.tables.values():
            if args and t.name.lower() != args[0].lower():
                continue
            for ix in t.indexes:
                rows.append((ix.name, t.name, ", ".join(ix.columns), "yes" if ix.unique else "no"))
        self.printer.result(["index", "table", "columns", "unique"], rows)

    def cmd_mode(self, args: list[str]) -> None:
        if not args:
            self.printer.write(f"current mode: {self.printer.mode}")
            return
        mode = args[0].lower()
        if mode == "column":
            mode = "table"
        if mode not in MODES:
            raise Error(f"unknown mode {mode!r}: use {', '.join(MODES)}")
        self.printer.mode = mode

    def cmd_headers(self, args: list[str]) -> None:
        self.printer.headers = not args or args[0].lower() in ("on", "1", "yes", "true")

    def cmd_nullvalue(self, args: list[str]) -> None:
        self.printer.null = args[0] if args else None

    def cmd_timer(self, args: list[str]) -> None:
        self.timer = not args or args[0].lower() in ("on", "1", "yes", "true")

    def cmd_plan(self, args: list[str]) -> None:
        if not args:
            raise Error("usage: .plan SELECT ...")
        result = self.db.execute("EXPLAIN QUERY PLAN " + " ".join(args).rstrip(";"))
        for line in plan_tree(result.rows):
            self.printer.write(line)

    def cmd_import(self, args: list[str]) -> None:
        if len(args) != 2:
            raise Error("usage: .import FILE TABLE")
        path, table = args
        with open(path, newline="", encoding="utf-8-sig") as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel  # type: ignore[assignment]
            rows = list(csv.reader(f, dialect))
        if not rows:
            raise Error(f"{path} is empty")
        from .engine.catalog import quote_ident

        header, data = rows[0], rows[1:]
        if self.db.catalog.table(table) is None:
            cols = ", ".join(
                f"{quote_ident(h or f'c{i + 1}')} {_guess_type([r[i] for r in data if i < len(r)])}" for i, h in enumerate(header)
            )
            self.db.execute(f"CREATE TABLE {quote_ident(table)} ({cols})")
        else:
            data = rows
        width = len(self.db.catalog.table(table).columns)  # type: ignore[union-attr]
        marks = ", ".join("?" * width)
        own = not self.db.in_transaction
        if own:
            self.db.execute("BEGIN")
        try:
            for r in data:
                r = (r + [None] * width)[:width]  # type: ignore[list-item]
                self.db.execute(f"INSERT INTO {quote_ident(table)} VALUES ({marks})", [None if v == "" else v for v in r])
        except BaseException:
            if own:
                self.db.rollback()
            raise
        if own:
            self.db.commit()
        self.printer.write(self.style.ok(f"imported {len(data)} rows into {table}"))

    def cmd_export(self, args: list[str]) -> None:
        if len(args) < 2:
            raise Error("usage: .export FILE QUERY")
        path, query = args[0], " ".join(args[1:])
        result = self.db.execute(query.rstrip(";"))
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(result.columns or [])
            for r in result.rows:
                w.writerow(["" if v is None else render_value(v) for v in r])
        self.printer.write(self.style.ok(f"wrote {len(result.rows)} rows to {path}"))

    def cmd_dump(self, args: list[str]) -> None:
        lines = list(self.db.dump())
        if args:
            with open(args[0], "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            self.printer.write(self.style.ok(f"wrote {args[0]}"))
        else:
            for line in lines:
                self.printer.write(line)

    def cmd_read(self, args: list[str]) -> None:
        if not args:
            raise Error("usage: .read FILE")
        with open(args[0], encoding="utf-8") as f:
            self.run_script(f.read())

    def cmd_open(self, args: list[str]) -> None:
        if not args:
            raise Error("usage: .open FILE")
        self.db.close()
        self.path = args[0]
        self.db = Database(self.path)

    def cmd_check(self, args: list[str]) -> None:
        problems = self.db.integrity_check()
        if problems:
            for p in problems:
                self.printer.write(self.style.err(p))
        else:
            self.printer.write(self.style.ok("ok: every page, row and index entry checks out"))

    def cmd_stats(self, args: list[str]) -> None:
        h = self.db.pager.header
        size = h.page_count * self.db.pager.page_size
        rows = [
            ("file", self.db.path),
            ("page size", f"{self.db.pager.page_size} bytes"),
            ("pages", f"{h.page_count} ({size / 1024:.1f} KiB)"),
            ("free pages", str(h.free_count)),
            ("log frames", str(self.db.pager.backend.wal_frames())),
            ("tables", str(len(self.db.catalog.tables))),
            ("indexes", str(len(self.db.catalog.indexes))),
            ("views", str(len(self.db.catalog.views))),
            ("commits", str(h.change_counter)),
        ]
        for t in self.db.catalog.tables.values():
            from .storage.btree import BTree

            rows.append((f"rows in {t.name}", str(BTree(self.db.pager, t.root).count())))
        self.printer.result(["", "value"], rows)

    # ------------------------------------------------------------ input loops

    def run_script(self, text: str) -> None:
        buffer: list[str] = []
        for line in text.splitlines():
            if not buffer and (not line.strip() or line.lstrip().startswith("--")):
                continue  # blank lines and comments between statements
            if not buffer and line.lstrip().startswith("."):
                if not self.dot(line.strip()):
                    return
                continue
            buffer.append(line)
            joined = "\n".join(buffer)
            if _complete(joined):
                self.run_sql(joined)
                buffer = []
        if buffer and "\n".join(buffer).strip():
            self.run_sql("\n".join(buffer))

    def interactive(self) -> None:
        try:
            import readline  # noqa: F401  (gives line editing and history where available)

            history = os.path.join(os.path.expanduser("~"), ".quarry_history")
            try:
                readline.read_history_file(history)
            except OSError:
                pass
            import atexit

            atexit.register(lambda: _save_history(readline, history))
        except ImportError:
            pass
        s = self.style
        self.printer.write(f"{s.accent('quarry')} {__version__}  {s.dim('·')}  {self.path}")
        self.printer.write(s.dim('Enter SQL ending in ";" or .help for commands. Created by jullewz.'))
        buffer: list[str] = []
        while True:
            prompt = s.accent("quarry> ") if not buffer else s.dim("   ...> ")
            try:
                line = input(prompt)
            except EOFError:
                self.printer.write()
                break
            except KeyboardInterrupt:
                self.printer.write()
                buffer = []
                continue
            if not buffer and line.lstrip().startswith("."):
                if not self.dot(line.strip()):
                    break
                continue
            buffer.append(line)
            joined = "\n".join(buffer)
            if _complete(joined):
                self.run_sql(joined)
                buffer = []

    def close(self) -> None:
        self.db.close()


def _save_history(readline: Any, path: str) -> None:
    try:
        readline.set_history_length(2000)
        readline.write_history_file(path)
    except OSError:
        pass


def _complete(text: str) -> bool:
    """A statement is complete when it ends with a semicolon outside quotes and comments."""
    if not text.strip():
        return False
    try:
        tokens = tokenize(text)
    except SQLError as e:
        return "unterminated" not in e.message
    real = [t for t in tokens if t.kind != "eof"]
    return bool(real) and real[-1].kind == "op" and real[-1].value == ";"


def _guess_type(values: Sequence[str]) -> str:
    vals = [v for v in values[:500] if v != ""]
    if not vals:
        return "TEXT"

    def is_int(v: str) -> bool:
        try:
            int(v)
            return True
        except ValueError:
            return False

    def is_real(v: str) -> bool:
        try:
            float(v)
            return True
        except ValueError:
            return False

    if all(is_int(v) for v in vals):
        return "INTEGER"
    if all(is_real(v) for v in vals):
        return "REAL"
    return "TEXT"


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(
        prog="quarry", description="Quarry: a SQL database engine written from scratch in Python. Created by jullewz."
    )
    p.add_argument(
        "database", nargs="?", default=":memory:", help="database file (created if missing); default: a temporary in-memory database"
    )
    p.add_argument("sql", nargs="*", help="SQL to run instead of starting the shell")
    p.add_argument("-m", "--mode", choices=MODES, help="output mode")
    p.add_argument("--no-header", action="store_true", help="leave out column names in csv, tsv and list output")
    p.add_argument("--no-color", action="store_true", help="plain output without colours")
    p.add_argument("-v", "--version", action="version", version=f"quarry {__version__}")
    args = p.parse_intermixed_args(argv)
    try:
        shell = Shell(args.database, color=False if args.no_color else None)
    except Error as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    if args.mode:
        shell.printer.mode = args.mode
    if args.no_header:
        shell.printer.headers = False
    try:
        if args.sql:
            for text in args.sql:
                if text.lstrip().startswith("."):
                    shell.dot(text.strip())
                else:
                    shell.run_sql(text)
        elif not sys.stdin.isatty():
            if not args.mode:
                shell.printer.mode = "box" if sys.stdout.isatty() else "list"
            shell.run_script(sys.stdin.read())
        else:
            shell.interactive()
    except BrokenPipeError:  # the reader went away (for example: quarry ... | head)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0
    finally:
        shell.close()
    return 1 if shell.failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
