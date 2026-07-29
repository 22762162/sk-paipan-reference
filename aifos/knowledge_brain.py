"""火火漫剧研究室的版本化知识大脑。

知识与风格包分离：风格包描述“画成什么样”，知识条目描述“在什么条件下
采用什么方法”。任何外部资料都先经过确定性的价值门禁，再成为待人工审核
候选；只有人工激活、且仍与当前制作标准对齐的版本才允许被生产 Skill 调用。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from copy import deepcopy

from .db import now
from .errors import AifosError


KNOWLEDGE_SCHEMA = "firefire.knowledge/v1"
ASSESSMENT_SCHEMA = "firefire.knowledge-assessment/v1"
MIN_USEFUL_SCORE = 70
KINDS = {"knowledge", "skill"}
DOMAINS = {
    "script", "storyboard", "blocking", "image", "video", "audio",
    "qc", "delivery", "cross_stage",
}
STAGES = {
    "script", "continuity", "cast", "storyboard", "blocking", "images",
    "text_assets", "frames", "production_gate", "video", "edit", "review",
    "delivery", "cross_stage",
}


def _json(value, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


def _strings(value):
    if isinstance(value, str):
        value = re.split(r"[\n,，;；]+", value)
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    seen = set()
    for item in value:
        text = str(item or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _slug(value):
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text).strip("-")
    return text


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


DEPTH_STRUCTURE_SEED = {
    "knowledge_key": "depth-structure-control",
    "title": "深度结构控制：拆分人物、场景、构图、动作与运镜职责",
    "kind": "skill",
    "domain": "cross_stage",
    "summary": (
        "把参考素材拆成单一职责：人物图锁身份与服装，场景图锁环境、"
        "色彩与光线，深度图或深度视频只传递构图、远近、遮挡、动作"
        "节奏与镜头运动，减少参考污染和控制权争夺。"
    ),
    "content": {
        "principles": [
            "每份参考素材只承担一个主要控制职责，禁止一张参考图同时争夺身份、风格、结构和动作控制权。",
            "灰度图、三维灰模与深度图不是同一种结构信号；只有逐像素相对距离可作为深度控制。",
            "深度信号只负责空间结构，不负责人物身份、服装纹理、色彩、灯光、表情和精细手指接触。",
        ],
        "workflow": [
            "先明确要保留的是风格、姿态、轮廓还是空间关系；只有需要构图、尺度、前后层次和遮挡时才使用深度图。",
            "从参考图或视频生成单目相对深度素材，锁定原画幅、机位、主体位置、姿态、轮廓与遮挡关系。",
            "确认深度方向；默认近白远黑，同一表面连续过渡，禁止把原图颜色、纹理、文字和材质带入深度素材。",
            "图像生成时分别绑定人物、场景、深度结构职责；视频生成时另行绑定动作顺序、速度、重心、走位和运镜轨迹。",
            "输出后专项复核身份、场景、构图、遮挡、脚底接触、手部道具关系、背景跳动与镜头反向。",
        ],
        "prompt_templates": [
            (
                "参考职责：人物参考只锁身份与当前造型；场景参考只锁环境、"
                "色彩与光线；深度参考只锁构图、机位、主体尺度、动作、"
                "前后层次和遮挡。禁止继承深度素材的灰度外观或其中人物身份。"
            ),
        ],
        "limitations": [
            "不能可靠锁定表情、眼神、嘴形、手指、精细抓握、绳索缠绕和复杂双人手部交互。",
            "单目深度通常是相对距离，不能冒充真实米制距离或精密测量。",
            "工具可能使用相反深度方向，调用前必须验证近远黑白约定。",
        ],
        "anti_patterns": [
            "把彩色参考图直接去色后当作深度图。",
            "让模型自由生成所谓灰模，导致原动作、机位和构图被重新设计。",
            "逐帧单独拉伸视频深度对比度，造成深度呼吸和闪烁。",
            "用深度信号强行控制表情、手指或道具接触细节。",
        ],
        "quality_gates": [
            "深度素材与原参考的画幅、构图、姿态、主体尺度、遮挡关系一致。",
            "视频深度使用统一模型、预处理、深度方向和全片归一化范围，并通过时间稳定检查。",
            "最终彩色结果未继承深度图灰度外观或原参考人物身份。",
            "表情、手部和道具接触由文字或专项参考补足并单独质检。",
        ],
        "validation_plan": [
            "同一镜头分别做直接多图参考与职责拆分参考，对比构图命中率、身份漂移和参考污染。",
            "视频抽取首中尾帧检查主体尺度、遮挡序、脚底接触与背景跳动。",
        ],
        "standard_refs": [
            "rules.storyboard",
            "rules.production.prompt_contract.reference_roles",
            "rules.quality_gates.spatial",
            "rules.quality_gates.frames",
        ],
    },
    "applicability": {
        "stages": ["storyboard", "blocking", "images", "frames", "video"],
        "task_types": [
            "reference_role_separation", "composition_transfer",
            "motion_transfer", "camera_transfer", "depth_control",
        ],
        "triggers": [
            "参考图污染", "构图复刻", "动作复刻", "运镜复刻", "前后层次",
            "遮挡关系", "深度图", "深度视频", "单目深度",
        ],
        "tags": [
            "构图控制", "动作控制", "运镜控制", "空间关系", "参考图职责",
        ],
        "exclusions": [
            "仅需要色彩、材质或布光风格时直接使用风格参考。",
            "仅需要关节姿态时优先使用姿态骨架。",
            "仅需要清晰轮廓时优先使用边缘控制。",
        ],
    },
    "provenance": {
        "source_url": "https://www.super-i.cn/info-2976.html",
        "source_title": "【提示词创作第七十二节】用结构控制AI画面的构图、动作与运镜",
        "author": "Loki / 刺猬星球 super-i",
        "published_at": "2026-07-29",
        "checked_at": "2026-07-30",
        "evidence": [
            "课程正文给出深度图概念、图片与视频职责拆分流程及明确能力边界。",
            "知识条目仅保存结构化方法摘要，不复制课程全文。",
        ],
    },
}

SCRIPT_DEVELOPMENT_SEED = {
    "knowledge_key": "idea-to-shootable-script",
    "title": "点子到可拍剧本：目标、阻力、代价、选择与伏笔回收",
    "kind": "skill",
    "domain": "script",
    "summary": (
        "在进入人物资产和分镜前，把模糊点子收敛为可见的主角目标、"
        "主要阻力、失败代价和关键选择；需要反转时，用已经出现的动作、"
        "道具、声音或镜头线索改变观众理解，并先比较少量差异化走向。"
    ),
    "content": {
        "principles": [
            "剧本必须先回答主角要完成什么、什么直接阻止他、失败会失去什么；三者都要能由具体画面、动作、关系或道具状态表现。",
            "高潮选择必须让主角在两个都有代价的选项之间取舍，禁止用轻易两全、巧合解围或临时新增设定收束。",
            "反转不是突兀换身份，而是让观众重新理解前面已经看见或听见的动作、道具、声音和镜头信息。",
            "同一核心点子的候选走向必须真正改变目标、冲突来源、关键选择、反转机制或结尾情绪，不能只换职业、地点和道具名称。",
        ],
        "workflow": [
            "从点子中提取主角、异常事件、潜在冲突和开场疑问，先不直接生成完整剧本。",
            "分别提出可见的主角目标、直接阻力、失败代价和行动窗口，再组合成因果成立的少量故事框架。",
            "为每个框架写开场钩子、第一次行动、压力升级、关键选择、结尾画面以及 AI 视频制作难点。",
            "按戏剧张力、视觉表现力、人物可信度、反转完成度和 AI 视频可控性筛选，只把一个入选方案交给正式剧本。",
            "需要反转时建立信息释放表，记录观众开场知道什么、中段误读什么、高潮揭示什么、结尾刻意保留什么。",
            "把入选方案写成分场剧本；每场都必须制造阻力、提供线索、改变关系、迫使选择或形成结果，并与前后场建立因果。",
        ],
        "prompt_templates": [
            (
                "先建立点子开发合同：主角的具体行动目标、直接阻力、"
                "失败代价、行动窗口、两个都有代价的关键选择和可拍开场钩子。"
                "重要信息只通过动作、空间、道具、声音或镜头呈现。"
            ),
            (
                "如需反转，列出至少两条前段可见或可听线索，以及观众的"
                "初始理解、真实含义和回收位置；禁止用旁白或结尾对白解释真相。"
            ),
        ],
        "limitations": [
            "并非所有题材都需要反转；温情、日常、表演或氛围短片可用选择与关系变化完成收束。",
            "多方案发散只属于视觉资产生成前的开发阶段；已锁定并生成资产的剧本不得自动回退重写。",
            "三分钟、三个人、三处场景和固定生成十个走向只是示例预算，不是通用硬标准。",
        ],
        "anti_patterns": [
            "把抽象愿望写成目标，例如只写证明自己、寻找真相或获得认可，却没有可执行动作。",
            "用死亡、爆炸或巨大灾难强行抬高失败代价，而代价与人物目标无直接关系。",
            "反转依赖卧底、幻觉、主角已死、突然变坏或巧合获知真相，却没有前段线索。",
            "把五个目标、五个阻力、五个代价和十个走向固定成每次运行的机械数量。",
            "在下游已经锁定人物或镜头后重新发散多个故事版本，导致正式事实源分叉。",
        ],
        "quality_gates": [
            "目标是可完成或失败的具体行动，阻力会迫使主角改变行动方式，失败代价具体、相关且可见。",
            "每场至少完成制造阻力、提供线索、改变关系、迫使选择或形成结果中的一项。",
            "关键选择不存在无代价的轻易两全方案。",
            "反转至少由两条已经出现的可见或可听线索支撑，揭示后能重新解释既有内容。",
            "入选方案的人物和场景规模、道具数量、信息表达方式符合本项目时长与 AI 视频可控性。",
            "最后一个镜头同时明确已经解决的冲突和刻意保留的信息，不重复解释反转。",
        ],
        "validation_plan": [
            "用同一模糊点子分别走直接生成与开发合同流程，对比主角目标清晰度、解释性对白数量、场景冗余和可拆镜率。",
            "对每个场次建立戏剧功能表，删除既不制造阻力、不提供线索、不改变关系也不形成结果的场次。",
            "对反转逐条做二刷回溯：若无法从前段至少两条线索推回结果，退回剧本阶段重写。",
            "在生成任何人物或场景资产前核对只保留一个正式入选方案，其他候选不得进入制作圣经。",
        ],
        "standard_refs": [
            "rules.script_development",
            "rules.story_analysis",
            "rules.quality_gates.script_bible",
            "rules.production.prompt_contract",
        ],
    },
    "applicability": {
        "stages": ["script"],
        "task_types": [
            "idea_expansion", "script_development", "story_selection",
            "twist_design", "script_repair",
        ],
        "triggers": [
            "点子写成剧本", "目标阻力代价", "失败代价", "关键选择",
            "剧情走向", "反转", "伏笔回收", "信息释放", "可拍剧本",
        ],
        "tags": [
            "剧本开发", "故事结构", "视觉叙事", "反转设计",
            "伏笔回收", "AI视频可控性",
        ],
        "exclusions": [
            "已锁定并生成角色、场景、分镜或视频资产的项目，除非先完成人工影响分析并批准局部返编。",
            "不需要反转的题材不得为了套模板强行增加身份揭露或信息欺骗。",
            "页面链接的第三方 GitHub Skill 未经独立代码与权限审查，不自动安装、运行或信任。",
        ],
    },
    "provenance": {
        "source_url": "https://www.super-i.cn/info-2955.html",
        "source_title": "【提示词创作第六十九节】一个点子，如何写成能拍的AI短片剧本？",
        "author": "西瓜 / 刺猬星球 super-i",
        "published_at": "2026-07",
        "checked_at": "2026-07-30",
        "evidence": [
            "课程正文给出点子展开、创作定位、反转设计、多走向筛选和可执行剧本接口的完整链路。",
            "AIFOS 代码审计确认现有剧本总闸门已覆盖因果、动机、信息、物理、空间、时间、道具、可拍性与密度，本条只保留失败代价、关键选择、信息释放表和多方案筛选等增量。",
            "知识条目只保存结构化方法与边界，不复制课程全文，也不自动信任页面链接的第三方 Skill。",
        ],
    },
}

BUILTIN_SEEDS = (
    DEPTH_STRUCTURE_SEED,
    SCRIPT_DEVELOPMENT_SEED,
)


class KnowledgeBrain:
    """知识候选、价值审核、人工激活、版本升级与运行时检索。"""

    def __init__(self, db, standards=None):
        self.db = db
        self.standards = standards

    def _standard_snapshot(self):
        snapshot = self.standards.active() if self.standards else None
        if not snapshot:
            return {
                "profile_key": "",
                "version": 0,
                "version_id": 0,
                "fingerprint": "",
                "name": "",
            }
        return {
            "profile_key": str(snapshot.get("profile_key") or ""),
            "version": int(snapshot.get("version") or 0),
            "version_id": int(snapshot.get("version_id") or 0),
            "fingerprint": str(snapshot.get("fingerprint") or ""),
            "name": str(snapshot.get("name") or ""),
        }

    @staticmethod
    def _normalize(payload):
        payload = dict(payload or {})
        content = dict(payload.get("content") or {})
        applicability = dict(payload.get("applicability") or {})
        provenance = dict(payload.get("provenance") or {})
        normalized_content = {}
        for key in (
                "principles", "workflow", "prompt_templates", "limitations",
                "anti_patterns", "quality_gates", "validation_plan",
                "standard_refs"):
            normalized_content[key] = _strings(content.get(key))
        normalized_applicability = {}
        for key in (
                "stages", "task_types", "triggers", "tags", "exclusions"):
            normalized_applicability[key] = _strings(
                applicability.get(key))
        normalized_provenance = {
            "source_url": str(provenance.get("source_url") or "").strip(),
            "source_title": str(
                provenance.get("source_title") or "").strip(),
            "author": str(provenance.get("author") or "").strip(),
            "published_at": str(
                provenance.get("published_at") or "").strip(),
            "checked_at": str(provenance.get("checked_at") or "").strip(),
            "evidence": _strings(provenance.get("evidence")),
        }
        return {
            "schema": KNOWLEDGE_SCHEMA,
            "knowledge_key": _slug(payload.get("knowledge_key")),
            "title": str(payload.get("title") or "").strip(),
            "kind": str(payload.get("kind") or "knowledge").strip(),
            "domain": str(payload.get("domain") or "cross_stage").strip(),
            "summary": str(payload.get("summary") or "").strip(),
            "content": normalized_content,
            "applicability": normalized_applicability,
            "provenance": normalized_provenance,
        }

    def assess(self, payload):
        item = self._normalize(payload)
        content = item["content"]
        applicability = item["applicability"]
        provenance = item["provenance"]
        dimensions = {}
        dimensions["relevance"] = min(
            20,
            (10 if applicability["stages"] else 0)
            + (5 if applicability["task_types"] else 0)
            + (5 if applicability["triggers"] else 0))
        dimensions["actionability"] = min(
            20,
            min(8, len(content["principles"]) * 3)
            + min(10, len(content["workflow"]) * 2)
            + (2 if content["prompt_templates"] else 0))
        dimensions["evidence"] = min(
            20,
            (8 if provenance["source_url"] else 0)
            + (4 if provenance["source_title"] else 0)
            + (2 if provenance["author"] else 0)
            + (2 if provenance["published_at"]
               or provenance["checked_at"] else 0)
            + min(4, len(provenance["evidence"]) * 2))
        dimensions["scope_safety"] = min(
            15,
            (6 if content["limitations"] else 0)
            + (4 if applicability["exclusions"] else 0)
            + (5 if content["standard_refs"] else 0))
        dimensions["testability"] = min(
            15,
            (7 if content["quality_gates"] else 0)
            + (8 if content["validation_plan"] else 0))
        dimensions["freshness"] = min(
            10,
            (6 if provenance["checked_at"] else 0)
            + (4 if provenance["published_at"] else 0))
        hard_blocks = []
        if not item["knowledge_key"] or not item["title"]:
            hard_blocks.append("缺少稳定知识 ID 或标题")
        if item["kind"] not in KINDS:
            hard_blocks.append("知识类型必须是 knowledge 或 skill")
        if item["domain"] not in DOMAINS:
            hard_blocks.append("知识领域不受 AIFOS 支持")
        invalid_stages = [
            stage for stage in applicability["stages"]
            if stage not in STAGES]
        if invalid_stages:
            hard_blocks.append(
                "存在未知生产阶段：" + "、".join(invalid_stages))
        if not applicability["stages"] or not applicability["task_types"]:
            hard_blocks.append("没有明确适用阶段或任务类型")
        if not applicability["triggers"]:
            hard_blocks.append("没有可检索的触发条件")
        if not content["principles"] or len(content["workflow"]) < 2:
            hard_blocks.append("缺少可执行原则或至少两步工作流")
        if not content["limitations"] or not applicability["exclusions"]:
            hard_blocks.append("没有声明能力边界与排除条件")
        if not content["quality_gates"] or not content["validation_plan"]:
            hard_blocks.append("没有可验证的质量门槛或验证计划")
        if not provenance["source_url"] or not provenance["evidence"]:
            hard_blocks.append("缺少可追溯来源或证据摘要")
        standard = self._standard_snapshot()
        if not standard["fingerprint"]:
            hard_blocks.append("当前制作标准不可用，无法做兼容性审核")
        score = sum(dimensions.values())
        if score < MIN_USEFUL_SCORE:
            hard_blocks.append(
                f"价值评分 {score} 低于入库门槛 {MIN_USEFUL_SCORE}")
        result = {
            "schema": ASSESSMENT_SCHEMA,
            "gate": "pass" if not hard_blocks else "reject",
            "useful": not hard_blocks,
            "score": score,
            "minimum_score": MIN_USEFUL_SCORE,
            "dimensions": dimensions,
            "hard_blocks": hard_blocks,
            "reasons": [
                "适用任务、调用触发词和排除条件明确",
                "方法可执行且带质量门槛与验证计划",
                "来源可追溯，并记录与当前制作标准的对齐快照",
            ] if not hard_blocks else [
                "候选未通过知识大脑入库门禁，不会写入知识版本库",
            ],
            "standard_snapshot": standard,
        }
        return item, result

    @staticmethod
    def _version(row):
        if row is None:
            return None
        result = dict(row)
        for key in (
                "content", "applicability", "provenance", "assessment",
                "standard_snapshot"):
            result[key] = _json(result.get(key), {})
        return result

    def get(self, knowledge_key, version=None):
        key = _slug(knowledge_key)
        if version is None:
            row = self.db.query_one(
                "SELECT v.* FROM firefire_knowledge_state s "
                "JOIN firefire_knowledge_versions v "
                "ON v.id=COALESCE(s.candidate_version_id,"
                "s.active_version_id) WHERE s.knowledge_key=?",
                (key,))
        else:
            row = self.db.query_one(
                "SELECT * FROM firefire_knowledge_versions "
                "WHERE knowledge_key=? AND version=?",
                (key, int(version)))
        return self._version(row)

    def list(self):
        standard = self._standard_snapshot()
        rows = self.db.query(
            "SELECT s.status AS state_status, s.active_version_id, "
            "s.candidate_version_id, s.reviewed_by, s.review_note, "
            "v.* FROM firefire_knowledge_state s "
            "JOIN firefire_knowledge_versions v "
            "ON v.id=COALESCE(s.candidate_version_id,s.active_version_id) "
            "ORDER BY s.updated_at DESC")
        result = []
        for row in rows:
            item = self._version(row)
            snapshot = item.get("standard_snapshot") or {}
            item["standard_status"] = (
                "current"
                if snapshot.get("fingerprint") == standard["fingerprint"]
                else "needs_review")
            result.append(item)
        return result

    def create_candidate(self, payload):
        item, assessment = self.assess(payload)
        if assessment["gate"] != "pass":
            raise AifosError(
                "知识候选未通过价值门禁：" + "；".join(
                    assessment["hard_blocks"]))
        standard = assessment["standard_snapshot"]
        fingerprint = hashlib.sha256(_canonical({
            "item": item,
            "standard_fingerprint": standard["fingerprint"],
        }).encode("utf-8")).hexdigest()
        duplicate = self.db.query_one(
            "SELECT version FROM firefire_knowledge_versions "
            "WHERE knowledge_key=? AND fingerprint=?",
            (item["knowledge_key"], fingerprint))
        if duplicate is not None:
            raise AifosError(
                f"相同知识内容已存在于 v{int(duplicate['version'])}，"
                "无需重复入库")
        latest = self.db.query_one(
            "SELECT COALESCE(MAX(version),0) AS version "
            "FROM firefire_knowledge_versions WHERE knowledge_key=?",
            (item["knowledge_key"],))
        version = int(latest["version"] or 0) + 1
        timestamp = now()
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO firefire_knowledge_versions("
                "knowledge_key,version,title,kind,domain,summary,content,"
                "applicability,provenance,assessment,standard_snapshot,"
                "fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item["knowledge_key"], version, item["title"],
                    item["kind"], item["domain"], item["summary"],
                    json.dumps(item["content"], ensure_ascii=False),
                    json.dumps(item["applicability"], ensure_ascii=False),
                    json.dumps(item["provenance"], ensure_ascii=False),
                    json.dumps(assessment, ensure_ascii=False),
                    json.dumps(standard, ensure_ascii=False),
                    fingerprint, timestamp,
                ))
            state = conn.execute(
                "SELECT active_version_id FROM firefire_knowledge_state "
                "WHERE knowledge_key=?", (item["knowledge_key"],)).fetchone()
            active_id = state["active_version_id"] if state else None
            conn.execute(
                "INSERT INTO firefire_knowledge_state("
                "knowledge_key,active_version_id,candidate_version_id,status,"
                "reviewed_by,review_note,updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(knowledge_key) DO UPDATE SET "
                "candidate_version_id=excluded.candidate_version_id,"
                "status='review',reviewed_by='',review_note='',"
                "updated_at=excluded.updated_at",
                (
                    item["knowledge_key"], active_id, cur.lastrowid, "review",
                    "", "", timestamp,
                ))
            version_id = cur.lastrowid
        result = self._version(self.db.query_one(
            "SELECT * FROM firefire_knowledge_versions WHERE id=?",
            (version_id,)))
        result["state_status"] = "review"
        result["standard_status"] = "current"
        return result

    def publish(self, knowledge_key, *, approved_by="human", note=""):
        key = _slug(knowledge_key)
        state = self.db.query_one(
            "SELECT * FROM firefire_knowledge_state WHERE knowledge_key=?",
            (key,))
        if state is None or not state["candidate_version_id"]:
            raise AifosError("该知识没有等待审核的新版本")
        candidate = self._version(self.db.query_one(
            "SELECT * FROM firefire_knowledge_versions WHERE id=?",
            (state["candidate_version_id"],)))
        current = self._standard_snapshot()
        if (candidate.get("standard_snapshot") or {}).get(
                "fingerprint") != current["fingerprint"]:
            raise AifosError("候选知识未对齐当前最新制作标准，请先刷新后再审核")
        reviewer = str(approved_by or "").strip()
        if not reviewer:
            raise AifosError("知识激活必须记录人工审核人")
        self.db.execute(
            "UPDATE firefire_knowledge_state SET active_version_id=?,"
            "candidate_version_id=NULL,status='active',reviewed_by=?,"
            "review_note=?,updated_at=? WHERE knowledge_key=?",
            (
                candidate["id"], reviewer, str(note or "").strip(),
                now(), key,
            ))
        result = candidate
        result["state_status"] = "active"
        result["standard_status"] = "current"
        result["reviewed_by"] = reviewer
        return result

    def refresh_alignment(self, knowledge_key):
        key = _slug(knowledge_key)
        state = self.db.query_one(
            "SELECT active_version_id FROM firefire_knowledge_state "
            "WHERE knowledge_key=?", (key,))
        if state is None or not state["active_version_id"]:
            raise AifosError("只有已激活知识可以刷新标准对齐")
        active = self._version(self.db.query_one(
            "SELECT * FROM firefire_knowledge_versions WHERE id=?",
            (state["active_version_id"],)))
        payload = {
            "knowledge_key": active["knowledge_key"],
            "title": active["title"],
            "kind": active["kind"],
            "domain": active["domain"],
            "summary": active["summary"],
            "content": deepcopy(active["content"]),
            "applicability": deepcopy(active["applicability"]),
            "provenance": deepcopy(active["provenance"]),
        }
        payload["provenance"]["evidence"] = _strings(
            payload["provenance"].get("evidence")) + [
                "已创建新候选版本，等待人工复核其与当前制作标准的兼容性。"
            ]
        return self.create_candidate(payload)

    def resolve(self, *, stage="", task_type="", query="", tags=None,
                limit=4):
        stage = str(stage or "").strip()
        task_type = str(task_type or "").strip()
        query_text = str(query or "").strip().lower()
        tag_set = {item.lower() for item in _strings(tags)}
        current = self._standard_snapshot()
        rows = self.db.query(
            "SELECT v.* FROM firefire_knowledge_state s "
            "JOIN firefire_knowledge_versions v "
            "ON v.id=s.active_version_id "
            "WHERE s.status='active' AND s.active_version_id IS NOT NULL")
        matches = []
        stale = []
        for row in rows:
            item = self._version(row)
            snapshot = item.get("standard_snapshot") or {}
            if snapshot.get("fingerprint") != current["fingerprint"]:
                stale.append(item["knowledge_key"])
                continue
            app = item.get("applicability") or {}
            stages = set(app.get("stages") or [])
            task_types = set(app.get("task_types") or [])
            if stage and stage not in stages and "cross_stage" not in stages:
                continue
            if task_type and task_type not in task_types:
                continue
            relevance = (5 if stage else 0) + (4 if task_type else 0)
            haystack = " ".join(
                [item["title"], item["summary"]]
                + list(app.get("triggers") or [])
                + list(app.get("tags") or [])).lower()
            if query_text:
                relevance += sum(
                    2 for trigger in app.get("triggers") or []
                    if str(trigger).lower() in query_text)
                relevance += sum(
                    1 for token in query_text.split()
                    if token and token in haystack)
            if tag_set:
                relevance += 2 * len(
                    tag_set.intersection(
                        str(tag).lower() for tag in app.get("tags") or []))
            if not (stage or task_type or query_text or tag_set):
                relevance = 1
            if relevance <= 0:
                continue
            item["relevance"] = relevance
            item["standard_status"] = "current"
            item["callable_context"] = self._callable_context(item)
            matches.append(item)
        matches.sort(key=lambda item: (
            -int(item.get("relevance") or 0),
            -int((item.get("assessment") or {}).get("score") or 0),
            -int(item.get("version") or 0),
        ))
        return {
            "schema": "firefire.knowledge-resolution/v1",
            "stage": stage,
            "task_type": task_type,
            "query": query,
            "matches": matches[:max(1, min(int(limit), 10))],
            "skipped_stale": stale,
            "standard_snapshot": current,
        }

    @staticmethod
    def _callable_context(item):
        content = item.get("content") or {}
        parts = [
            f"【知识大脑·{item['title']} v{item['version']}】",
            item.get("summary") or "",
        ]
        if content.get("principles"):
            parts.append("原则：" + "；".join(content["principles"]))
        if content.get("workflow"):
            parts.append("执行：" + "；".join(content["workflow"]))
        if content.get("quality_gates"):
            parts.append("质检：" + "；".join(content["quality_gates"]))
        if content.get("limitations"):
            parts.append("边界：" + "；".join(content["limitations"]))
        return "\n".join(part for part in parts if part)

    def ensure_seed(self):
        """把用户投喂且通过价值门禁的内置知识激活到新库。"""
        for seed in BUILTIN_SEEDS:
            state = self.db.query_one(
                "SELECT active_version_id FROM firefire_knowledge_state "
                "WHERE knowledge_key=?",
                (seed["knowledge_key"],))
            if state is not None and state["active_version_id"]:
                continue
            try:
                candidate = self.create_candidate(seed)
                self.publish(
                    candidate["knowledge_key"],
                    approved_by="user-fed-2026-07-30",
                    note=(
                        "用户要求学习外部课程；该条已完成去重、价值审核、"
                        "适用范围与验证计划检查，允许进入 AIFOS 知识大脑。"
                    ),
                )
            except (AifosError, sqlite3.IntegrityError):
                # 并行 worker 在全新 workspace 同时初始化时，另一连接可能
                # 已完成同一条知识；只有确认激活指针存在时才吞掉竞态。
                state = self.db.query_one(
                    "SELECT active_version_id FROM firefire_knowledge_state "
                    "WHERE knowledge_key=?",
                    (seed["knowledge_key"],))
                if state is None or not state["active_version_id"]:
                    raise
