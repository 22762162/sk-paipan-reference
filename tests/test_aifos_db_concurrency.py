"""SQLite connections used by parallel AIFOS image workers."""

from concurrent.futures import ThreadPoolExecutor

from aifos.db import Database
from aifos.project_center import ProjectCenter


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


def test_parallel_document_versions_are_allocated_atomically(tmp_path):
    path = tmp_path / "documents.db"
    db = Database(path)
    projects = ProjectCenter(db)
    project, _ = projects.get_or_create_project("并行版本")
    episode, _ = projects.get_or_create_episode(project["id"], 1)

    def save(index):
        worker = Database(path)
        try:
            return ProjectCenter(worker).save_document(
                episode["id"], "quality_policy", {"worker": index})
        finally:
            worker.close()

    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            versions = list(pool.map(save, range(36)))
        assert sorted(versions) == list(range(1, 37))
        rows = db.query(
            "SELECT version FROM documents WHERE episode_id=? AND kind=? "
            "ORDER BY version",
            (episode["id"], "quality_policy"))
        assert [row["version"] for row in rows] == list(range(1, 37))
    finally:
        db.close()
