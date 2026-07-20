"""项目中心:项目、剧集、剧本、分镜、制作状态统一管理。"""

import json

from .db import now
from .errors import AifosError


class ProjectCenter:
    def __init__(self, db):
        self.db = db

    # ---- 项目(账号矩阵:一个项目 = 一个账号的内容线) ----
    def get_or_create_project(self, title, description="", style="",
                              kind="drama", account="", aspect=""):
        row = self.db.query_one(
            "SELECT * FROM projects WHERE title=?", (title,))
        if row is not None:
            return row, False
        self.db.execute(
            "INSERT INTO projects(title, description, style, kind, account, "
            "aspect, created_at) VALUES(?,?,?,?,?,?,?)",
            (title, description, style, kind, account, aspect, now()),
        )
        return self.db.query_one(
            "SELECT * FROM projects WHERE title=?", (title,)), True

    def update_project(self, title, **fields):
        allowed = {"description", "style", "kind", "account", "aspect",
                   "status"}
        updates = {k: v for k, v in fields.items()
                   if k in allowed and v is not None}
        if not updates:
            return self.get_project(title)
        assignments = ", ".join(f"{k}=?" for k in updates)
        self.db.execute(
            f"UPDATE projects SET {assignments} WHERE title=?",
            (*updates.values(), title))
        return self.get_project(title)

    def rename_project(self, title, new_title):
        """项目改名:剧集/资产/产物都按 project_id 关联,改名不动数据。"""
        new_title = (new_title or "").strip()
        if not new_title:
            raise AifosError("新名字不能为空")
        row = self.get_project(title)
        if row is None:
            raise AifosError(f"项目不存在: {title}")
        if new_title == title:
            return row
        if self.get_project(new_title) is not None:
            raise AifosError(f"已存在同名项目: {new_title}")
        self.db.execute(
            "UPDATE projects SET title=? WHERE id=?", (new_title, row["id"]))
        return self.get_project(new_title)

    def get_project(self, title):
        return self.db.query_one("SELECT * FROM projects WHERE title=?", (title,))

    def list_projects(self):
        return self.db.query("SELECT * FROM projects ORDER BY id")

    # ---- 剧集 ----
    def get_or_create_episode(self, project_id, number, title="", premise=""):
        row = self.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=?",
            (project_id, number))
        if row is not None:
            return row, False
        ts = now()
        self.db.execute(
            "INSERT INTO episodes(project_id, number, title, premise, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (project_id, number, title, premise, ts, ts),
        )
        return self.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=?",
            (project_id, number)), True

    def get_episode(self, episode_id):
        return self.db.query_one(
            "SELECT * FROM episodes WHERE id=?", (episode_id,))

    def list_episodes(self, project_id):
        return self.db.query(
            "SELECT * FROM episodes WHERE project_id=? ORDER BY number",
            (project_id,))

    def set_episode_status(self, episode_id, status):
        self.db.execute(
            "UPDATE episodes SET status=?, updated_at=? WHERE id=?",
            (status, now(), episode_id))

    def add_episode_cost(self, episode_id, delta):
        self.db.execute(
            "UPDATE episodes SET cost = cost + ?, updated_at=? WHERE id=?",
            (delta, now(), episode_id))

    def set_qc_score(self, episode_id, score):
        self.db.execute(
            "UPDATE episodes SET qc_score=?, updated_at=? WHERE id=?",
            (score, now(), episode_id))

    # ---- 剧本 / 分镜文档(版本化) ----
    def save_document(self, episode_id, kind, content):
        row = self.db.query_one(
            "SELECT MAX(version) AS v FROM documents "
            "WHERE episode_id=? AND kind=?", (episode_id, kind))
        version = (row["v"] or 0) + 1
        self.db.execute(
            "INSERT INTO documents(episode_id, kind, version, content, "
            "created_at) VALUES(?,?,?,?,?)",
            (episode_id, kind, version,
             json.dumps(content, ensure_ascii=False), now()),
        )
        return version

    def latest_document(self, episode_id, kind):
        row = self.db.query_one(
            "SELECT * FROM documents WHERE episode_id=? AND kind=? "
            "ORDER BY version DESC LIMIT 1", (episode_id, kind))
        if row is None:
            return None, 0
        return json.loads(row["content"]), row["version"]

    # ---- 制作状态看板 ----
    def status_board(self):
        return self.db.query(
            "SELECT p.title AS project, e.number, e.title, e.status, "
            "e.qc_score, e.cost "
            "FROM episodes e JOIN projects p ON p.id = e.project_id "
            "ORDER BY p.id, e.number")
