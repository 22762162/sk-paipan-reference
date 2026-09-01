"""数据中心:沉淀 Prompt、图片、视频、成功案例、失败案例,
为后续 AI 学习提供基础。"""

import json

from .db import now


class DataCenter:
    def __init__(self, db):
        self.db = db

    def record(self, kind, label, prompt="", uri="", meta=None,
               episode_id=None):
        """kind: prompt/image/video/voice/case;label: success/failure。"""
        self.db.execute(
            "INSERT INTO archive(episode_id, kind, label, prompt, uri, meta, "
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (episode_id, kind, label, prompt, uri,
             json.dumps(meta or {}, ensure_ascii=False), now()),
        )

    def cases(self, label=None, kind=None, episode_id=None):
        sql = "SELECT * FROM archive WHERE 1=1"
        params = []
        if label is not None:
            sql += " AND label=?"
            params.append(label)
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        if episode_id is not None:
            sql += " AND episode_id=?"
            params.append(episode_id)
        return self.db.query(sql + " ORDER BY id", tuple(params))

    def stats(self):
        return self.db.query(
            "SELECT kind, label, COUNT(*) AS total FROM archive "
            "GROUP BY kind, label ORDER BY kind, label")

    def export_jsonl(self, out_path):
        """导出全部沉淀数据为 JSONL,供后续训练/分析。"""
        rows = self.db.query("SELECT * FROM archive ORDER BY id")
        with open(out_path, "w", encoding="utf-8") as f:
            for row in rows:
                item = dict(row)
                item["meta"] = json.loads(item["meta"])
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        return len(rows)
