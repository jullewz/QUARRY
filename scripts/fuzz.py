"""Compare thousands of random queries between Quarry and SQLite.

Run:  python scripts/fuzz.py SEED [SEED ...]   (each seed generates 1,400 queries)
"""

import os
import sys

sys.path[:0] = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"),
]
from oracle import Oracle, same  # noqa: E402
from test_fuzz import generate  # noqa: E402

o = Oracle()
fails = 0
total = 0
errs = {}
for seed in map(int, sys.argv[1:] or ["1"]):
    for q in generate(1400, seed):
        ordered = q.endswith("-- ordered")
        q = q.removesuffix("-- ordered").strip()
        try:
            exp = o.ref.execute(q).fetchall()
        except Exception:
            continue
        total += 1
        try:
            got = o.db.execute(q).fetchall()
        except Exception as e:
            k = type(e).__name__ + ": " + str(e).splitlines()[0]
            errs[k] = errs.get(k, 0) + 1
            if errs[k] == 1:
                print("EXC", q[:300], k)
            continue
        if not same(exp, got, ordered):
            fails += 1
            if fails <= 6:
                print(q)
                if len(exp) == len(got):
                    for a, b in zip(sorted(exp, key=repr), sorted(got, key=repr)):
                        if a != b:
                            print("  sqlite", a, "\n  quarry", b)
                            break
                else:
                    print("  lengths", len(exp), len(got))
print(f"{total} queries compared, {fails} differences, errors: {errs or 'none'}")
