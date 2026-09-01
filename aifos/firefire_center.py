"""火火漫剧研究室:可追溯的漫剧学习与独立风格包中心。

这里保存的是研究资料和人工确认后的可复用结果，而不是另一套会偷偷改写
生产标准的规则。学习结果只有在项目第一步显式选择后，才会作为该项目的
视觉提示词使用；发布独立风格包始终需要人工确认。
"""

import json
import uuid

from .db import now
from .errors import AifosError
from .knowledge_brain import KnowledgeBrain
from .style_director import (
    compile_director_style,
    director_counts,
    director_ready,
    normalize_director_knowledge,
)


BRAIN_NAME = "火火漫剧研究室"
ANALYSIS_SCHEMA = "firefire.analysis/v1"


def _json(value, default):
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


class FireFireCenter:
    """管理学习会话、引用证据、分析草稿和人工确认的风格包。"""

    def __init__(self, db, artifacts_root=None, standards=None):
        self.db = db
        self.artifacts_root = artifacts_root
        self.knowledge = KnowledgeBrain(db, standards=standards)
        self.knowledge.ensure_seed()

    @staticmethod
    def _session(row, *, include_evidence=False):
        if row is None:
            return None
        result = dict(row)
        result["rights_confirmed"] = bool(result["rights_confirmed"])
        result["analysis"] = _json(result.get("analysis"), {})
        if include_evidence:
            result["evidence"] = []
        return result

    @staticmethod
    def _evidence(row):
        result = dict(row)
        result["meta"] = _json(result.get("meta"), {})
        return result

    @staticmethod
    def _style(row):
        if row is None:
            return None
        result = dict(row)
        result["references"] = _json(result.pop("references_json", "[]"), [])
        result["validation"] = _json(result.get("validation"), {})
        result["director_knowledge"] = normalize_director_knowledge(
            _json(result.get("director_knowledge"), {}))
        result["director_counts"] = director_counts(
            result["director_knowledge"])
        result["director_ready"] = director_ready(
            result["director_knowledge"])
        result["director_prompt"] = compile_director_style(
            result.get("compiled_style", ""),
            result["director_knowledge"])
        return result

    @staticmethod
    def _validation(row):
        if row is None:
            return None
        result = dict(row)
        result["references"] = _json(
            result.pop("references_json", "[]"), [])
        result["result"] = _json(result.pop("result_json", "{}"), {})
        return result

    def overview(self):
        sessions = self.list_sessions(limit=20)
        styles = self.list_styles()
        knowledge = self.knowledge.list()
        validation = [self._validation(row) for row in self.db.query(
            "SELECT * FROM firefire_validation_tasks "
            "ORDER BY created_at DESC LIMIT 20")]
        return {
            "name": BRAIN_NAME,
            "analysis_schema": ANALYSIS_SCHEMA,
            "sessions": sessions,
            "styles": styles,
            "knowledge": knowledge,
            "validation_tasks": validation,
            "counts": {
                "sessions": int(self.db.query_one(
                    "SELECT COUNT(*) AS n FROM firefire_sessions")["n"]),
                "evidence": int(self.db.query_one(
                    "SELECT COUNT(*) AS n FROM firefire_evidence")["n"]),
                "draft_styles": int(self.db.query_one(
                    "SELECT COUNT(*) AS n FROM firefire_styles WHERE status='draft'"
                )["n"]),
                "approved_styles": int(self.db.query_one(
                    "SELECT COUNT(*) AS n FROM firefire_styles "
                    "WHERE status='approved'")["n"]),
                "validation_tasks": int(self.db.query_one(
                    "SELECT COUNT(*) AS n FROM firefire_validation_tasks"
                )["n"]),
                "knowledge_active": int(self.db.query_one(
                    "SELECT COUNT(*) AS n FROM firefire_knowledge_state "
                    "WHERE status='active' AND active_version_id IS NOT NULL"
                )["n"]),
                "knowledge_review": int(self.db.query_one(
                    "SELECT COUNT(*) AS n FROM firefire_knowledge_state "
                    "WHERE status='review' AND candidate_version_id IS NOT NULL"
                )["n"]),
            },
        }

    def create_knowledge(self, payload):
        return self.knowledge.create_candidate(payload)

    def publish_knowledge(self, knowledge_key, *, approved_by="human",
                          note=""):
        return self.knowledge.publish(
            knowledge_key, approved_by=approved_by, note=note)

    def refresh_knowledge(self, knowledge_key):
        return self.knowledge.refresh_alignment(knowledge_key)

    def resolve_knowledge(self, **context):
        return self.knowledge.resolve(**context)

    def create_session(self, name="", source_url="", source_type="url",
                       rights_confirmed=False, notes=""):
        name = str(name or "").strip() or BRAIN_NAME
        source_url = str(source_url or "").strip()
        source_type = str(source_type or "url").strip() or "url"
        notes = str(notes or "").strip()
        if not source_url and not notes:
            raise AifosError("学习会话需要来源链接或研究备注")
        timestamp = now()
        status = "queued" if rights_confirmed else "awaiting_rights"
        analysis = self._analysis_template()
        cur = self.db.execute(
            "INSERT INTO firefire_sessions(name, source_url, source_type, "
            "status, rights_confirmed, notes, analysis, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (name, source_url, source_type, status,
             1 if rights_confirmed else 0, notes,
             json.dumps(analysis, ensure_ascii=False), timestamp, timestamp),
        )
        return self.get_session(cur.lastrowid, include_evidence=True)

    @staticmethod
    def _analysis_template():
        return {
            "schema": ANALYSIS_SCHEMA,
            "status": "draft",
            "facts": [],
            "hypotheses": [],
            "dimensions": {
                "narrative": {},
                "scene": {},
                "visual_style": {},
                "character": {},
                "camera_motion": {},
                "sound": {},
                "rhythm": {},
            },
            "reference_plan": [],
            "validation_tasks": [],
            "source_trace": [],
            "next_actions": [
                "补充可使用的原始素材或公开链接",
                "为关键结论绑定截图/时间码/参考图证据",
                "生成验证图并由人工确认后再发布独立风格",
            ],
        }

    def get_session(self, session_id, *, include_evidence=False):
        row = self.db.query_one(
            "SELECT * FROM firefire_sessions WHERE id=?", (int(session_id),))
        result = self._session(row, include_evidence=include_evidence)
        if result is not None and include_evidence:
            result["evidence"] = [
                self._evidence(item) for item in self.db.query(
                    "SELECT * FROM firefire_evidence WHERE session_id=? "
                    "ORDER BY created_at, id", (int(session_id),))]
        return result

    def list_sessions(self, limit=20):
        limit = max(1, min(int(limit), 200))
        return [self._session(row) for row in self.db.query(
            "SELECT * FROM firefire_sessions ORDER BY updated_at DESC "
            "LIMIT ?", (limit,))]

    def start_analysis(self, session_id):
        session = self.get_session(session_id)
        if session is None:
            raise AifosError("学习会话不存在")
        if not session["rights_confirmed"]:
            raise AifosError("请先确认你有权使用该链接或素材")
        analysis = dict(session.get("analysis") or self._analysis_template())
        analysis["status"] = "review"
        analysis["started_at"] = now()
        self.db.execute(
            "UPDATE firefire_sessions SET status='review', analysis=?, "
            "updated_at=? WHERE id=?",
            (json.dumps(analysis, ensure_ascii=False), now(), int(session_id)),
        )
        return self.get_session(session_id, include_evidence=True)

    def add_evidence(self, session_id, *, kind="frame", label="", uri="",
                     timecode="", observation="", meta=None):
        if self.get_session(session_id) is None:
            raise AifosError("学习会话不存在")
        if not any(str(value or "").strip()
                   for value in (uri, observation, label)):
            raise AifosError("证据至少需要文件/链接、标签或观察说明")
        cur = self.db.execute(
            "INSERT INTO firefire_evidence(session_id, kind, label, uri, "
            "timecode, observation, meta, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (int(session_id), str(kind or "frame").strip(),
             str(label or "").strip(), str(uri or "").strip(),
             str(timecode or "").strip(), str(observation or "").strip(),
             json.dumps(meta or {}, ensure_ascii=False), now()),
        )
        self.db.execute(
            "UPDATE firefire_sessions SET updated_at=? WHERE id=?",
            (now(), int(session_id)))
        return self._evidence(self.db.query_one(
            "SELECT * FROM firefire_evidence WHERE id=?", (cur.lastrowid,)))

    def create_style(self, *, name, session_id=None, summary="",
                     compiled_style="", positive_prompt="",
                     negative_prompt="", references=None, validation=None,
                     director_knowledge=None):
        name = str(name or "").strip()
        compiled_style = str(compiled_style or "").strip()
        if not name or not compiled_style:
            raise AifosError("独立风格需要名称和可执行的风格提示词")
        linked_session = None
        if session_id is not None:
            linked_session = self.get_session(session_id)
            if linked_session is None:
                raise AifosError("关联的学习会话不存在")
            if not linked_session["rights_confirmed"]:
                raise AifosError("关联会话尚未确认素材使用权")
        row = self.db.query_one(
            "SELECT COALESCE(MAX(version), 0) AS version FROM firefire_styles "
            "WHERE name=?", (name,))
        version = int(row["version"] or 0) + 1
        style_id = f"ffstyle-{uuid.uuid4().hex[:12]}"
        timestamp = now()
        knowledge = normalize_director_knowledge(director_knowledge)
        self.db.execute(
            "INSERT INTO firefire_styles(id, name, version, status, session_id, "
            "summary, compiled_style, positive_prompt, negative_prompt, "
            "references_json, director_knowledge, validation, created_at, "
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (style_id, name, version, "draft", session_id,
             str(summary or "").strip(), compiled_style,
             str(positive_prompt or "").strip(),
             str(negative_prompt or "").strip(),
             json.dumps(references or [], ensure_ascii=False),
             json.dumps(knowledge, ensure_ascii=False),
             json.dumps(validation or {}, ensure_ascii=False),
             timestamp, timestamp),
        )
        return self.get_style(style_id)

    def get_style(self, style_id, *, approved_only=False):
        row = self.db.query_one(
            "SELECT * FROM firefire_styles WHERE id=?", (str(style_id),))
        result = self._style(row)
        if approved_only and (result is None or result["status"] != "approved"):
            return None
        return result

    def list_styles(self, status=None):
        params = []
        where = ""
        if status:
            where = "WHERE status=?"
            params.append(str(status))
        return [self._style(row) for row in self.db.query(
            f"SELECT * FROM firefire_styles {where} "
            "ORDER BY updated_at DESC", params)]

    def publish_style(self, style_id, *, approved_by="human", feedback=""):
        style = self.get_style(style_id)
        if style is None:
            raise AifosError("独立风格不存在")
        if style["status"] != "draft":
            raise AifosError("只有草稿风格可以发布")
        if not style.get("director_ready"):
            raise AifosError(
                "独立风格缺少结构化导演知识：必须同时填写镜头语言、"
                "视觉效果和按剧情功能选择规则，不能只发布一段总提示词")
        approved_by = str(approved_by or "human").strip()
        validation = dict(style.get("validation") or {})
        validation.update({
            "human_confirmed": True,
            "approved_by": approved_by,
            "approved_at": now(),
            "feedback": str(feedback or "").strip(),
        })
        self.db.execute(
            "UPDATE firefire_styles SET status='approved', validation=?, "
            "updated_at=? WHERE id=?",
            (json.dumps(validation, ensure_ascii=False), now(), str(style_id)),
        )
        if style.get("session_id"):
            self.db.execute(
                "UPDATE firefire_sessions SET status='complete', updated_at=? "
                "WHERE id=? AND status IN ('queued','review')",
                (now(), style["session_id"]))
        return self.get_style(style_id)

    def archive_style(self, style_id):
        style = self.get_style(style_id)
        if style is None:
            raise AifosError("独立风格不存在")
        self.db.execute(
            "UPDATE firefire_styles SET status='archived', updated_at=? "
            "WHERE id=?", (now(), str(style_id)))
        return self.get_style(style_id)

    def create_validation_task(self, *, title, prompt, session_id=None,
                               style_id=None, references=None):
        if session_id is not None and self.get_session(session_id) is None:
            raise AifosError("关联的学习会话不存在")
        if style_id is not None and self.get_style(style_id) is None:
            raise AifosError("关联的独立风格不存在")
        title = str(title or "").strip() or "风格验证图"
        prompt = str(prompt or "").strip()
        if not prompt:
            raise AifosError("验证任务需要提示词")
        timestamp = now()
        cur = self.db.execute(
            "INSERT INTO firefire_validation_tasks(session_id, style_id, title, "
            "prompt, references_json, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (session_id, style_id, title, prompt,
             json.dumps(references or [], ensure_ascii=False),
             timestamp, timestamp),
        )
        return self._validation(self.db.query_one(
            "SELECT * FROM firefire_validation_tasks WHERE id=?",
            (cur.lastrowid,)))
