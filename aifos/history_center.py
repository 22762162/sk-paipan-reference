"""生产历史中心：持久记录每次运行，并恢复服务重启遗留的幽灵任务。"""

import json
import re

from .asset_center import AssetCenter, IMAGE_KINDS
from .db import now


STABLE_EPISODE_STATUSES = {
    "done", "failed", "qc_failed", "created",
    "awaiting_script", "awaiting_cast", "awaiting_confirm", "queued_script",
}


def _loads(value, fallback):
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


class HistoryCenter:
    """SQLite-backed production run history.

    JobRegistry is intentionally in-memory; this center is the durable record
    shown to operators after reloads and server restarts.
    """

    def __init__(self, db):
        self.db = db

    def create_run(self, project_title, episode_number, action="produce",
                   force=False, request=None, source="web"):
        episode = self.db.query_one(
            "SELECT e.id FROM episodes e JOIN projects p ON p.id=e.project_id "
            "WHERE p.title=? AND e.number=?",
            (project_title, int(episode_number)))
        ts = now()
        cur = self.db.execute(
            "INSERT INTO production_runs(episode_id, project_title, "
            "episode_number, action, source, status, force, request, "
            "started_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (episode["id"] if episode else None, project_title,
             int(episode_number), action, source, "running", bool(force),
             json.dumps(request or {}, ensure_ascii=False), ts, ts))
        return cur.lastrowid

    def _bind_episode(self, run_id):
        run = self.db.query_one(
            "SELECT * FROM production_runs WHERE id=?", (run_id,))
        if run is None or run["episode_id"] is not None:
            return run
        episode = self.db.query_one(
            "SELECT e.id FROM episodes e JOIN projects p ON p.id=e.project_id "
            "WHERE p.title=? AND e.number=?",
            (run["project_title"], run["episode_number"]))
        if episode is not None:
            self.db.execute(
                "UPDATE production_runs SET episode_id=?, updated_at=? "
                "WHERE id=?", (episode["id"], now(), run_id))
        return self.db.query_one(
            "SELECT * FROM production_runs WHERE id=?", (run_id,))

    def mark_cancelling(self, run_id):
        if run_id:
            self.db.execute(
                "UPDATE production_runs SET status='cancelling', "
                "updated_at=? WHERE id=? AND status='running'",
                (now(), run_id))

    def finish_run(self, run_id, summary=None, error=""):
        run = self._bind_episode(run_id)
        if run is None:
            return None
        summary = summary if isinstance(summary, dict) else {
            "result": summary}
        stages = summary.get("stages") or []
        result_status = str(summary.get("status") or "")
        if error:
            status = "failed"
        elif result_status in {"failed", "qc_failed"}:
            status = "failed"
        elif result_status in {"awaiting_script", "awaiting_cast",
                               "awaiting_confirm", "queued_script"}:
            status = "paused"
        elif result_status == "created":
            status = "stopped"
        else:
            status = "completed"
        providers = set()
        for stage in stages:
            providers.update(stage.get("providers") or [])
        if run["episode_id"] is not None:
            task_rows = self.db.query(
                "SELECT provider FROM tasks WHERE run_id=?", (run_id,))
            for task in task_rows:
                providers.update(p.strip() for p in
                                 (task["provider"] or "").split(",")
                                 if p.strip())
        if stages:
            cost = sum(float(stage.get("cost") or 0) for stage in stages)
            last_stage = str(stages[-1].get("stage") or "")
        else:
            cost = float(summary.get("cost") or 0)
            last_stage = str(summary.get("stage") or "")
        ts = now()
        self.db.execute(
            "UPDATE production_runs SET status=?, result_status=?, cost=?, "
            "providers=?, stage_count=?, last_stage=?, error=?, summary=?, "
            "finished_at=?, updated_at=? WHERE id=?",
            (status, result_status, round(cost, 4),
             ",".join(sorted(providers)), len(stages), last_stage,
             str(error or summary.get("error") or "")[:2000],
             json.dumps(summary, ensure_ascii=False)[:200000], ts, ts,
             run_id))
        return self.get(run_id)

    def _landing_status(self, episode_id):
        gate = self.db.query_one(
            "SELECT COUNT(*) AS n FROM tasks WHERE episode_id=? "
            "AND stage='preflight' AND status='done'", (episode_id,))
        script = self.db.query_one(
            "SELECT id FROM documents WHERE episode_id=? AND kind='script' "
            "LIMIT 1", (episode_id,))
        if gate and gate["n"]:
            return "awaiting_confirm"
        if script is not None:
            return "awaiting_script"
        return "created"

    def recover_episode(self, episode_id, reason="生成进程已结束，恢复历史状态"):
        episode = self.db.query_one(
            "SELECT * FROM episodes WHERE id=?", (episode_id,))
        if episode is None:
            return None
        landing = self._landing_status(episode_id)
        ts = now()
        self.db.execute(
            "UPDATE tasks SET status='interrupted', "
            "error=CASE WHEN error='' THEN ? ELSE error END, updated_at=? "
            "WHERE episode_id=? AND status='running'",
            (reason, ts, episode_id))
        self.db.execute(
            "UPDATE episodes SET status=?, updated_at=? WHERE id=?",
            (landing, ts, episode_id))
        self.db.execute(
            "UPDATE production_runs SET status='interrupted', "
            "result_status=?, error=CASE WHEN error='' THEN ? ELSE error END, "
            "finished_at=COALESCE(finished_at, ?), updated_at=? "
            "WHERE episode_id=? AND status IN ('running','cancelling')",
            (landing, reason, ts, ts, episode_id))
        return landing

    def bootstrap(self):
        """只在 Web 服务启动时调用：恢复失联任务并回填旧数据。"""
        ts = now()
        self.db.execute(
            "UPDATE production_runs SET status='interrupted', "
            "error=CASE WHEN error='' THEN '服务重启，原运行已中断' "
            "ELSE error END, finished_at=COALESCE(finished_at, ?), "
            "updated_at=? WHERE status IN ('running','cancelling')",
            (ts, ts))
        active = self.db.query(
            "SELECT id FROM episodes WHERE status NOT IN "
            "('done','failed','qc_failed','created','awaiting_script',"
            "'awaiting_cast','awaiting_confirm','queued_script')")
        for episode in active:
            self.recover_episode(
                episode["id"], "服务重启，生成任务已安全恢复")
        self._bind_unbound_runs()
        self._backfill_legacy()

    def _bind_unbound_runs(self):
        for row in self.db.query(
                "SELECT id FROM production_runs WHERE episode_id IS NULL"):
            self._bind_episode(row["id"])

    def _backfill_legacy(self):
        episodes = self.db.query(
            "SELECT e.*, p.title AS project_title FROM episodes e "
            "JOIN projects p ON p.id=e.project_id WHERE NOT EXISTS "
            "(SELECT 1 FROM production_runs r WHERE r.episode_id=e.id) "
            "ORDER BY e.id")
        for episode in episodes:
            tasks = [dict(row) for row in self.db.query(
                "SELECT id, stage, name, status, provider, cost, error, "
                "created_at, updated_at FROM tasks WHERE episode_id=? "
                "ORDER BY id", (episode["id"],))]
            providers = set()
            for task in tasks:
                providers.update(p.strip() for p in
                                 (task["provider"] or "").split(",")
                                 if p.strip())
            interrupted = any(t["status"] in {"running", "interrupted"}
                              for t in tasks)
            status = ("interrupted" if interrupted else
                      "completed" if episode["status"] == "done" else
                      "failed" if episode["status"] in {"failed", "qc_failed"}
                      else "paused" if episode["status"] in {
                          "awaiting_script", "awaiting_cast",
                          "awaiting_confirm", "queued_script"} else
                      "stopped")
            started = min((t["created_at"] for t in tasks),
                          default=episode["created_at"])
            finished = max((t["updated_at"] for t in tasks),
                           default=episode["updated_at"])
            summary = {
                "legacy": True,
                "status": episode["status"],
                "stages": tasks,
            }
            self.db.execute(
                "INSERT INTO production_runs(episode_id, project_title, "
                "episode_number, action, source, status, result_status, "
                "cost, providers, stage_count, last_stage, summary, "
                "started_at, finished_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (episode["id"], episode["project_title"], episode["number"],
                 "legacy_import", "migration", status, episode["status"],
                 episode["cost"], ",".join(sorted(providers)), len(tasks),
                 tasks[-1]["stage"] if tasks else "",
                 json.dumps(summary, ensure_ascii=False), started, finished,
                 now()))

    def _payload(self, row, include_summary=False):
        if row is None:
            return None
        item = dict(row)
        item["force"] = bool(item["force"])
        item["providers"] = [p for p in item["providers"].split(",") if p]
        item["duration_seconds"] = round(
            max(0, (item["finished_at"] or now()) - item["started_at"]), 1)
        item["request"] = _loads(item.pop("request", "{}"), {})
        raw_summary = item.pop("summary", "{}")
        if include_summary:
            item["summary"] = _loads(raw_summary, {})
        return item

    def list(self, limit=200, status=None, action=None, query=""):
        limit = max(1, min(int(limit), 500))
        where, params = [], []
        if status:
            where.append("r.status=?")
            params.append(status)
        if action:
            where.append("r.action=?")
            params.append(action)
        if query:
            where.append("(r.project_title LIKE ? OR "
                         "CAST(r.episode_number AS TEXT) LIKE ?)")
            needle = f"%{query}%"
            params.extend((needle, needle))
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = self.db.query(
            "SELECT r.*, COALESCE(p.title, r.project_title) AS current_project "
            "FROM production_runs r LEFT JOIN episodes e ON e.id=r.episode_id "
            "LEFT JOIN projects p ON p.id=e.project_id" + clause +
            " ORDER BY r.started_at DESC, r.id DESC LIMIT ?",
            (*params, limit))
        items = [self._payload(row) for row in rows]
        counts = {row["status"]: row["n"] for row in self.db.query(
            "SELECT status, COUNT(*) AS n FROM production_runs "
            "GROUP BY status")}
        total = sum(counts.values())
        cost = self.db.query_one(
            "SELECT COALESCE(SUM(cost),0) AS total FROM production_runs")
        return {
            "items": items,
            "stats": {
                "total": total,
                "completed": counts.get("completed", 0),
                "paused": counts.get("paused", 0),
                "failed": counts.get("failed", 0),
                "interrupted": counts.get("interrupted", 0),
                "running": counts.get("running", 0) +
                           counts.get("cancelling", 0),
                "total_cost": round(cost["total"], 2),
            },
            "filters": {
                "statuses": sorted(counts),
                "actions": [row["action"] for row in self.db.query(
                    "SELECT DISTINCT action FROM production_runs "
                    "ORDER BY action")],
            },
        }

    def get(self, run_id):
        row = self.db.query_one(
            "SELECT r.*, COALESCE(p.title, r.project_title) AS current_project "
            "FROM production_runs r LEFT JOIN episodes e ON e.id=r.episode_id "
            "LEFT JOIN projects p ON p.id=e.project_id WHERE r.id=?",
            (int(run_id),))
        item = self._payload(row, include_summary=True)
        if item is None:
            return None
        if item["episode_id"] is not None:
            tasks = self.db.query(
                "SELECT id, run_id, stage, name, status, provider, cost, "
                "error, created_at, updated_at FROM tasks "
                "WHERE run_id=? OR (run_id IS NULL AND episode_id=? AND "
                "created_at>=? AND created_at<=?) ORDER BY id",
                (item["id"], item["episode_id"], item["started_at"] - 0.001,
                 (item["finished_at"] or now()) + 0.001))
            item["tasks"] = [dict(task) for task in tasks]
        else:
            item["tasks"] = []
        return item

    def delete_work(self, run_id, delete_assets=False):
        """删除某次历史所对应的整集作品，并可选软删除关联图片。

        删除对象是 ``《项目》第 N 集``，因此该集的全部运行记录会一起
        删除；项目壳与物理产物文件始终保留。多集项目中的人物/场景等
        公共母资产不会因为删除其中一集而被误删。
        """
        run = self.db.query_one(
            "SELECT r.*, e.project_id, p.title AS current_project "
            "FROM production_runs r LEFT JOIN episodes e ON e.id=r.episode_id "
            "LEFT JOIN projects p ON p.id=e.project_id WHERE r.id=?",
            (int(run_id),))
        if run is None:
            return None

        # 极早期未绑定到剧集的迁移记录只能删除历史本身。
        if run["episode_id"] is None or run["project_id"] is None:
            task_count = self.db.query_one(
                "SELECT COUNT(*) AS n FROM tasks WHERE run_id=?",
                (int(run_id),))["n"]
            self.db.execute("DELETE FROM tasks WHERE run_id=?", (int(run_id),))
            self.db.execute(
                "DELETE FROM production_runs WHERE id=?", (int(run_id),))
            return {
                "project": run["project_title"],
                "episode_number": run["episode_number"],
                "episode_deleted": False,
                "history_only": True,
                "runs_deleted": 1,
                "tasks_deleted": task_count,
                "assets_requested": bool(delete_assets),
                "assets_soft_deleted": 0,
                "asset_files_preserved": True,
                "project_retained": True,
            }

        episode_id = int(run["episode_id"])
        project_id = int(run["project_id"])
        episode_number = int(run["episode_number"])
        project_title = run["current_project"] or run["project_title"]
        prefix = f"e{episode_number:03d}"
        counts = {
            "runs": self.db.query_one(
                "SELECT COUNT(*) AS n FROM production_runs WHERE episode_id=?",
                (episode_id,))["n"],
            "tasks": self.db.query_one(
                "SELECT COUNT(*) AS n FROM tasks WHERE episode_id=?",
                (episode_id,))["n"],
            "documents": self.db.query_one(
                "SELECT COUNT(*) AS n FROM documents WHERE episode_id=?",
                (episode_id,))["n"],
        }
        episode_count = self.db.query_one(
            "SELECT COUNT(*) AS n FROM episodes WHERE project_id=?",
            (project_id,))["n"]

        assets_deleted = 0
        if delete_assets:
            center = AssetCenter(self.db)
            for asset in center.active_list(project_id):
                if asset["kind"] not in IMAGE_KINDS:
                    continue
                meta = center.meta(asset)
                source_id = meta.get("source_episode_id")
                source_number = meta.get(
                    "source_episode_number", meta.get("episode_number"))
                episode_scoped = bool(
                    re.match(rf"^{re.escape(prefix)}(?:_|$)", asset["name"])
                    or str(source_id or "") == str(episode_id)
                    or str(source_number or "") == str(episode_number))
                # 单集项目中的所有可见图片都属于这一作品；多集项目只删
                # 明确绑定本集的图片，保住跨集人物/场景/服装母资产。
                if episode_count == 1 or episode_scoped:
                    deleted = center.soft_delete(
                        project_id, asset["kind"], asset["name"], meta={
                            "reason": "deleted_with_history_work",
                            "deleted_episode_id": episode_id,
                            "deleted_episode_number": episode_number,
                        })
                    assets_deleted += int(deleted is not None)

        batch_ids = [row["batch_id"] for row in self.db.query(
            "SELECT DISTINCT batch_id FROM series_batch_items WHERE episode_id=?",
            (episode_id,))]
        self.db.execute(
            "DELETE FROM series_batch_items WHERE episode_id=?", (episode_id,))
        for batch_id in batch_ids:
            remaining = self.db.query_one(
                "SELECT COUNT(*) AS n FROM series_batch_items WHERE batch_id=?",
                (batch_id,))["n"]
            if remaining:
                self.db.execute(
                    "UPDATE series_batches SET total=?, updated_at=? WHERE id=?",
                    (remaining, now(), batch_id))
            else:
                self.db.execute(
                    "DELETE FROM series_batches WHERE id=?", (batch_id,))

        # 所有外键子表先删，最后删除剧集；项目保留以承载用户选择保留的
        # 资产，以及软删除图片的可追溯版本链。
        self.db.execute("DELETE FROM archive WHERE episode_id=?", (episode_id,))
        self.db.execute("DELETE FROM tasks WHERE episode_id=?", (episode_id,))
        self.db.execute("DELETE FROM documents WHERE episode_id=?", (episode_id,))
        self.db.execute(
            "DELETE FROM production_runs WHERE episode_id=?", (episode_id,))
        self.db.execute("DELETE FROM episodes WHERE id=?", (episode_id,))
        return {
            "project": project_title,
            "episode_number": episode_number,
            "episode_deleted": True,
            "history_only": False,
            "runs_deleted": counts["runs"],
            "tasks_deleted": counts["tasks"],
            "documents_deleted": counts["documents"],
            "assets_requested": bool(delete_assets),
            "assets_soft_deleted": assets_deleted,
            "asset_files_preserved": True,
            "project_retained": True,
        }
