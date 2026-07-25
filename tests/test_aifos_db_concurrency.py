"""SQLite connections used by parallel AIFOS image workers."""

from concurrent.futures import ThreadPoolExecutor

from aifos.db import Database


def test_parallel_connections_do_not_keep_hidden_write_transactions(tmp_path):
    path = tmp_path / "parallel.db"

    def write(index):
        db = Database(path)
        try:
            db.execute(
                "INSERT INTO logs(ts, level, source, message) "
                "VALUES(?, 'INFO', 'test', ?)",
                (float(index), f"worker-{index}"))
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(write, range(48)))

    db = Database(path)
    try:
        row = db.query_one("SELECT COUNT(*) AS n FROM logs")
        assert row["n"] == 48
    finally:
        db.close()
