"""Pages, the B+tree, the write-ahead log, crash recovery and savepoints."""

import os
import random
import shutil

import pytest

import quarry
from quarry.errors import CorruptDatabaseError, OperationalError
from quarry.storage.btree import BTree
from quarry.storage.encoding import decode_record, encode_record
from quarry.storage.pager import Pager


@pytest.mark.parametrize(
    "values",
    [
        (),
        (None,),
        (0, 1, -1, 2**63 - 1, -(2**63)),
        (1.5, -0.0, float("inf")),
        ("", "héllo", "\U0001f600"),
        (b"", b"\x00\xff"),
        (None, 1, "a", 2.5, b"x"),
    ],
)
def test_record_round_trip(values):
    data = encode_record(values)
    back, end = decode_record(data)
    assert back == values and end == len(data)


@pytest.mark.parametrize("page_size", [512, 4096, 16384])
def test_btree_matches_a_dict_under_random_inserts_and_deletes(page_size):
    pager = Pager(None, page_size=page_size)
    pager.begin()
    tree = BTree(pager, BTree.create(pager))
    ref = {}
    rnd = random.Random(page_size)
    for _ in range(6000):
        k = rnd.randrange(1500)
        if rnd.random() < 0.65:
            v = bytes([rnd.randrange(256)]) * rnd.choice([0, 5, 300, page_size])  # includes overflow values
            tree.insert((k,), v)
            ref[k] = v
        else:
            assert tree.delete((k,)) == (k in ref)
            ref.pop(k, None)
    assert tree.check() == []
    assert [(k[0], v) for k, v in tree.scan()] == sorted(ref.items())
    assert tree.count() == len(ref)
    for k in rnd.sample(range(1500), 50):
        assert tree.get((k,)) == ref.get(k)


def test_btree_range_and_prefix_scans():
    pager = Pager(None, page_size=512)
    pager.begin()
    tree = BTree(pager, BTree.create(pager))
    for i in range(2000):
        tree.insert((i % 10, i), b"")
    got = [k for k, _ in tree.scan((3,), True, (3,), True, prefix=True)]
    assert got == [(3, i) for i in range(3, 2000, 10)]
    got = [k for k, _ in tree.scan((7,), False, None, prefix=True)]
    assert {k[0] for k in got} == {8, 9}
    assert [k[1] for k, _ in tree.scan((2, 500), True, (2, 600), False)] == list(range(502, 600, 10))


def test_dropping_a_tree_returns_its_pages_to_the_free_list():
    pager = Pager(None, page_size=512)
    pager.begin()
    tree = BTree(pager, BTree.create(pager))
    for i in range(3000):
        tree.insert((i,), b"x" * 50)
    used = pager.header.page_count
    freed = tree.drop()
    assert pager.header.free_count == freed and freed >= used - 3
    t2 = BTree(pager, BTree.create(pager))
    for i in range(3000):
        t2.insert((i,), b"x" * 50)
    assert pager.header.page_count <= used + 2  # reused the freed pages


def test_savepoints_restore_pages():
    pager = Pager(None, page_size=512)
    pager.begin()
    tree = BTree(pager, BTree.create(pager))
    for i in range(100):
        tree.insert((i,), b"a")
    sp = pager.savepoint()
    for i in range(100, 2000):
        tree.insert((i,), b"b" * 20)
    for i in range(50):
        tree.delete((i,))
    pager.rollback_to(sp)
    assert [k[0] for k, _ in tree.scan()] == list(range(100))
    assert tree.check() == []
    pager.release(sp)
    pager.commit()


def test_data_survives_close_and_reopen(tmp_path):
    path = tmp_path / "keep.qdb"
    con = quarry.connect(path)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
    con.executemany("INSERT INTO t (body) VALUES (?)", [("row %d " % i * 20,) for i in range(500)])
    con.commit()
    con.close()
    assert not os.path.exists(str(path) + "-wal")  # checkpointed on close
    con = quarry.connect(path)
    assert con.execute("SELECT count(*), max(id) FROM t").fetchone() == (500, 500)
    assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    con.close()


def _crash_copy(src, dst):
    """Copy a database and its log while it is still open: what a crash leaves on disk."""
    shutil.copy(src, dst)
    if os.path.exists(str(src) + "-wal"):
        shutil.copy(str(src) + "-wal", str(dst) + "-wal")


