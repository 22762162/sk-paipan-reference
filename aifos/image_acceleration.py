"""尚未开工图片的调度内 API 加速状态。

本模块只保存/认领派发契约，不直接启动第二条生产线。真正的 Provider 调用
仍由原 Director worker 完成，因而资产登记、成本、暂停和阶段状态只有一个
写入者，不会与主生产任务重复生成同一张图。
"""

from __future__ import annotations

import json
import time

from .errors import AifosError


class ImageAccelerationStore:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _dump(value):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))

    @staticmethod
    def _row(row):
        if row is None:
            return None
        value = dict(row)
        try:
            value["contract"] = json.loads(value.get("contract") or "{}")
        except (TypeError, ValueError):
            value["contract"] = {}
        value["never_started"] = bool(value.get("never_started"))
        return value

    def register(self, episode_id, records):
        """登记当前 stage 的真实 task payload 契约。

        同一契约已有尚未被 claim 的加速请求时保留；提示词/参考图发生变化
        就清空旧请求，强制用户重新预检。
        """
        timestamp = time.time()
        with self.db.transaction(immediate=True) as conn:
            for record in records:
                previous = conn.execute(
                    "SELECT * FROM image_dispatch_contracts "
                    "WHERE episode_id=? AND item_id=?",
                    (episode_id, record["item_id"])).fetchone()
                same = bool(
                    previous
                    and previous["contract_token"] == record["contract_token"]
                    and previous["production_state"] == "pending")
                keep_request = bool(
                    same and previous["acceleration_status"] == "queued")
                created_at = (previous["created_at"]
                              if previous is not None else timestamp)
                values = (
                    episode_id, record["item_id"], record["category"],
                    record["capability"], record["contract_token"],
                    self._dump(record["contract"]), "pending",
                    1 if record.get("never_started") else 0,
                    previous["acceleration_status"] if keep_request else "",
                    previous["requested_provider"] if keep_request else "",
                    previous["requested_model"] if keep_request else "",
                    previous["requested_quality"] if keep_request else "medium",
                    "", "", "", created_at, timestamp,
                )
                conn.execute(
                    "INSERT OR REPLACE INTO image_dispatch_contracts("
                    "episode_id,item_id,category,capability,contract_token,"
                    "contract,production_state,never_started,"
                    "acceleration_status,requested_provider,requested_model,"
                    "requested_quality,actual_provider,actual_model,error,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values)

    def list(self, episode_id):
        rows = self.db.query(
            "SELECT * FROM image_dispatch_contracts WHERE episode_id=? "
            "ORDER BY created_at,item_id", (episode_id,))
        return [self._row(row) for row in rows]

    def prune(self, episode_id, category, valid_item_ids):
        """Remove derived dispatch rows absent from the current full plan.

        The versioned storyboard, run logs and generated assets remain intact;
        only the active acceleration cache is pruned so a deleted/renumbered
        shot cannot re-enter production through an old dispatch contract.
        """
        valid = {str(item_id) for item_id in valid_item_ids}
        removed = []
        with self.db.transaction(immediate=True) as conn:
            rows = conn.execute(
                "SELECT item_id FROM image_dispatch_contracts "
                "WHERE episode_id=? AND category=?",
                (episode_id, category)).fetchall()
            for row in rows:
                item_id = str(row["item_id"])
                if item_id in valid:
                    continue
                conn.execute(
                    "DELETE FROM image_dispatch_contracts "
                    "WHERE episode_id=? AND item_id=?",
                    (episode_id, item_id))
                removed.append(item_id)
        return removed

    def get(self, episode_id, item_id):
        return self._row(self.db.query_one(
            "SELECT * FROM image_dispatch_contracts "
            "WHERE episode_id=? AND item_id=?", (episode_id, item_id)))

    def queue(self, episode_id, requests):
        """把已通过预检的条目全有或全无地标成 queued。"""
        timestamp = time.time()
        with self.db.transaction(immediate=True) as conn:
            rows = {}
            for request in requests:
                item_id = request["item_id"]
                row = conn.execute(
                    "SELECT * FROM image_dispatch_contracts "
                    "WHERE episode_id=? AND item_id=?",
                    (episode_id, item_id)).fetchone()
                if row is None:
                    raise AifosError(f"图片 {item_id} 尚未形成可派发契约")
                if row["contract_token"] != request["contract_token"]:
                    raise AifosError(f"图片 {item_id} 的提示词或参考图已变化，请重新预检")
                if row["production_state"] != "pending" \
                        or not bool(row["never_started"]):
                    raise AifosError(f"图片 {item_id} 已进入生产线，不能再切换 API")
                rows[item_id] = row
            for request in requests:
                conn.execute(
                    "UPDATE image_dispatch_contracts SET "
                    "acceleration_status='queued',requested_provider=?,"
                    "requested_model=?,requested_quality=?,error='',updated_at=? "
                    "WHERE episode_id=? AND item_id=?",
                    (request["provider"], request["model"],
                     request["quality"], timestamp, episode_id,
                     request["item_id"]))
        return [self._row(rows[request["item_id"]]) for request in requests]

    def claim(self, episode_id, item_id, contract_token):
        """原 Director 提交 worker 前原子 claim；返回选定 API 请求或 None。"""
        timestamp = time.time()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM image_dispatch_contracts "
                "WHERE episode_id=? AND item_id=?",
                (episode_id, item_id)).fetchone()
            if row is None or row["contract_token"] != contract_token:
                raise AifosError(f"图片 {item_id} 的派发契约不存在或已过期")
            if row["production_state"] != "pending":
                raise AifosError(f"图片 {item_id} 已被生产线认领")
            conn.execute(
                "UPDATE image_dispatch_contracts SET production_state='claimed',"
                "acceleration_status=CASE WHEN acceleration_status='queued' "
                "THEN 'running' ELSE acceleration_status END,updated_at=? "
                "WHERE episode_id=? AND item_id=?",
                (timestamp, episode_id, item_id))
            if row["acceleration_status"] != "queued":
                return None
            return {
                "provider": row["requested_provider"],
                "model": row["requested_model"],
                "quality": row["requested_quality"] or "medium",
                "contract_token": row["contract_token"],
            }

    def finish(self, episode_id, item_id, result=None, error=""):
        result = result or {}
        failed = bool(error)
        self.db.execute(
            "UPDATE image_dispatch_contracts SET production_state=?,"
            "acceleration_status=CASE WHEN acceleration_status='running' "
            "THEN ? ELSE acceleration_status END,actual_provider=?,"
            "actual_model=?,error=?,updated_at=? "
            "WHERE episode_id=? AND item_id=?",
            ("failed" if failed else "generated",
             "failed" if failed else "done",
             result.get("provider", ""), result.get("model", ""),
             str(error)[:500], time.time(), episode_id, item_id))
