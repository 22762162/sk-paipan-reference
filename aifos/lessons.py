"""出错观察库：保留质检证据，但不自动升级为永久提示词规则。

闭环:质检发现问题(如"古代场景出现笔记本电脑""物体被拉长变形")
→ 自动归档为项目级观察(同类问题聚合计数)
→ 只有人工确认适用范围后才允许注入提示词
→ 当前镜头的临时修订通过或重试一次后立即失效。

教训存放在资产中心(kind="lesson"),按归一化指纹聚合;不需要
额外表结构,跨集共享,断点续产/重启后依然生效。
"""

import hashlib
import json
import re
import time

# 注入提示词的教训上限:太多会稀释注意力,取最高频的前几条
MAX_INJECTED = 6
# 单条教训文字上限
MAX_TEXT = 120

# 环境/流程类问题不是"画错了",不进教训库(如产线不可用、文件缺失)
_SKIP_PATTERNS = (
    "质检产线不可用", "质检未确认", "质检未核对", "未给出具体原因",
    "文件不存在", "为空", "API", "不可用",
)


def _normalize(issue):
    """归一化教训文本:去掉镜头号/文件路径等易变细节,便于聚合。"""
    text = str(issue or "").strip()
    text = re.sub(r"镜头\s*\d+", "镜头", text)
    text = re.sub(r"第\s*\d+\s*[张个只人次]", "第N", text)
    text = re.sub(r"/\S+\.(png|jpg|jpeg|webp|svg)", "<图片>", text)
    text = re.sub(r"\s+", "", text)
    return text[:MAX_TEXT]


def lesson_worthy(issue):
    text = str(issue or "").strip()
    if len(text) < 6:
        return False
    return not any(pattern in text for pattern in _SKIP_PATTERNS)


def _fingerprint(normalized):
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def record_lessons(assets, project_id, issues, category=""):
    """记录质检观察；默认 pending_review，绝不自动污染后续提示词。"""
    recorded = 0
    for issue in issues or []:
        if not lesson_worthy(issue):
            continue
        normalized = _normalize(issue)
        if not normalized:
            continue
        name = _fingerprint(normalized)
        row = assets.latest(project_id, "lesson", name)
        meta = {}
        if row is not None:
            raw = row["meta"]
            if isinstance(raw, str):
                try:
                    meta = json.loads(raw or "{}")
                except ValueError:
                    meta = {}
            else:
                meta = raw or {}
        categories = dict(meta.get("categories") or {})
        if category:
            categories[category] = int(categories.get(category, 0)) + 1
        assets.register(
            project_id, "lesson", name,
            meta={
                "issue": normalized,
                "example": str(issue)[:MAX_TEXT * 2],
                "count": int(meta.get("count", 0)) + 1,
                "categories": categories,
                "last_at": time.time(),
                "scope": meta.get("scope") or "qc_observation",
                "status": meta.get("status") or "pending_review",
                "approved_for_prompt": bool(
                    meta.get("approved_for_prompt", False)),
            }, new_version=row is not None)
        recorded += 1
    return recorded


def project_lessons(assets, project_id, limit=50):
    """项目全部教训,按出现次数降序(供接口/看板展示)。"""
    rows = []
    for row in assets.active_list(project_id, kind="lesson"):
        raw = row["meta"]
        if isinstance(raw, str):
            try:
                meta = json.loads(raw or "{}")
            except ValueError:
                continue
        else:
            meta = raw or {}
        if not meta.get("issue"):
            continue
        rows.append({
            "issue": meta["issue"],
            "count": int(meta.get("count", 1)),
            "categories": meta.get("categories") or {},
            "last_at": meta.get("last_at"),
            "scope": meta.get("scope") or "qc_observation",
            "status": meta.get("status") or "pending_review",
            "approved_for_prompt": bool(
                meta.get("approved_for_prompt", False)),
        })
    rows.sort(key=lambda item: (-item["count"], -(item["last_at"] or 0)))
    return rows[:limit]


def lesson_lines(assets, project_id, limit=MAX_INJECTED):
    """只返回人工批准的项目规则；旧记录也默认不注入。"""
    return [f"{item['issue']}(此前已出错{item['count']}次)"
            for item in project_lessons(assets, project_id, limit=50)
            if item.get("approved_for_prompt")
            and item.get("status") == "approved"][:limit]


def lessons_block(assets, project_id, limit=MAX_INJECTED):
    """拼接为提示词片段;没有教训时返回空串。"""
    lines = lesson_lines(assets, project_id, limit=limit)
    if not lines:
        return ""
    return ("历史出错教训(本项目此前真实犯过的错误,本次严禁重犯,"
            "逐条自查):" + ";".join(lines))