def test_committed_transactions_survive_a_crash(tmp_path):
    path = tmp_path / "live.qdb"
    con = quarry.connect(path, autocommit=True)
    con.execute("CREATE TABLE t (n INTEGER)")
    for i in range(20):
        con.execute("INSERT INTO t VALUES (?)", (i,))
    con.execute("BEGIN")
    con.execute("INSERT INTO t VALUES (999)")  # not committed when the "crash" happens
    _crash_copy(path, tmp_path / "after.qdb")
    con.close()
    after = quarry.connect(tmp_path / "after.qdb")
    assert after.execute("SELECT count(*), max(n) FROM t").fetchone() == (20, 19)
    after.close()


def test_a_torn_log_write_loses_only_the_last_transaction(tmp_path):
    path = tmp_path / "torn.qdb"
    con = quarry.connect(path, autocommit=True)
    con.execute("CREATE TABLE t (n INTEGER)")
    con.execute("INSERT INTO t VALUES (1)")
    con.execute("INSERT INTO t VALUES (2)")
    _crash_copy(path, tmp_path / "copy.qdb")
    con.close()
    wal = str(tmp_path / "copy.qdb") + "-wal"
    size = os.path.getsize(wal)
    with open(wal, "r+b") as f:
        f.truncate(size - 100)  # the last frame of the last commit is half written
    after = quarry.connect(tmp_path / "copy.qdb")
    assert after.execute("SELECT n FROM t").fetchall() == [(1,)]
    assert after.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    after.close()


def test_a_corrupted_log_frame_is_ignored(tmp_path):
    path = tmp_path / "flip.qdb"
    con = quarry.connect(path, autocommit=True)
    con.execute("CREATE TABLE t (n INTEGER)")
    con.execute("INSERT INTO t VALUES (1)")
    con.execute("INSERT INTO t VALUES (2)")
    _crash_copy(path, tmp_path / "copy.qdb")
    con.close()
    wal = str(tmp_path / "copy.qdb") + "-wal"
    with open(wal, "r+b") as f:
        f.seek(os.path.getsize(wal) - 10)
        f.write(b"\xff\xfe\xfd")  # damage the data of the last frame
    after = quarry.connect(tmp_path / "copy.qdb")
    assert after.execute("SELECT n FROM t").fetchall() == [(1,)]
    after.close()


def test_a_second_process_cannot_open_the_same_file(tmp_path):
    path = tmp_path / "locked.qdb"
    first = quarry.connect(path)
    with pytest.raises(OperationalError, match="already open"):
        quarry.connect(path)
    first.close()
    quarry.connect(path).close()


def test_a_file_that_is_not_a_database_is_rejected(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"hello, this is a text file" * 200)
    with pytest.raises(CorruptDatabaseError):
        quarry.connect(path)


def test_large_values_use_overflow_pages_and_come_back_intact(tmp_path):
    con = quarry.connect(tmp_path / "big.qdb", autocommit=True)
    con.execute("CREATE TABLE blobs (id INTEGER PRIMARY KEY, data BLOB)")
    payloads = [os.urandom(n) for n in (10, 5000, 100_000, 1_000_000)]
    for p in payloads:
        con.execute("INSERT INTO blobs (data) VALUES (?)", (p,))
    con.close()
    con = quarry.connect(tmp_path / "big.qdb")
    assert [r[0] for r in con.execute("SELECT data FROM blobs ORDER BY id")] == payloads
    con.execute("DELETE FROM blobs WHERE length(data) > 1000")
    con.commit()
    assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    free = con.execute("PRAGMA freelist_count").fetchone()[0]
    assert free > 200
    con.execute("VACUUM")
    assert con.execute("PRAGMA freelist_count").fetchone()[0] == 0
    assert [r[0] for r in con.execute("SELECT data FROM blobs")] == payloads[:1]
    con.close()


def test_autocheckpoint_keeps_the_log_small(tmp_path):
    con = quarry.connect(tmp_path / "ckpt.qdb", autocommit=True)
    con.execute("CREATE TABLE t (x)")
    for i in range(1500):
        con.execute("INSERT INTO t VALUES (?)", (i,))
    frames = con.database.pager.backend.wal_frames()
    assert frames < 1000
    con.close()
