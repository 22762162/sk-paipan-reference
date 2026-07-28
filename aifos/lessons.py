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
            # 指纹即资产名,人工审批时用它定位这条教训
            "id": row["name"],
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


def set_lesson_approval(assets, project_id, lesson_id, approved,
                        scope="project_rule"):
    """人工审批一条教训:批准后才允许注入后续提示词。

    审批是这套闭环里唯一的"升级"通道——系统只负责观察和归档,把一次性
    的偶发问题升级成永久规则必须由人决定,否则修一次错就多一条永久约
    束,几轮之后提示词里全是互相冲突的禁令。
    """
    row = assets.latest(project_id, "lesson", str(lesson_id))
    if row is None:
        raise KeyError(lesson_id)
    raw = row["meta"]
    if isinstance(raw, str):
        try:
            meta = json.loads(raw or "{}")
        except ValueError:
            meta = {}
    else:
        meta = dict(raw or {})
    if not meta.get("issue"):
        raise KeyError(lesson_id)
    meta["approved_for_prompt"] = bool(approved)
    meta["status"] = "approved" if approved else "pending_review"
    meta["scope"] = scope if approved else "qc_observation"
    meta["reviewed_at"] = time.time()
    assets.register(project_id, "lesson", str(lesson_id), meta=meta,
                    new_version=True)
    return {
        "id": str(lesson_id),
        "issue": meta["issue"],
        "domain": _row_domain(meta),
        "status": meta["status"],
        "approved_for_prompt": meta["approved_for_prompt"],
    }


DISTILLED_SCOPE = "distilled_project_rule"
DISTILL_MIN_PENDING = 12       # 少于这么多条不值得归纳
DISTILL_MIN_COUNT = 2          # 只喂反复出现过的观察,一次性抱怨不进


def pending_observations(assets, project_id, domain=DOMAIN_IMAGE,
                         min_count=DISTILL_MIN_COUNT, limit=40):
    """待归纳的原始观察(按出现次数降序):只取反复出现的。

    一次性观察多半是判官对单镜的即兴描述,归纳价值低、噪音大。
    """
    return [
        item for item in project_lessons(
            assets, project_id, limit=200, domain=domain)
        if not item.get("approved_for_prompt")
        and int(item.get("count") or 1) >= min_count
    ][:limit]


def adopt_distilled_rules(assets, project_id, rules, domain=DOMAIN_IMAGE):
    """把归纳出的通用规则登记为已批准教训(直接进后续提示词)。

    归纳规则是"经验的结论"而不是"原始观察":它已被明确要求不含镜头级
    专名、可跨镜执行,所以自动采纳;原始观察仍保持 pending,人工看板
    上照常可查可撤。返回实际写入的条数。
    """
    written = 0
    for index, rule in enumerate(rules or [], 1):
        text = str((rule or {}).get("rule")
                   if isinstance(rule, dict) else rule or "").strip()
        if not text:
            continue
        lesson_id = f"distilled:{domain}:{_digest(text)}"
        assets.register(project_id, "lesson", lesson_id, meta={
            "issue": text,
            "domain": domain,
            "count": int((rule or {}).get("count") or 1)
            if isinstance(rule, dict) else 1,
            "status": "approved",
            "approved_for_prompt": True,
            "scope": DISTILLED_SCOPE,
            "source": "ai_distilled",
            "covers": list((rule or {}).get("covers") or [])
            if isinstance(rule, dict) else [],
            "reviewed_at": time.time(),
        }, new_version=True)
        written += 1
    return written


def _digest(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


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
