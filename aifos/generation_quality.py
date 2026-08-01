"""把历史生成失败收敛为短规则、前置合同检查和非阻断复盘。

这个模块不接触数据库、资产或生成服务，供导演层在三个时间点调用：

* 生成前：只检查能够确定的合同矛盾，逐镜返回问题，不抛异常；
* 写提示词时：只选择与本镜上下文相关的最多五条短规则；
* 生成后：内容问题只作建议，只有供应商或文件技术错误可以重试。

这样可以真正利用历史失败，又不会把所有旧问题塞入每个新镜头，或把
审美意见重新变成阻断整集生产的质检闸门。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


MAX_CONTEXT_RULES = 5

QUALITY_CATEGORIES = (
    "identity",
    "gender_age",
    "count",
    "wardrobe",
    "era",
    "text",
    "camera_contract",
    "spatial_logic",
    "prop_physics",
    "lighting",
    "video_state_chain",
    "video_camera_motion",
    "video_identity_continuity",
    "video_prop_state",
    "reference_budget",
    "audio_lipsync",
    "technical_provider",
    "technical_encoding",
)

CONTENT_CATEGORIES = frozenset(QUALITY_CATEGORIES[:-2])
TECHNICAL_CATEGORIES = frozenset(QUALITY_CATEGORIES[-2:])


@dataclass(frozen=True)
class QualityRule:
    """一条可以原样注入生成提示词的短规则。"""

    category: str
    instruction: str
    modalities: tuple[str, ...]
    keywords: tuple[str, ...]
    priority: int = 50


_RULES = (
    QualityRule(
        "identity",
        "人脸、发型和妆造严格沿用已锁定人物母图。",
        ("image", "video"),
        ("人物", "角色", "人脸", "脸型", "五官", "发型", "妆造", "identity"),
        100,
    ),
    QualityRule(
        "gender_age",
        "性别与年龄段严格按人物设定，不增龄、不变性别。",
        ("image", "video"),
        ("性别", "年龄", "少年", "青年", "中年", "老年", "gender", "age"),
        95,
    ),
    QualityRule(
        "count",
        "画面人数严格等于镜头合同，不增人、不复制人。",
        ("image", "video"),
        ("人数", "单人", "双人", "多人", "独自", "只有", "people", "count"),
        95,
    ),
    QualityRule(
        "wardrobe",
        "服装、配饰和职业着装按本镜设定保持连续。",
        ("image", "video"),
        ("服装", "衣服", "穿着", "配饰", "头饰", "帽", "制服", "职业装", "wardrobe"),
        85,
    ),
    QualityRule(
        "era",
        "场景、服饰和道具只使用剧情时代可存在的形制。",
        ("image", "video"),
        ("时代", "古代", "现代", "明代", "清代", "朝代", "历史", "era"),
        90,
    ),
    QualityRule(
        "text",
        "可读文字只生成合同给定原文与版式，禁止乱码、错字和额外文字。",
        ("image", "video"),
        ("文字", "屏幕", "书页", "牌匾", "信件", "字幕", "logo", "text"),
        90,
    ),
    QualityRule(
        "camera_contract",
        "只执行一个明确景别、机位、焦段和运镜，不混入互斥镜头语言。",
        ("image", "video"),
        ("镜头", "景别", "机位", "焦段", "构图", "俯拍", "仰拍", "特写", "全景", "camera"),
        100,
    ),
    QualityRule(
        "spatial_logic",
        "人物、相机和物体按空间图站位与遮挡，禁止穿模、越轴和瞬移。",
        ("image", "video"),
        ("空间", "站位", "走位", "前后", "左右", "遮挡", "轴线", "穿模", "spatial"),
        100,
    ),
    QualityRule(
        "prop_physics",
        "道具位置、朝向、持有人和受力连续，接触后才可移动。",
        ("image", "video"),
        ("道具", "物品", "手持", "书册", "拿", "放", "递", "重力", "受力", "物理", "prop", "physics"),
        100,
    ),
    QualityRule(
        "lighting",
        "主光方向、色温和时间保持连续，不新增矛盾光源。",
        ("image", "video"),
        ("光影", "灯光", "光源", "色温", "曝光", "白天", "夜晚", "lighting"),
        70,
    ),
    QualityRule(
        "video_state_chain",
        "复杂动作明确起始、过渡、结束至少三态，各态必须可物理到达。",
        ("video",),
        ("复杂动作", "动作链", "起始", "过渡", "结束", "状态", "state chain"),
        100,
    ),
    QualityRule(
        "video_camera_motion",
        "视频只执行合同指定运镜，保持主体尺度和轴线连续。",
        ("video",),
        ("运镜", "推镜", "拉镜", "摇镜", "跟拍", "环绕", "camera motion"),
        90,
    ),
    QualityRule(
        "video_identity_continuity",
        "首尾帧与全程沿用锁定人脸、发型和妆造，不中途变脸。",
        ("video",),
        ("首帧", "尾帧", "连续", "变脸", "身份连续", "identity continuity"),
        100,
    ),
    QualityRule(
        "video_prop_state",
        "道具持有人、位置、朝向和开合状态逐帧连续。",
        ("video",),
        ("道具状态", "持有人", "开合", "逐帧", "prop state"),
        95,
    ),
    QualityRule(
        "reference_budget",
        "提交前按模型版本核对参考图总数和素材位，超限先精简职责。",
        ("image", "video"),
        ("参考图", "素材", "参考预算", "seedance", "reference"),
        95,
    ),
    QualityRule(
        "audio_lipsync",
        "对白逐句绑定说话人，使用即梦内置配音并核验可见口型同步。",
        ("video",),
        ("对白", "说话人", "配音", "口型", "声音", "audio", "lipsync"),
        85,
    ),
    QualityRule(
        "technical_provider",
        "生成前确认模型端点可用，供应商失败只按技术错误重试。",
        ("image", "video"),
        ("供应商", "端点", "接口", "超时", "限流", "provider", "timeout"),
        60,
    ),
    QualityRule(
        "technical_encoding",
        "成片必须实测分辨率、帧率、时长、解码和音轨完整性。",
        ("video",),
        ("编码", "分辨率", "帧率", "时长", "解码", "音轨", "encoding"),
        60,
    ),
)

RULES_BY_CATEGORY = {rule.category: rule for rule in _RULES}


def _context_text(context: Any) -> str:
    if isinstance(context, str):
        return context.strip().lower()
    if isinstance(context, Mapping):
        return json.dumps(
            context, ensure_ascii=False, sort_keys=True, default=str,
        ).lower()
    if isinstance(context, Sequence) and not isinstance(context, (bytes, bytearray)):
        return " ".join(str(item) for item in context).lower()
    return str(context or "").lower()


def infer_quality_categories(context: Any, *, modality: str = "") -> tuple[str, ...]:
    """从本镜上下文推断相关类别；不读取历史，避免历史反向污染上下文。"""
    text = _context_text(context)
    normalized_modality = str(modality or "").strip().lower()
    found = []
    for rule in _RULES:
        if normalized_modality and normalized_modality not in rule.modalities:
            continue
        if any(_positive_keyword_match(text, keyword.lower())
               for keyword in rule.keywords):
            found.append(rule.category)
    return tuple(found)


def _positive_keyword_match(text: str, keyword: str) -> bool:
    """忽略“无人物/不含文字”等明确否定，避免空镜被人物规则污染。"""
    start = 0
    while True:
        index = text.find(keyword, start)
        if index < 0:
            return False
        prefix = text[max(0, index - 4):index]
        if not re.search(r"(?:无|没有|不含|禁止|无需)\s*$", prefix):
            return True
        start = index + len(keyword)


def select_quality_rules(
        *,
        categories: Iterable[str] = (),
        context: Any = "",
        modality: str = "",
        historical_failures: Mapping[str, Any] | None = None,
        limit: int = MAX_CONTEXT_RULES,
) -> tuple[QualityRule, ...]:
    """选择本镜真正相关的规则，硬上限五条。

    ``historical_failures`` 只给当前已相关类别加权，绝不能单独把一个旧
    类别带进新镜头。调用方即使传入全项目失败统计，也不会造成提示词
    污染。类别可由调用方明确给出，也可由当前镜头文本推断。
    """
    normalized_modality = str(modality or "").strip().lower()
    explicit = {
        str(category).strip()
        for category in categories or ()
        if str(category).strip() in RULES_BY_CATEGORY
    }
    inferred = set(infer_quality_categories(
        context, modality=normalized_modality))
    relevant = explicit | inferred
    if not relevant:
        return ()

    history = historical_failures if isinstance(
        historical_failures, Mapping) else {}
    ranked = []
    for category in relevant:
        rule = RULES_BY_CATEGORY[category]
        if normalized_modality and normalized_modality not in rule.modalities:
            continue
        try:
            repeats = max(0, int(history.get(category, 0)))
        except (TypeError, ValueError):
            repeats = 0
        score = (
            int(category in explicit) * 10_000
            + int(category in inferred) * 5_000
            + min(repeats, 999) * 10
            + rule.priority
        )
        ranked.append((score, rule.category, rule))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    try:
        requested_limit = int(limit)
    except (TypeError, ValueError):
        requested_limit = MAX_CONTEXT_RULES
    safe_limit = max(0, min(requested_limit, MAX_CONTEXT_RULES))
    return tuple(rule for _score, _category, rule in ranked[:safe_limit])


def quality_rule_lines(**kwargs: Any) -> tuple[str, ...]:
    """返回可直接拼入提示词的纯指令，不附带历史失败长解释。"""
    return tuple(rule.instruction for rule in select_quality_rules(**kwargs))


@dataclass(frozen=True)
class BlockingIssue:
    """只阻断当前镜头生成前调用的确定性合同问题。"""

    shot_id: str
    code: str
    category: str
    message: str
    blocking: bool = True


_CAMERA_FIELDS = {
    "shot_scale": ("shot_scale", "scale", "景别"),
    "angle": ("angle", "camera_angle", "机位"),
    "lens": ("lens", "focal_length", "焦段"),
    "movement": ("movement", "camera_motion", "运镜"),
}

_CAMERA_CONTRADICTIONS = {
    "shot_scale": (
        ("特写", "全景"), ("近景", "大全景"),
        ("close-up", "wide shot"),
    ),
    "angle": (
        ("俯拍", "仰拍"), ("鸟瞰", "低机位"),
        ("top-down", "low-angle"),
    ),
    "lens": (("广角", "长焦"), ("wide-angle", "telephoto")),
    "movement": (
        ("固定镜头", "跟拍"), ("静止镜头", "环绕"),
        ("static camera", "tracking shot"),
    ),
}

_ANCIENT_ERAS = (
    "古代", "先秦", "秦代", "汉代", "唐代", "宋代", "元代", "明代",
    "清代", "民国以前", "ancient", "ming dynasty", "qing dynasty",
)
_MODERN_PROPS = (
    "笔记本电脑", "电脑屏幕", "手机", "智能手机", "平板电脑", "液晶屏",
    "汽车", "电灯", "塑料瓶", "信用卡", "laptop", "smartphone", "tablet",
    "lcd", "led screen",
)

SEEDANCE_TOTAL_REFERENCE_LIMITS = {"2.0": 9, "2.5": 50}
SEEDANCE_ASSET_REFERENCE_LIMITS = {"2.0": 7, "2.5": 40}


def _shot_id(shot: Mapping[str, Any], fallback: int | None = None) -> str:
    value = shot.get("shot_id", shot.get("shot_no", shot.get("id")))
    if value in (None, ""):
        value = fallback if fallback is not None else "unknown"
    return str(value)


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _camera_contract(shot: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("camera_contract", "camera"):
        value = shot.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _field_values(shot: Mapping[str, Any], names: Sequence[str]) -> list[str]:
    camera = _camera_contract(shot)
    values: list[str] = []
    for name in names:
        value = camera.get(name, shot.get(name))
        for item in _as_list(value):
            text = str(item or "").strip().lower()
            if text and text not in values:
                values.append(text)
    return values


def _prompt_text(shot: Mapping[str, Any]) -> str:
    parts = []
    for key in ("prompt", "description", "action", "story", "story_context"):
        value = shot.get(key)
        if value:
            parts.append(_context_text(value))
    return " ".join(parts)


def _issue(shot_id: str, code: str, category: str, message: str) -> BlockingIssue:
    return BlockingIssue(shot_id, code, category, message)


def _camera_issues(shot: Mapping[str, Any], shot_id: str) -> list[BlockingIssue]:
    issues = []
    prompt = _prompt_text(shot)
    for dimension, field_names in _CAMERA_FIELDS.items():
        values = _field_values(shot, field_names)
        contradiction = len(values) > 1
        if not contradiction:
            contradiction = any(
                left in prompt and right in prompt
                for left, right in _CAMERA_CONTRADICTIONS[dimension]
            )
        if contradiction:
            label = {
                "shot_scale": "景别", "angle": "机位",
                "lens": "焦段", "movement": "运镜",
            }[dimension]
            issues.append(_issue(
                shot_id,
                f"camera_contract.{dimension}_conflict",
                "camera_contract",
                f"{label}含互斥描述；本镜只保留一个明确值。",
            ))
    return issues


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _people_count_issue(
        shot: Mapping[str, Any], shot_id: str,
) -> list[BlockingIssue]:
    expected = None
    for key in ("expected_people_count", "people_count", "visible_people_count"):
        expected = _positive_int(shot.get(key))
        if expected is not None:
            break
    visible = shot.get("visible_characters")
    if visible is None and shot.get("characters_are_visible") is True:
        visible = shot.get("characters")
    actual = len(visible) if isinstance(visible, (list, tuple)) else None
    if expected is not None and actual is not None and expected != actual:
        return [_issue(
            shot_id,
            "count.visible_characters_mismatch",
            "count",
            f"合同人数为{expected}，可见人物清单为{actual}；先统一人数。",
        )]
    return []


def _prop_names(value: Any) -> list[str]:
    output = []
    for item in _as_list(value):
        if isinstance(item, Mapping):
            item = item.get("name", item.get("label", item.get("type", "")))
        text = str(item or "").strip().lower()
        if text:
            output.append(text)
    return output


def _era_prop_issues(shot: Mapping[str, Any], shot_id: str) -> list[BlockingIssue]:
    era = str(
        shot.get("era") or shot.get("period")
        or shot.get("era_context") or "").strip().lower()
    if not era or not any(token in era for token in _ANCIENT_ERAS):
        return []
    if bool(shot.get("allow_anachronism") or shot.get("cross_era_prop_allowed")):
        return []
    # A declared prop list is authoritative but not exhaustive: the authored
    # main action may still mention an undeclared object. Check both instead
    # of letting any one valid prop hide an unrelated anachronism in prose.
    haystack = " ".join([
        *_prop_names(shot.get("props")),
        _prompt_text(shot),
    ])
    sanctioned = " ".join(_prop_names(
        shot.get("sanctioned_anachronisms")))
    conflicts = [
        prop for prop in _MODERN_PROPS
        if prop in haystack and prop not in sanctioned
    ]
    if not conflicts:
        return []
    return [_issue(
        shot_id,
        "era.modern_prop_in_ancient_scene",
        "era",
        "古代镜头含现代道具：" + "、".join(conflicts[:3])
        + "；删除或显式标记跨时代剧情依据。",
    )]


def _physical_spatial_issues(
        shot: Mapping[str, Any], shot_id: str,
) -> list[BlockingIssue]:
    """Require the authored physical/spatial inputs only when declared.

    This is a presence/validity check, not an aesthetic judge.  It therefore
    cannot reject a shot merely because a blocking map is optional.
    """
    issues = []
    spatial = shot.get("spatial_blocking")
    spatial = spatial if isinstance(spatial, Mapping) else {}
    if shot.get("spatial_required"):
        spatial_ref = str(
            shot.get("spatial_ref")
            or spatial.get("spatial_reference_uri") or "").strip()
        if not spatial or not spatial_ref:
            issues.append(_issue(
                shot_id,
                "spatial.required_contract_missing",
                "spatial_logic",
                "本镜要求空间调度，但缺少空间合同或空间参考图。",
            ))
    if shot.get("physical_contract_required"):
        physical = shot.get("physical_contract")
        physical = physical if isinstance(physical, Mapping) else {}
        rules = physical.get("rules")
        if not isinstance(rules, (list, tuple)) or not any(
                str(rule or "").strip() for rule in rules):
            issues.append(_issue(
                shot_id,
                "physics.required_contract_missing",
                "prop_physics",
                "本镜含道具或空间运动，但缺少可执行物理合同。",
            ))
        prompt_text = _prompt_text(shot).lower()
        carriage_motion = (
            "马车" in prompt_text
            and (bool(re.search(
                r"马车.{0,8}(?:疾驰|奔驰|飞驰|驶入|驶出|行进|前行|移动|冲来|冲出|赶来)",
                prompt_text,
            )) or any(token in prompt_text for token in (
                "马车疾驰", "马车奔驰", "马车飞驰", "马车驶", "马车冲",
                "马车赶", "马车前行", "马车行进", "马车移动", "车轮滚动",
                "赶着马车", "驾驶马车", "驾着马车", "赶车", "策马驾车",
            ))))
        horse_absent = bool(re.search(
            r"(?:马|马匹).{0,4}(?:已经|已|都)?(?:死|逃|跑)", prompt_text,
        )) or any(token in prompt_text for token in (
            "无马", "没有马", "马已死", "马死了", "死马", "马跑了",
            "马匹逃走", "马已逃", "解下马匹", "卸下马匹",
        ))
        if carriage_motion and horse_absent:
            issues.append(_issue(
                shot_id,
                "physics.carriage_power_conflict",
                "prop_physics",
                "本镜同时要求马车移动且声明没有可用马匹；请先明确真实动力来源。",
            ))
        elif carriage_motion:
            object_contract = " ".join(
                str(value or "") for value in physical.get("objects") or [])
            if not all(token in object_contract
                       for token in ("马", "挽具", "车体")):
                issues.append(_issue(
                    shot_id,
                    "physics.carriage_power_chain_missing",
                    "prop_physics",
                    "移动马车缺少马匹、挽具/辕杆与车体的完整动力关系。",
                ))
    return issues


def _reference_rows(shot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("references", "seedance_references", "reference_manifest"):
        value = shot.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
        if isinstance(value, Mapping):
            rows = value.get("items")
            if isinstance(rows, list):
                return [item for item in rows if isinstance(item, Mapping)]
    return []


def _responsibilities(reference: Mapping[str, Any]) -> list[str]:
    value = reference.get(
        "responsibilities",
        reference.get("roles", reference.get("responsibility", reference.get("role"))),
    )
    roles = []
    for item in _as_list(value):
        for part in re.split(r"[,，/;+|]", str(item or "")):
            normalized = part.strip().lower()
            if normalized and normalized not in roles:
                roles.append(normalized)
    return roles


def _normalized_terms(value: Any) -> set[str]:
    return {
        re.sub(r"\s+", "", str(item or "")).lower()
        for item in _as_list(value)
        if str(item or "").strip()
    }


def _reference_conflict_issues(
        shot: Mapping[str, Any], shot_id: str,
) -> list[BlockingIssue]:
    issues = []
    primary_bindings: dict[tuple[str, str], str] = {}
    for index, reference in enumerate(_reference_rows(shot), 1):
        roles = _responsibilities(reference)
        label = str(reference.get("label") or reference.get("name") or index)
        if len(roles) > 1:
            issues.append(_issue(
                shot_id,
                "reference.multiple_responsibilities",
                "reference_budget",
                f"参考图“{label}”承担{len(roles)}项职责；每张参考图只保留一项职责。",
            ))
        included = _normalized_terms(reference.get("include"))
        excluded = _normalized_terms(reference.get("exclude"))
        overlap = sorted(included & excluded)
        if overlap:
            issues.append(_issue(
                shot_id,
                "reference.include_exclude_conflict",
                "reference_budget",
                f"参考图“{label}”同时要求包含和排除：{overlap[0]}。",
            ))
        if len(roles) == 1 and bool(reference.get("primary")):
            target = str(reference.get("target") or reference.get("character") or "")
            binding = (roles[0], target)
            asset = str(reference.get("asset_id") or reference.get("uri") or label)
            previous = primary_bindings.get(binding)
            if previous and previous != asset:
                issues.append(_issue(
                    shot_id,
                    "reference.competing_primary_anchors",
                    "reference_budget",
                    f"职责“{roles[0]}”存在两个主参考；每个目标只保留一个主锚点。",
                ))
            primary_bindings[binding] = asset
    return issues


def _seedance_version(shot: Mapping[str, Any]) -> str:
    model = str(shot.get("model") or shot.get("video_model") or "2.0").lower()
    return "2.5" if "2.5" in model else "2.0"


def _reference_budget_issues(
        shot: Mapping[str, Any], shot_id: str,
) -> list[BlockingIssue]:
    version = _seedance_version(shot)
    references = _reference_rows(shot)
    total = _positive_int(shot.get("total_reference_count"))
    if total is None:
        total = len(references)
    assets = _positive_int(shot.get("asset_reference_count"))
    if assets is None:
        explicit_assets = shot.get("asset_references")
        if isinstance(explicit_assets, (list, tuple)):
            assets = len(explicit_assets)
        else:
            assets = sum(
                1 for item in references
                if str(item.get("role") or item.get("kind") or "").lower()
                not in {"first_frame", "last_frame", "首帧", "尾帧"}
            )
    total_limit = _positive_int(shot.get("max_total_references"))
    if total_limit is None:
        total_limit = SEEDANCE_TOTAL_REFERENCE_LIMITS[version]
    asset_limit = _positive_int(shot.get("max_asset_references"))
    if asset_limit is None:
        asset_limit = SEEDANCE_ASSET_REFERENCE_LIMITS[version]
    issues = []
    if total > total_limit:
        issues.append(_issue(
            shot_id,
            "reference.total_budget_exceeded",
            "reference_budget",
            f"Seedance {version}总参考{total}张，超过上限{total_limit}张。",
        ))
    if assets > asset_limit:
        issues.append(_issue(
            shot_id,
            "reference.asset_budget_exceeded",
            "reference_budget",
            f"Seedance {version}素材参考{assets}张，超过上限{asset_limit}张。",
        ))
    return issues


_MULTI_PHASE = re.compile(
    r"\bthen\b|然后|随后|接着|继而|两阶段|两个阶段|第一阶段|第二阶段|"
    r"先.{0,24}(?:再|然后)",
    re.IGNORECASE,
)


def _is_single_action(shot: Mapping[str, Any]) -> bool:
    if shot.get("single_action") is True:
        return True
    mode = str(shot.get("action_mode") or shot.get("motion_mode") or "").lower()
    return mode in {"single", "single_action", "单动作", "单一动作"}


def _state_count(shot: Mapping[str, Any]) -> int:
    value = shot.get("states", shot.get("state_chain", shot.get("action_states")))
    if isinstance(value, Mapping):
        return sum(1 for item in value.values() if item not in (None, "", [], {}))
    if isinstance(value, (list, tuple)):
        return sum(1 for item in value if item not in (None, "", [], {}))
    if isinstance(value, str):
        return len([part for part in re.split(r"\s*(?:→|->|=>)\s*", value) if part])
    return 0


def _motion_contract_issues(
        shot: Mapping[str, Any], shot_id: str,
) -> list[BlockingIssue]:
    issues = []
    if _is_single_action(shot) and _MULTI_PHASE.search(_prompt_text(shot)):
        issues.append(_issue(
            shot_id,
            "motion.single_action_has_multiple_phases",
            "video_state_chain",
            "单动作镜头含“然后/再/第二阶段”；拆镜或只保留一个动作。",
        ))
    complexity = str(shot.get("action_complexity") or "").lower()
    complex_action = bool(shot.get("complex_action")) or complexity in {
        "complex", "high", "复杂", "multi_stage", "multi-stage",
    }
    if complex_action and _state_count(shot) < 3:
        issues.append(_issue(
            shot_id,
            "motion.complex_action_missing_three_states",
            "video_state_chain",
            "复杂动作至少定义起始、过渡、结束三态。",
        ))
    return issues


def preflight_shot_contract(shot: Any) -> tuple[BlockingIssue, ...]:
    """返回当前镜头的确定性阻断项；输入不完整也不抛异常。"""
    if not isinstance(shot, Mapping):
        return (_issue(
            "unknown", "contract.invalid", "camera_contract",
            "镜头合同必须是对象。",
        ),)
    shot_id = _shot_id(shot)
    issues = []
    issues.extend(_camera_issues(shot, shot_id))
    issues.extend(_people_count_issue(shot, shot_id))
    issues.extend(_era_prop_issues(shot, shot_id))
    issues.extend(_physical_spatial_issues(shot, shot_id))
    issues.extend(_reference_conflict_issues(shot, shot_id))
    issues.extend(_reference_budget_issues(shot, shot_id))
    issues.extend(_motion_contract_issues(shot, shot_id))
    return tuple(issues)


def preflight_episode_contracts(
        shots: Iterable[Any],
) -> dict[str, tuple[BlockingIssue, ...]]:
    """只列出有问题的镜头；一个镜头有误不会取消其他镜头。"""
    output = {}
    for index, shot in enumerate(shots or (), 1):
        issues = preflight_shot_contract(shot)
        if issues:
            shot_id = issues[0].shot_id if issues else str(index)
            if shot_id == "unknown" and isinstance(shot, Mapping):
                shot_id = _shot_id(shot, index)
            output[shot_id] = issues
    return output


@dataclass(frozen=True)
class PostGenerationIssue:
    category: str
    message: str


@dataclass(frozen=True)
class PostGenerationDecision:
    """内容建议与技术重试彻底分路后的决定。"""

    advisory_issues: tuple[PostGenerationIssue, ...]
    technical_errors: tuple[PostGenerationIssue, ...]
    retry_allowed: bool
    retry_reason: str


def _normalize_post_issue(value: Any) -> PostGenerationIssue | None:
    if isinstance(value, PostGenerationIssue):
        return value
    if isinstance(value, Mapping):
        category = str(value.get("category") or "").strip()
        message = str(value.get("message") or value.get("issue") or "").strip()
    else:
        category = ""
        message = str(value or "").strip()
    if not message:
        return None
    if category not in QUALITY_CATEGORIES:
        category = "content_unknown"
    return PostGenerationIssue(category, message)


def evaluate_post_generation(
        issues: Iterable[Any], *, attempts_remaining: int = 0,
) -> PostGenerationDecision:
    """内容问题只提示；仅 provider/encoding 技术错误允许自动重试。"""
    advisory = []
    technical = []
    for raw in issues or ():
        issue = _normalize_post_issue(raw)
        if issue is None:
            continue
        if issue.category in TECHNICAL_CATEGORIES:
            technical.append(issue)
        else:
            advisory.append(issue)
    retry_allowed = bool(technical) and _positive_int(attempts_remaining) not in (None, 0)
    if retry_allowed:
        reason = "存在供应商或编码技术错误，可在剩余次数内重试。"
    elif technical:
        reason = "存在技术错误，但自动重试次数已用完。"
    elif advisory:
        reason = "只有内容建议，不自动重试。"
    else:
        reason = "没有需要重试的问题。"
    return PostGenerationDecision(
        advisory_issues=tuple(advisory),
        technical_errors=tuple(technical),
        retry_allowed=retry_allowed,
        retry_reason=reason,
    )
