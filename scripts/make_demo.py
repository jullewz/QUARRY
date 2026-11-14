#!/usr/bin/env python3
"""Render the README screenshots from real shell sessions.

Every line in the pictures is real output of the quarry shell against a small demo database.
Usage: python scripts/make_demo.py
"""

from __future__ import annotations

import io
import re
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from quarry.shell import Shell  # noqa: E402

ASSETS = ROOT / "docs" / "assets"
SGR = re.compile(r"\x1b\[([0-9;]*)m")
CHAR_W, LINE_H, PAD = 8.43, 20, 22
DEFAULT_FG = "#d4d4d8"


def xterm(n: int) -> str:
    base = ["#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
            "#666666", "#f14c4c", "#23d18b", "#f5f543", "#3b8eea", "#d670d6", "#29b8db", "#ffffff"]  # fmt: skip
    if n < 16:
        return base[n]
    if n >= 232:
        v = 8 + (n - 232) * 10
        return f"#{v:02x}{v:02x}{v:02x}"
    n -= 16
    steps = [0, 95, 135, 175, 215, 255]
    return f"#{steps[n // 36]:02x}{steps[(n // 6) % 6]:02x}{steps[n % 6]:02x}"


def ansi_to_svg(text: str, title: str) -> str:
    lines = text.rstrip("\n").split("\n")
    cols = max(len(SGR.sub("", line)) for line in lines)
    width = int(cols * CHAR_W + PAD * 2)
    height = int(len(lines) * LINE_H + PAD * 2 + 34)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">',
        "<style>text{font:13.5px 'JetBrains Mono','Cascadia Code',Menlo,Consolas,monospace;white-space:pre}</style>",
        f'<rect width="{width}" height="{height}" rx="12" fill="#0b0b0f" stroke="#26262d"/>',
        f'<rect width="{width}" height="34" rx="12" fill="#141419"/>',
        f'<rect y="22" width="{width}" height="12" fill="#141419"/>',
        '<circle cx="20" cy="17" r="5.5" fill="#3f3f46"/><circle cx="38" cy="17" r="5.5" fill="#3f3f46"/><circle cx="56" cy="17" r="5.5" fill="#3f3f46"/>',
        f'<text x="{width / 2}" y="21.5" fill="#71717a" text-anchor="middle">{escape(title)}</text>',
    ]
    y = 34 + PAD + 13
    for line in lines:
        col, fg, bold = 0, DEFAULT_FG, False
        spans: list[str] = []
        pos = 0
        for match in [*SGR.finditer(line), None]:
            end = match.start() if match else len(line)
            chunk = line[pos:end]
            if chunk:
                for k, ch in enumerate(chunk):  # one tspan per cell keeps box drawing aligned in any font
                    if ch == " ":
                        continue
                    x = PAD + (col + k) * CHAR_W
                    weight = ' font-weight="700"' if bold else ""
                    spans.append(f'<tspan x="{x:.1f}" fill="{fg}"{weight}>{escape(ch)}</tspan>')
                col += len(chunk)
            if match is None:
                break
            pos = match.end()
            codes = [int(c) for c in match.group(1).split(";") if c] or [0]
            i = 0
            while i < len(codes):
                c = codes[i]
                if c == 0:
                    fg, bold = DEFAULT_FG, False
                elif c == 1:
                    bold = True
                elif c == 38 and i + 2 < len(codes) and codes[i + 1] == 5:
                    fg = xterm(codes[i + 2])
                    i += 2
                i += 1
        if spans:
            out.append(f'<text y="{y}">{"".join(spans)}</text>')
        y += LINE_H
    out.append("</svg>")
    return "\n".join(out) + "\n"


SETUP = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INT, total REAL, placed TEXT);
CREATE INDEX orders_customer ON orders(customer_id);
INSERT INTO customers (name, city) VALUES ('Ada', 'Helsinki'), ('Linus', 'Espoo'), ('Grace', 'Tampere'), ('Alan', 'Helsinki');
INSERT INTO orders (customer_id, total, placed) VALUES
  (1, 120.50, '2026-09-02'), (1, 80.00, '2026-10-11'), (2, 42.25, '2026-10-12'),
  (3, 310.00, '2026-10-30'), (3, 15.75, '2026-11-01'), (1, 99.90, '2026-11-03');
"""


def session(commands: list[str]) -> str:
    out = io.StringIO()
    shell = Shell(":memory:", out=out, err=out, color=True)
    shell.printer.out = io.StringIO()  # keep the setup's own output out of the picture
    shell.run_script(SETUP)
    shell.printer.out = out
    for cmd in commands:
        out.write("\x1b[38;5;177mquarry>\x1b[0m " + cmd.replace("\n", "\n\x1b[38;5;245m   ...>\x1b[0m ") + "\n")
        shell.run_script(cmd)
        out.write("\n")
    shell.close()
    return out.getvalue().rstrip("\n") + "\n"


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    queries = session(
        [
            "SELECT c.name, count(*) AS orders, sum(o.total) AS spent,\n       rank() OVER (ORDER BY sum(o.total) DESC) AS rank\nFROM customers c JOIN orders o ON o.customer_id = c.id\nGROUP BY c.id ORDER BY rank;",
            "EXPLAIN QUERY PLAN SELECT * FROM customers c\nJOIN orders o ON o.customer_id = c.id WHERE c.id = 1;",
        ]
    )
    (ASSETS / "session.svg").write_text(ansi_to_svg(queries, "quarry shop.qdb"), encoding="utf-8")
    errors = session(
        [
            "SELECT nmae, city FROM customers;",
            "INSERT INTO customers (city) VALUES ('Oulu');",
            "WITH RECURSIVE days(d) AS (SELECT date('2026-11-01')\n  UNION ALL SELECT date(d, '+1 day') FROM days WHERE d < '2026-11-04')\nSELECT d, strftime('%w', d) AS weekday FROM days;",
        ]
    )
    (ASSETS / "errors.svg").write_text(ansi_to_svg(errors, "quarry shop.qdb"), encoding="utf-8")
    print("wrote", ASSETS / "session.svg", "and", ASSETS / "errors.svg")


if __name__ == "__main__":
    main()
