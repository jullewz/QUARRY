# Contributing to Quarry

Thanks for helping. Bug reports, compatibility gaps, performance ideas and pull requests are all welcome.

## Setup

```bash
git clone https://github.com/jullewz/quarry.git
cd quarry
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

Quarry has no runtime dependencies and should stay that way. The development tools are `pytest` and `ruff`.

## Before a pull request

```bash
pytest
ruff check src tests examples scripts
ruff format --check src tests examples scripts
python examples/crash_test.py 10      # if you touched storage/
```

## The golden rule: match SQLite

When Quarry and SQLite disagree, Quarry is wrong, unless the difference is listed in
[docs/compatibility.md](docs/compatibility.md). For any change to SQL behaviour:

1. Add the query to `QUERIES` in `tests/test_against_sqlite.py`. That test runs it on both engines and compares
   values and types.
2. Run the wider fuzzer to look for regressions: `python scripts/fuzz.py 1 2 3` compares 4,200 random queries.
   A few differences involving huge REALs turned into text are expected (see the compatibility notes).
3. Mention any new difference in `docs/compatibility.md`.

## Where things live

| Change | Files |
| --- | --- |
| New syntax | `sql/ast.py`, `sql/parser.py`, then the engine |
| New function | `engine/functions.py` (scalar: `@scalar`; aggregate: `AGGREGATES`) |
| Planner or executor | `engine/query.py`; expressions in `engine/expr.py` |
| Writes, DDL, pragmas, transactions | `engine/database.py`, `engine/catalog.py` |
| Pages, tree, log | `storage/` (read [docs/file-format.md](docs/file-format.md) first) |

## Storage rules

- Call `pager.mark_dirty(pgno)` **before** changing any page. Savepoints and rollback depend on it.
- Never write to the main database file outside a checkpoint.
- A change to the on-disk format needs a new magic string and an entry in `docs/file-format.md`.

## Style

- Python 3.11+, type hints, `ruff` formatting with a 140-character line limit.
- Error messages say what went wrong and, when possible, how to fix it.
- Keep functions small enough to read in one screen where you can.

## Reporting bugs

Use the issue form. The most useful report is a short SQL script that gives a different answer in Quarry and
in SQLite.
