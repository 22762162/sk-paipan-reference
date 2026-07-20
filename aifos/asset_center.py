"""IP 资产中心:角色/场景/动作/镜头/Prompt/首尾帧/图片/视频/配音等资产的
沉淀、版本管理与复用。"""

import json

from .db import now

KINDS = (
    "character", "scene", "action", "shot", "prompt",
    "first_frame", "last_frame", "image", "video", "voice",
    "cover", "title", "clip", "edit",
)


class AssetCenter:
    def __init__(self, db):
        self.db = db

    def acquire(self, project_id, kind, name, uri="", meta=None):
        """复用优先:已有同名资产直接复用(reuse_count+1),否则登记 v1。

        返回 (row, reused)。
        """
        row = self.latest(project_id, kind, name)
        if row is not None:
            self.db.execute(
                "UPDATE assets SET reuse_count = reuse_count + 1 WHERE id=?",
                (row["id"],))
            return self.db.query_one(
                "SELECT * FROM assets WHERE id=?", (row["id"],)), True
        return self.register(project_id, kind, name, uri=uri, meta=meta), False

    def register(self, project_id, kind, name, uri="", meta=None,
                 new_version=False):
        """登记资产;new_version=True 时在已有资产上叠加新版本。"""
        latest = self.latest(project_id, kind, name)
        if latest is not None and not new_version:
            # 同名资产重复登记视为更新最新版本的 uri/meta
            self.db.execute(
                "UPDATE assets SET uri=?, meta=? WHERE id=?",
                (uri or latest["uri"],
                 json.dumps(meta, ensure_ascii=False) if meta is not None
                 else latest["meta"],
                 latest["id"]))
            return self.db.query_one(
                "SELECT * FROM assets WHERE id=?", (latest["id"],))
        version = (latest["version"] + 1) if latest is not None else 1
        self.db.execute(
            "INSERT INTO assets(project_id, kind, name, version, uri, meta, "
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (project_id, kind, name, version, uri,
             json.dumps(meta or {}, ensure_ascii=False), now()),
        )
        return self.db.query_one(
            "SELECT * FROM assets WHERE project_id=? AND kind=? AND name=? "
            "AND version=?", (project_id, kind, name, version))

    def latest(self, project_id, kind, name):
        return self.db.query_one(
            "SELECT * FROM assets WHERE project_id=? AND kind=? AND name=? "
            "ORDER BY version DESC LIMIT 1", (project_id, kind, name))

    def history(self, project_id, kind, name):
        return self.db.query(
            "SELECT * FROM assets WHERE project_id=? AND kind=? AND name=? "
            "ORDER BY version", (project_id, kind, name))

    def list(self, project_id, kind=None):
        if kind is None:
            return self.db.query(
                "SELECT * FROM assets WHERE project_id=? ORDER BY kind, name, "
                "version", (project_id,))
        return self.db.query(
            "SELECT * FROM assets WHERE project_id=? AND kind=? "
            "ORDER BY name, version", (project_id, kind))

    def delete(self, project_id, kind, name):
        """删除某资产全部版本(用于作废需重新生成的产物)。"""
        self.db.execute(
            "DELETE FROM assets WHERE project_id=? AND kind=? AND name=?",
            (project_id, kind, name))

    def stats(self, project_id):
        return self.db.query(
            "SELECT kind, COUNT(*) AS total, SUM(reuse_count) AS reused "
            "FROM assets WHERE project_id=? GROUP BY kind ORDER BY kind",
            (project_id,))
