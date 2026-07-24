"""IP 资产中心:角色/场景/动作/镜头/Prompt/首尾帧/图片/视频/配音等资产的
沉淀、版本管理与复用。"""

import json

from .db import now

KINDS = (
    "character", "scene", "action", "shot", "prompt",
    "first_frame", "last_frame", "image", "video", "voice",
    "cover", "title", "clip", "edit",
)

# 资产中心里可直接预览、筛选与软删除的图片类型。人物身份锚点通常与
# character_art 指向同一文件，不单独展示，避免用户看到重复卡片。
IMAGE_KINDS = (
    "character_candidate", "character_art", "character_sheet",
    "scene_art", "image", "first_frame", "last_frame", "cover",
    "reference", "spatial_blocking",
)


class AssetCenter:
    def __init__(self, db, on_registered=None):
        self.db = db
        self.on_registered = on_registered

    def _registered(self, row):
        """通知非关键旁路；镜像失败绝不能让正式资产登记失败。"""
        if row is not None and self.on_registered is not None:
            try:
                self.on_registered(row)
            except Exception:
                pass
        return row

    def acquire(self, project_id, kind, name, uri="", meta=None):
        """复用优先:已有同名资产直接复用(reuse_count+1),否则登记 v1。

        返回 (row, reused)。
        """
        row = self.latest(project_id, kind, name, include_deleted=True)
        if row is not None and not self.is_deleted(row):
            self.db.execute(
                "UPDATE assets SET reuse_count = reuse_count + 1 WHERE id=?",
                (row["id"],))
            return self.db.query_one(
                "SELECT * FROM assets WHERE id=?", (row["id"],)), True
        return self.register(
            project_id, kind, name, uri=uri, meta=meta,
            new_version=row is not None), False

    def register(self, project_id, kind, name, uri="", meta=None,
                 new_version=False):
        """登记资产;new_version=True 时在已有资产上叠加新版本。"""
        latest = self.latest(
            project_id, kind, name, include_deleted=True)
        if latest is not None and self.is_deleted(latest):
            # 删除采用墓碑版本；同名资产再次生产时必须另起新版本，
            # 不能覆盖删除记录和它之前的历史版本。
            new_version = True
        if latest is not None and not new_version:
            # 同名资产重复登记视为更新最新版本的 uri/meta
            self.db.execute(
                "UPDATE assets SET uri=?, meta=? WHERE id=?",
                (uri or latest["uri"],
                 json.dumps(meta, ensure_ascii=False) if meta is not None
                 else latest["meta"],
                 latest["id"]))
            return self._registered(self.db.query_one(
                "SELECT * FROM assets WHERE id=?", (latest["id"],)))
        version = (latest["version"] + 1) if latest is not None else 1
        self.db.execute(
            "INSERT INTO assets(project_id, kind, name, version, uri, meta, "
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (project_id, kind, name, version, uri,
             json.dumps(meta or {}, ensure_ascii=False), now()),
        )
        return self._registered(self.db.query_one(
            "SELECT * FROM assets WHERE project_id=? AND kind=? AND name=? "
            "AND version=?", (project_id, kind, name, version)))

    def latest(self, project_id, kind, name, include_deleted=False):
        row = self.db.query_one(
            "SELECT * FROM assets WHERE project_id=? AND kind=? AND name=? "
            "ORDER BY version DESC LIMIT 1", (project_id, kind, name))
        if row is not None and self.is_deleted(row) and not include_deleted:
            return None
        return row

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

    @staticmethod
    def meta(row):
        if row is None:
            return {}
        value = row["meta"]
        if isinstance(value, str):
            try:
                return json.loads(value or "{}")
            except ValueError:
                return {}
        return value or {}

    @classmethod
    def is_deleted(cls, row):
        return bool(cls.meta(row).get("deleted"))

    def active_list(self, project_id, kind=None):
        """每个 kind/name 只返回当前未删除的最新版本。"""
        rows = self.list(project_id, kind=kind)
        latest = {}
        for row in rows:
            latest[(row["kind"], row["name"])] = row
        return [row for row in latest.values() if not self.is_deleted(row)]

    def get(self, asset_id):
        return self.db.query_one(
            "SELECT * FROM assets WHERE id=?", (int(asset_id),))

    def soft_delete(self, project_id, kind, name, meta=None):
        """以墓碑新版本隐藏当前资产；文件和历史版本保持可恢复。"""
        latest = self.latest(
            project_id, kind, name, include_deleted=True)
        if latest is None or self.is_deleted(latest):
            return None
        tombstone = {
            "deleted": True,
            "deleted_at": now(),
            "source_asset_id": latest["id"],
            "source_version": latest["version"],
        }
        if meta:
            tombstone.update(meta)
        return self.register(
            project_id, kind, name, uri="", meta=tombstone,
            new_version=True)

    def mark_superseded(self, asset_id, replacement_asset_id,
                        reason="revision"):
        """标记已被修正版替代的旧版本，但保留历史和原文件。

        修正版已经通过 ``register(..., new_version=True)`` 成为同名资产的
        最新版本，旧版本不应再被当前资产面板或参考图选择器误用。这里不
        写删除墓碑（否则会把修正版一起遮蔽），而是在旧行上写入替代关系，
        既能隐藏旧图的使用意图，又保留审计、回溯和重新导出能力。
        """
        old = self.get(asset_id)
        if old is None:
            return None
        try:
            old_id = int(old["id"])
            replacement_id = int(replacement_asset_id)
        except (TypeError, ValueError):
            return old
        if old_id == replacement_id:
            return old
        metadata = self.meta(old)
        metadata.update({
            "superseded": True,
            "superseded_at": now(),
            "superseded_by_asset_id": replacement_id,
            "superseded_reason": str(reason or "revision"),
        })
        self.db.execute(
            "UPDATE assets SET meta=? WHERE id=?",
            (json.dumps(metadata, ensure_ascii=False), old_id),
        )
        return self.get(old_id)

    def delete(self, project_id, kind, name):
        """兼容旧调用：作废资产但不物理删除文件或历史。"""
        return self.soft_delete(project_id, kind, name)

    def stats(self, project_id):
        return self.db.query(
            "SELECT kind, COUNT(*) AS total, SUM(reuse_count) AS reused "
            "FROM assets WHERE project_id=? GROUP BY kind ORDER BY kind",
            (project_id,))
