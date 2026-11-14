"""A tour of the Python API: tables, constraints, transactions, upserts, joins, windows and EXPLAIN.

Run:  python examples/bookstore.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import quarry  # noqa: E402


def main() -> None:
    path = os.path.join(tempfile.mkdtemp(), "bookstore.qdb")
    with quarry.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, country TEXT);
            CREATE TABLE books (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                author_id INTEGER NOT NULL REFERENCES authors(id),
                year INTEGER CHECK (year > 1400),
                price REAL NOT NULL CHECK (price >= 0),
                stock INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX books_author ON books(author_id);
            """
        )
        db.executemany(
            "INSERT INTO authors (name, country) VALUES (?, ?)",
            [("Tove Jansson", "Finland"), ("Ursula K. Le Guin", "USA"), ("Stanisław Lem", "Poland")],
        )
        db.executemany(
            "INSERT INTO books (title, author_id, year, price, stock) VALUES (?, ?, ?, ?, ?)",
            [
                ("Finn Family Moomintroll", 1, 1948, 12.90, 4),
                ("The Summer Book", 1, 1972, 14.50, 2),
                ("A Wizard of Earthsea", 2, 1968, 11.00, 7),
                ("The Left Hand of Darkness", 2, 1969, 13.25, 0),
                ("The Dispossessed", 2, 1974, 15.00, 3),
                ("Solaris", 3, 1961, 10.75, 5),
                ("The Cyberiad", 3, 1965, 12.00, 1),
            ],
        )
    db.close()  # the with-block committed; like sqlite3, it does not close the connection
    db = quarry.connect(path)  # reopen to show the data is on disk
    db.row_factory = quarry.Row

    print("Books per author, with total stock value:")
    for row in db.execute(
        """
        SELECT a.name, count(*) AS books, round(sum(b.price * b.stock), 2) AS stock_value
        FROM authors a JOIN books b ON b.author_id = a.id
        GROUP BY a.id ORDER BY stock_value DESC
        """
    ):
        print(f"  {row['name']:<20} {row['books']} books  {row['stock_value']:>7.2f}")

    print("\nEach author's books ranked by price (a window function):")
    for title, name, rank in db.execute(
        "SELECT title, (SELECT name FROM authors WHERE id = author_id), rank() OVER (PARTITION BY author_id ORDER BY price DESC) FROM books ORDER BY author_id, 3"
    ):
        print(f"  {rank}. {title} ({name})")

    print("\nA sale and a restock in one transaction, as an upsert:")
    db.execute("CREATE TABLE sales (book_id INTEGER PRIMARY KEY, sold INTEGER NOT NULL)")
    for book_id in (3, 3, 6):
        db.execute(
            "INSERT INTO sales VALUES (?, 1) ON CONFLICT (book_id) DO UPDATE SET sold = sold + 1",
            (book_id,),
        )
        db.execute("UPDATE books SET stock = stock - 1 WHERE id = ?", (book_id,))
    db.commit()
    print("  ", db.execute("SELECT book_id, sold FROM sales ORDER BY book_id").fetchall())

    print("\nConstraints protect the data:")
    try:
        db.execute("INSERT INTO books (title, author_id, year, price) VALUES ('Free book?', 1, 2020, -5)")
    except quarry.IntegrityError as e:
        print("  IntegrityError:", e)
    db.rollback()

    print("\nHow Quarry runs a lookup by author:")
    for _id, _parent, _x, detail in db.execute("EXPLAIN QUERY PLAN SELECT title FROM books WHERE author_id = 2 AND price < 14"):
        print("  ", detail)

    print("\nIntegrity check:", db.execute("PRAGMA integrity_check").fetchone()[0])
    db.close()


if __name__ == "__main__":
    main()
