"""出错观察库：保留质检证据，但不自动升级为永久提示词规则。

闭环:质检发现问题(如"古代场景出现笔记本电脑""物体被拉长变形")
→ 自动归档为项目级观察(同类问题聚合计数)
→ 只有人工确认适用范围后才允许注入提示词
→ 当前镜头的临时修订通过或重试一次后立即失效。

教训存放在资产中心(kind="lesson"),按归一化指纹聚合;不需要
额外表结构,跨集共享,断点续产/重启后依然生效。

教训按 **domain** 分域,注入时不得串味:
- ``image``  出图/质检域(默认;历史记录没有 domain 字段,一律视为本域)
- ``script`` 剧本解析域(人物误识别、旁白音效被当人物、场次名单错乱)
剧本域的教训要进编剧提示词,出图域的要进出图/视频提示词。把剧本教训
塞进出图提示词只会稀释注意力,反之亦然。
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


DOMAIN_IMAGE = "image"
DOMAIN_SCRIPT = "script"


def _row_domain(meta):
    """历史记录没有 domain 字段,全部来自出图质检,归入 image 域。"""
    return str((meta or {}).get("domain") or DOMAIN_IMAGE)


def record_lessons(assets, project_id, issues, category="",
                   domain=DOMAIN_IMAGE):
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
                # 同一条教训不跨域改写:首次记入哪个域就留在哪个域
                "domain": _row_domain(meta) if row is not None else domain,
                "last_at": time.time(),
                "scope": meta.get("scope") or "qc_observation",
                "status": meta.get("status") or "pending_review",
                "approved_for_prompt": bool(
                    meta.get("approved_for_prompt", False)),
            }, new_version=row is not None)
        recorded += 1
    return recorded


def project_lessons(assets, project_id, limit=50, domain=None):
    """项目教训,按出现次数降序(供接口/看板展示)。

    ``domain=None`` 返回全部(看板用);传入域名则只返回该域(注入用)。
    """
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
        row_domain = _row_domain(meta)
        if domain is not None and row_domain != domain:
            continue
        rows.append({
            "issue": meta["issue"],
            "count": int(meta.get("count", 1)),
            "categories": meta.get("categories") or {},
            "domain": row_domain,
            "last_at": meta.get("last_at"),
            "scope": meta.get("scope") or "qc_observation",
            "status": meta.get("status") or "pending_review",
            "approved_for_prompt": bool(
                meta.get("approved_for_prompt", False)),
        })
    rows.sort(key=lambda item: (-item["count"], -(item["last_at"] or 0)))
    return rows[:limit]


def lesson_lines(assets, project_id, limit=MAX_INJECTED,
                 domain=DOMAIN_IMAGE):
    """只返回人工批准的项目规则；旧记录也默认不注入。"""
    return [f"{item['issue']}(此前已出错{item['count']}次)"
            for item in project_lessons(
                assets, project_id, limit=50, domain=domain)
            if item.get("approved_for_prompt")
            and item.get("status") == "approved"][:limit]


def lessons_block(assets, project_id, limit=MAX_INJECTED,
                  domain=DOMAIN_IMAGE):
    """拼接为提示词片段;没有教训时返回空串。"""
    lines = lesson_lines(assets, project_id, limit=limit, domain=domain)
    if not lines:
        return ""
    return ("历史出错教训(本项目此前真实犯过的错误,本次严禁重犯,"
            "逐条自查):" + ";".join(lines))


def script_lessons_block(assets, project_id, limit=MAX_INJECTED):
    """剧本域教训,供编剧/剧本分析提示词使用。"""
    lines = lesson_lines(assets, project_id, limit=limit,
                         domain=DOMAIN_SCRIPT)
    if not lines:
        return ""
    return ("剧本解析历史教训(本项目此前真实出过的错,本次严禁重犯,"
            "逐条自查):" + ";".join(lines))
