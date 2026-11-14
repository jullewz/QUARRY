"""Kill a writing process at random moments and check that the database survives every time.

Each round starts a child process that inserts rows in small transactions as fast as it can, then kills it
without warning (no cleanup, no close). The parent reopens the file and checks:

* PRAGMA integrity_check passes
* the rows form an unbroken sequence 1..N: every committed transaction is there, whole, and nothing else

Run:  python examples/crash_test.py [rounds]
Works on Linux, macOS and Windows.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)

import quarry  # noqa: E402

WRITER = r"""
import sys
sys.path.insert(0, sys.argv[2])
import quarry
db = quarry.connect(sys.argv[1], autocommit=True)
db.execute("CREATE TABLE IF NOT EXISTS log (n INTEGER PRIMARY KEY, payload TEXT)")
n = db.execute("SELECT coalesce(max(n), 0) FROM log").fetchone()[0]
print("ready", flush=True)
while True:
    db.execute("BEGIN")
    for _ in range(5):  # five rows per transaction: a crash must never leave a partial batch
        n += 1
        db.execute("INSERT INTO log VALUES (?, ?)", (n, "x" * (n % 700)))
    db.execute("COMMIT")
"""


def main() -> None:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    rnd = random.Random()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "crash.qdb")
        last = 0
        for r in range(1, rounds + 1):
            child = subprocess.Popen([sys.executable, "-c", WRITER, path, SRC], stdout=subprocess.PIPE, text=True)
            assert child.stdout is not None
            child.stdout.readline()  # wait until it is writing
            time.sleep(rnd.uniform(0.05, 0.6))
            child.kill()
            child.wait()
            db = quarry.connect(path)
            check = db.execute("PRAGMA integrity_check").fetchone()[0]
            count, top = db.execute("SELECT count(*), coalesce(max(n), 0) FROM log").fetchone()
            db.close()
            whole = count == top and top % 5 == 0
            status = "ok" if check == "ok" and whole and top >= last else "FAILED"
            print(f"round {r:2}: killed the writer, {top:6} rows survive, integrity {check}, batches whole: {whole}  [{status}]")
            if status != "ok":
                sys.exit(1)
            last = top
    print("every committed transaction survived every crash, and no partial one did")


if __name__ == "__main__":
    main()
