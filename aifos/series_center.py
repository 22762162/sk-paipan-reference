"""多集文档的持久串行队列。"""

import json

from .db import now
from .errors import AifosError
from .quality_policy import normalize_aspect


ACTIVE_EPISODE_STATUSES = {
    "created", "script", "awaiting_script", "continuity", "cast",
    "awaiting_cast", "storyboard", "blocking", "images", "text_assets",
    "frames", "preflight", "awaiting_confirm", "videos", "voices", "edit",
    "qc", "package", "archive", "cancelling",
}


class SeriesCenter:
    def __init__(self, db):
        self.db = db

    def import_batch(self, parsed, *, style="", kind=None,
                     auto_advance=True, aspect=""):
        """原子建立项目、批次、各集与脚本文档；所有集先进入队列。"""
        title = str(parsed.get("project_title") or "").strip()
        episodes = list(parsed.get("episodes") or [])
        if not title or not episodes:
            raise AifosError("导入批次缺少作品名或剧集")
        numbers = [int(item["episode_number"]) for item in episodes]
        if len(numbers) != len(set(numbers)):
            raise AifosError("导入批次存在重复集数")
        timestamp = now()
        selected_aspect = (normalize_aspect(aspect, field="aspect")
                           if str(aspect or "").strip() else "")
        with self.db._lock:
            conn = self.db.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                project = conn.execute(
                    "SELECT * FROM projects WHERE title=?", (title,)).fetchone()
                if project is None:
                    cur = conn.execute(
                        "INSERT INTO projects(title, style, kind, aspect, "
                        "created_at) VALUES(?,?,?,?,?)",
                        (title, str(style or ""),
                         kind if kind in ("drama", "idol") else "drama",
                         selected_aspect,
                         timestamp))
                    project_id = cur.lastrowid
                else:
                    project_id = project["id"]
                    updates, params = [], []
                    if style and style != project["style"]:
                        updates.append("style=?")
                        params.append(str(style))
                    if kind in ("drama", "idol") and kind != project["kind"]:
                        updates.append("kind=?")
                        params.append(kind)
                    if selected_aspect and selected_aspect != project["aspect"]:
                        updates.append("aspect=?")
                        params.append(selected_aspect)
                    if updates:
                        conn.execute(
                            f"UPDATE projects SET {', '.join(updates)} WHERE id=?",
                            (*params, project_id))
                placeholders = ",".join("?" for _ in numbers)
                conflicts = conn.execute(
                    f"SELECT number FROM episodes WHERE project_id=? "
                    f"AND number IN ({placeholders}) ORDER BY number",
                    (project_id, *numbers)).fetchall()
                if conflicts:
                    joined = "、".join(f"第{row['number']}集" for row in conflicts)
                    raise AifosError(
                        f"以下剧集已经存在，未覆盖任何内容：{joined}；"
                        "请调整起始集数后重试")
                cur = conn.execute(
                    "INSERT INTO series_batches(project_id, filename, "
                    "source_format, source_sha256, total, auto_advance, "
                    "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (project_id, parsed.get("filename", ""),
                     parsed.get("source_format", "text"),
                     parsed.get("source_sha256", ""), len(episodes),
                     1 if auto_advance else 0, timestamp, timestamp))
                batch_id = cur.lastrowid
                episode_ids = []
                for position, item in enumerate(episodes, start=1):
                    script = item.get("script")
                    premise = ((script or {}).get("logline")
                               or item.get("source_text", "")[:1000])
                    cur = conn.execute(
                        "INSERT INTO episodes(project_id, number, title, premise, "
                        "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                        (project_id, int(item["episode_number"]),
                         item.get("title", ""), premise, "queued_script",
                         timestamp, timestamp))
                    episode_id = cur.lastrowid
                    episode_ids.append(episode_id)
                    if script is not None:
                        conn.execute(
                            "INSERT INTO documents(episode_id, kind, version, "
                            "content, created_at) VALUES(?,?,?,?,?)",
                            (episode_id, "script", 1,
                             json.dumps(script, ensure_ascii=False), timestamp))
                    source_doc = {
                        "batch_id": batch_id,
                        "position": position,
                        "total": len(episodes),
                        "filename": parsed.get("filename", ""),
                        "source_format": parsed.get("source_format", "text"),
                        "source_number": item.get("source_number"),
                        "source_title": item.get("title", ""),
                        "mode": item.get("mode", "script"),
                        "source_text": item.get("source_text", ""),
                    }
                    conn.execute(
                        "INSERT INTO documents(episode_id, kind, version, "
                        "content, created_at) VALUES(?,?,?,?,?)",
                        (episode_id, "series_source", 1,
                         json.dumps(source_doc, ensure_ascii=False), timestamp))
                    conn.execute(
                        "INSERT INTO series_batch_items(batch_id, episode_id, "
                        "position, source_number, source_title, mode, "
                        "source_text, created_at, updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (batch_id, episode_id, position,
                         item.get("source_number"), item.get("title", ""),
                         item.get("mode", "script"),
                         item.get("source_text", ""), timestamp, timestamp))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_batch(batch_id)

    def _rows(self, batch_id):
        return self.db.query(
            "SELECT i.*, e.number AS episode_number, e.title AS episode_title, "
            "e.premise, e.status AS episode_status, e.qc_score, e.cost, "
            "p.title AS project_title, p.id AS project_id "
            "FROM series_batch_items i "
            "JOIN episodes e ON e.id=i.episode_id "
            "JOIN projects p ON p.id=e.project_id "
            "WHERE i.batch_id=? ORDER BY i.position", (int(batch_id),))

    @staticmethod
    def _item_status(episode_status):
        if episode_status == "queued_script":
            return "queued"
        if episode_status == "done":
            return "done"
        if episode_status in ("failed", "qc_failed"):
            return "needs_attention"
        return "active"

    def _sync(self, batch_id):
        rows = self._rows(batch_id)
        timestamp = now()
        for row in rows:
            status = self._item_status(row["episode_status"])
            if status != row["status"]:
                self.db.execute(
                    "UPDATE series_batch_items SET status=?, updated_at=? "
                    "WHERE id=?", (status, timestamp, row["id"]))
        states = [self._item_status(row["episode_status"]) for row in rows]
        batch_status = ("done" if states and all(state == "done" for state in states)
                        else "needs_attention" if "needs_attention" in states
                        else "active")
        self.db.execute(
            "UPDATE series_batches SET status=?, updated_at=? WHERE id=?",
            (batch_status, timestamp, int(batch_id)))
        return self._rows(batch_id)

    def get_batch(self, batch_id):
        batch = self.db.query_one(
            "SELECT b.*, p.title AS project_title FROM series_batches b "
            "JOIN projects p ON p.id=b.project_id WHERE b.id=?",
            (int(batch_id),))
        if batch is None:
            return None
        rows = self._sync(batch_id)
        batch = dict(self.db.query_one(
            "SELECT b.*, p.title AS project_title FROM series_batches b "
            "JOIN projects p ON p.id=b.project_id WHERE b.id=?",
            (int(batch_id),)))
        items = []
        for row in rows:
            item = dict(row)
            source_text = item.pop("source_text", "")
            item["char_count"] = len(source_text)
            item["status"] = self._item_status(item["episode_status"])
            items.append(item)
        batch["auto_advance"] = bool(batch["auto_advance"])
        batch["items"] = items
        batch["completed"] = sum(item["status"] == "done" for item in items)
        batch["current"] = next(
            (item for item in items if item["status"] == "active"), None)
        batch["next"] = next(
            (item for item in items if item["status"] == "queued"), None)
        return batch

    def list_batches(self, project_id=None):
        if project_id is None:
            rows = self.db.query(
                "SELECT id FROM series_batches ORDER BY created_at DESC")
        else:
            rows = self.db.query(
                "SELECT id FROM series_batches WHERE project_id=? "
                "ORDER BY created_at DESC", (int(project_id),))
        return [self.get_batch(row["id"]) for row in rows]

    def activate_next(self, batch_id):
        batch = self.get_batch(batch_id)
        if batch is None:
            raise AifosError("多集导入批次不存在")
        if batch["current"] is not None:
            current = batch["current"]
            raise AifosError(
                f"第{current['episode_number']}集尚未完成；"
                "请先通过本集剧本、人物、开拍和质检，再开始下一集")
        attention = next(
            (item for item in batch["items"]
             if item["status"] == "needs_attention"), None)
        if attention is not None:
            raise AifosError(
                f"第{attention['episode_number']}集需要处理失败或质检问题，"
                "修复完成后才能继续下一集")
        item = batch["next"]
        if item is None:
            return {"done": True, "batch": batch}
        status = "awaiting_script" if item["mode"] == "script" else "created"
        timestamp = now()
        self.db.execute(
            "UPDATE episodes SET status=?, updated_at=? WHERE id=?",
            (status, timestamp, item["episode_id"]))
        self.db.execute(
            "UPDATE series_batch_items SET status='active', updated_at=? "
            "WHERE id=?", (timestamp, item["id"]))
        return {
            "done": False,
            "batch_id": int(batch_id),
            "episode_id": item["episode_id"],
            "title": item["project_title"],
            "number": item["episode_number"],
            "episode_title": item["episode_title"],
            "premise": item["premise"],
            "mode": item["mode"],
            "batch": self.get_batch(batch_id),
        }

    def batch_for_episode(self, episode_id):
        row = self.db.query_one(
            "SELECT batch_id FROM series_batch_items WHERE episode_id=?",
            (int(episode_id),))
        return self.get_batch(row["batch_id"]) if row else None

    def maybe_auto_advance(self, episode_id):
        batch = self.batch_for_episode(episode_id)
        if batch is None or not batch["auto_advance"]:
            return None
        episode = next(
            (item for item in batch["items"]
             if item["episode_id"] == int(episode_id)), None)
        if episode is None or episode["episode_status"] != "done":
            return None
        return self.activate_next(batch["id"])

    def set_auto_advance(self, batch_id, enabled):
        if self.get_batch(batch_id) is None:
            raise AifosError("多集导入批次不存在")
        self.db.execute(
            "UPDATE series_batches SET auto_advance=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, now(), int(batch_id)))
        return self.get_batch(batch_id)
