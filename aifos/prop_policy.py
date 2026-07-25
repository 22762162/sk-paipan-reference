"""重要道具分级、母资产计划与镜头级前置检查。

道具不是“画面里顺手出现的东西”。凡是会承载剧情信息、被角色操作，或
在多个镜头间保持状态的物件，都必须在进入生图/视频前有可追踪的合同。
本模块只做确定性的归一化和门禁，不调用模型，便于 API、检查板和重生成
共用同一套规则。
"""

from __future__ import annotations

import copy
import re


PROP_POLICY_SCHEMA = "aifos.prop-policy/v1"
PROP_CONTRACT_SCHEMA = "aifos.prop-contract/v1"

TEXT_CARRIERS = (
    "屏幕", "电脑", "笔记本", "手机", "合同", "文件", "书", "书页",
    "门牌", "招牌", "榜单", "票据", "欠条", "公告", "海报", "直播屏",
    "聊天框", "弹幕", "字幕",
)
INTERACTION_HINTS = (
    "拿", "握", "持", "抓", "递", "接", "打开", "关闭", "翻", "阅读",
    "查看", "点击", "输入", "敲", "推", "拉", "佩戴", "戴着", "放下",
    "掉落", "摔", "砸", "拔", "插", "点燃", "倒入", "饮用", "使用",
)
CLOSEUP_HINTS = ("特写", "大特写", "近景", "局部", "细节", "屏幕", "文字")
PLOT_HINTS = ("关键", "线索", "证据", "信物", "秘密", "身份", "遗物", "道具", "机关", "契约")
GENERIC_HINTS = ("桌", "椅", "杯", "碗", "灯", "窗", "墙", "地面", "背景", "家具")


def _text(value):
    if value is None:
        return ""
    return str(value).strip()


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return _text(value).lower() in {"1", "true", "yes", "y", "是", "需要", "必需"}


def _as_int(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _iter_entries(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _entry_text(entry):
    if isinstance(entry, dict):
        return " ".join(
            _text(entry.get(key)) for key in (
                "name", "label", "description", "action", "state", "text",
                "role", "purpose", "importance", "type",
            ) if _text(entry.get(key)))
    return _text(entry)


def _normalize_entry(entry, source=""):
    if isinstance(entry, dict):
        data = copy.deepcopy(entry)
        name = _text(data.get("name") or data.get("label") or data.get("id"))
    else:
        data = {}
        name = _text(entry)
    if not name:
        return None
    raw = _entry_text({**data, "name": name})
    signal_text = " ".join(
        _text(data.get(key)) for key in (
            "name", "label", "description", "action", "purpose", "type",
            "text", "visible_text",
        ) if _text(data.get(key)))
    text_required = _as_bool(
        data.get("text_required") or data.get("readable_text")
        or data.get("requires_readable_text")) or any(
            token in signal_text for token in TEXT_CARRIERS)
    interaction = _as_bool(
        data.get("interaction") or data.get("interactive")
        or data.get("physical_interaction")) or any(
            token in signal_text for token in INTERACTION_HINTS)
    closeup = _as_bool(data.get("closeup") or data.get("close_up")) or any(
        token in signal_text for token in CLOSEUP_HINTS)
    plot_critical = _as_bool(
        data.get("plot_critical") or data.get("key_prop")
        or data.get("story_critical")) or any(token in raw for token in PLOT_HINTS)
    explicit_tier = _text(data.get("tier") or data.get("importance")).upper()
    if explicit_tier in {"A", "B", "C"}:
        tier = explicit_tier
    elif text_required or plot_critical or interaction or closeup:
        tier = "A"
    elif _as_bool(data.get("signature") or data.get("recurring")
                  or data.get("repeated")):
        tier = "B"
    else:
        tier = "C"
    return {
        "name": name,
        "description": _text(data.get("description") or data.get("purpose")),
        "state": _text(data.get("state") or data.get("initial_state")),
        "owner": _text(data.get("owner") or data.get("holder")),
        "position": _text(data.get("position") or data.get("location")),
        "action": _text(data.get("action") or data.get("interaction_action")),
        "text": _text(data.get("text") or data.get("visible_text")),
        "text_required": text_required,
        "interaction": interaction,
        "closeup": closeup,
        "plot_critical": plot_critical,
        "signature": _as_bool(data.get("signature")),
        "recurring": _as_bool(data.get("recurring") or data.get("repeated")),
        "explicit_tier": explicit_tier if explicit_tier in {"A", "B", "C"} else "",
        "tier": tier,
        "source": source,
        "asset_outputs": list(data.get("asset_outputs") or []),
        "references": list(data.get("references") or []),
        "reuse_count": _as_int(data.get("reuse_count") or data.get("repeated_shots")),
    }


def _collect_entries(script=None, storyboard=None):
    script = script if isinstance(script, dict) else {}
    storyboard = storyboard if isinstance(storyboard, dict) else {}
    entries = []
    for key in ("props", "key_props", "important_props", "important_objects"):
        entries.extend((value, f"script.{key}") for value in _iter_entries(script.get(key)))
    for index, scene in enumerate(script.get("scenes") or [], 1):
        if not isinstance(scene, dict):
            continue
        for key in ("props", "key_props", "important_props", "important_objects"):
            entries.extend((value, f"scene[{index}].{key}")
                           for value in _iter_entries(scene.get(key)))
    for index, character in enumerate(script.get("characters") or [], 1):
        if not isinstance(character, dict):
            continue
        entries.extend((value, f"character[{index}].signature_props")
                       for value in _iter_entries(character.get("signature_props")))
    for index, shot in enumerate(storyboard.get("shots") or [], 1):
        if not isinstance(shot, dict):
            continue
        for key in ("props", "key_props", "important_props", "objects"):
            entries.extend((value, f"shot[{index}].{key}")
                           for value in _iter_entries(shot.get(key)))
        contract = shot.get("prop_contract")
        if isinstance(contract, dict):
            entries.extend((value, f"shot[{index}].prop_contract.items")
                           for value in _iter_entries(contract.get("items")))
    return entries


def default_prop_policy():
    return {
        "schema": PROP_POLICY_SCHEMA,
        "tier_a": {
            "label": "必须独立母资产",
            "reasons": ["可读文字", "剧情关键", "重复出现至少2镜", "特写/近景", "角色物理操作", "复杂开合/状态变化"],
            "repeated_min": 2,
        },
        "tier_b": {
            "label": "建议独立母资产",
            "reasons": ["签名道具", "反复出现", "需要稳定材质/颜色/轮廓"],
        },
        "tier_c": {
            "label": "不单独生图",
            "reasons": ["通用家具", "背景小物", "不承载剧情且不被操作"],
        },
        "master_asset_outputs": ["master", "detail_or_state"],
        "text_asset_outputs": ["flat_text_layout", "context_keyframe"],
        "pure_background": "no_text_no_scene_no_characters",
        "no_asset_for_generic": True,
        "qc": [
            "轮廓/材质/颜色", "文字逐字白名单", "开合与状态", "手部接触/遮挡",
            "透视与空间位置", "首尾帧及相邻镜头状态连续",
        ],
    }


def build_prop_asset_plan(script=None, storyboard=None, rules=None):
    """建立全剧道具母资产清单，并把重复/交互/文字需求前置为可检查字段。"""
    config = default_prop_policy()
    if isinstance(rules, dict):
        for key, value in rules.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(copy.deepcopy(value))
            elif key in config:
                config[key] = copy.deepcopy(value)
    items = {}
    source_entries = _collect_entries(script, storyboard)
    for raw, source in source_entries:
        item = _normalize_entry(raw, source)
        if not item:
            continue
        key = item["name"]
        existing = items.get(key)
        if existing is None:
            items[key] = item
        else:
            for field in ("description", "state", "owner", "position", "action", "text"):
                if not existing.get(field) and item.get(field):
                    existing[field] = item[field]
            for field in ("text_required", "interaction", "closeup", "plot_critical", "signature", "recurring"):
                existing[field] = bool(existing.get(field) or item.get(field))
            existing["reuse_count"] = max(existing.get("reuse_count", 0), item.get("reuse_count", 0))
            existing["references"] = list(dict.fromkeys(existing.get("references", []) + item.get("references", [])))
            existing["source"] = f"{existing.get('source')}; {source}"
    shots = storyboard.get("shots", []) if isinstance(storyboard, dict) else []
    for item in items.values():
        pattern = re.escape(item["name"])
        occurrences = 0
        shot_refs = []
        for shot in shots:
            if not isinstance(shot, dict):
                continue
            blob = _entry_text({
                "name": shot.get("description"),
                "props": shot.get("props"),
                "contract": shot.get("prop_contract"),
            })
            if re.search(pattern, blob, flags=re.IGNORECASE):
                occurrences += 1
                if shot.get("shot_no") is not None:
                    shot_refs.append(shot.get("shot_no"))
        item["reuse_count"] = max(item.get("reuse_count", 0), occurrences)
        item["shot_refs"] = shot_refs
        repeated_min = _as_int((config.get("tier_a") or {}).get("repeated_min"), 2)
        if item["reuse_count"] >= repeated_min:
            item["tier"] = "A"
        item["requires_separate_asset"] = item["tier"] in {"A", "B"}
        if item["tier"] == "A":
            item["asset_outputs"] = list(item.get("asset_outputs") or config["master_asset_outputs"])
            if item["text_required"]:
                item["asset_outputs"] = list(dict.fromkeys(
                    item["asset_outputs"] + config["text_asset_outputs"]))
        elif item["tier"] == "B":
            item["asset_outputs"] = list(item.get("asset_outputs") or ["master"])
        else:
            item["asset_outputs"] = []
        item["asset_status"] = "required" if item["requires_separate_asset"] else "inline_only"
    ordered = sorted(items.values(), key=lambda value: (value["tier"], value["name"]))
    required_assets = [item for item in ordered if item["requires_separate_asset"]]
    deferred = [item for item in ordered if not item["requires_separate_asset"]]
    return {
        "schema": PROP_POLICY_SCHEMA,
        "policy": config,
        "items": ordered,
        "required_assets": required_assets,
        "deferred_items": deferred,
        "summary": {
            "total": len(ordered),
            "tier_a": sum(item["tier"] == "A" for item in ordered),
            "tier_b": sum(item["tier"] == "B" for item in ordered),
            "tier_c": sum(item["tier"] == "C" for item in ordered),
        },
    }


def _shot_prop_entries(shot):
    if not isinstance(shot, dict):
        return []
    entries = []
    for key in ("props", "key_props", "important_props", "objects"):
        entries.extend(_iter_entries(shot.get(key)))
    contract = shot.get("prop_contract")
    if isinstance(contract, dict):
        entries.extend(_iter_entries(contract.get("items")))
    return entries


def _real_text_asset(readable):
    if not isinstance(readable, dict) or not readable.get("required"):
        return False
    carrier = _text(readable.get("carrier"))
    if any(token in carrier for token in ("字幕", "对白字幕", "旁白字幕")):
        return False
    return (bool([item for item in readable.get("whitelist") or [] if _text(item)])
            and any(token in carrier for token in TEXT_CARRIERS))


def validate_prop_contract(shot, plan=None):
    """检查镜头是否把当前道具的状态、位置、动作和文字约束写清楚。"""
    entries = []
    for raw in _shot_prop_entries(shot):
        item = _normalize_entry(raw, "shot")
        if item:
            entries.append(item)
    readable = shot.get("readable_text") or {}
    carrier = _text(readable.get("carrier"))
    # “无字幕”是负面约束，不是需要单独生成的文字道具。
    if (_real_text_asset(readable) and not entries
            and not any(token in carrier for token in ("字幕", "对白字幕", "旁白字幕"))):
        entries.append(_normalize_entry({
            "name": readable.get("carrier") or "文字载体",
            "text_required": True,
            "text": "、".join(readable.get("whitelist") or []),
        }, "readable_text"))
    if not entries:
        return []
    plan_by_name = {item.get("name"): item for item in (plan or {}).get("items", [])}
    issues = []
    contract = shot.get("prop_contract") or {}
    has_contract_items = isinstance(contract, dict) and bool(contract.get("items"))
    for item in entries:
        name = item["name"]
        planned = plan_by_name.get(name, {})
        text_required = bool(item["text_required"] or planned.get("text_required"))
        interaction = bool(item["interaction"] or planned.get("interaction"))
        if text_required and not ((shot.get("readable_text") or {}).get("whitelist") or item.get("text")):
            issues.append({"shot_no": shot.get("shot_no"), "prop": name, "code": "text_whitelist_missing", "message": "文字道具必须先提供逐字白名单"})
        if (text_required or interaction or planned.get("tier") == "A") and not has_contract_items:
            issues.append({"shot_no": shot.get("shot_no"), "prop": name, "code": "prop_contract_missing", "message": "重要/交互道具必须有镜头道具合同"})
        if interaction and not (item.get("state") or item.get("action") or item.get("owner")):
            issues.append({"shot_no": shot.get("shot_no"), "prop": name, "code": "interaction_state_missing", "message": "被操作道具必须写明持有者、动作或起始状态"})
        if interaction and not (item.get("position") or (shot.get("spatial_logic") or shot.get("physical_logic"))):
            issues.append({"shot_no": shot.get("shot_no"), "prop": name, "code": "interaction_position_missing", "message": "被操作道具必须写明空间位置/接触关系"})
        if planned.get("requires_separate_asset") and not (item.get("references") or shot.get("prop_references")):
            issues.append({"shot_no": shot.get("shot_no"), "prop": name, "code": "asset_reference_missing", "message": "A/B级道具必须绑定已生成的母资产参考图"})
    return issues


def build_prop_contract(shot):
    """将镜头道具压缩为只描述本镜实际出现对象的合同。"""
    items = []
    for raw in _shot_prop_entries(shot):
        item = _normalize_entry(raw, "shot")
        if item:
            items.append({
                "name": item["name"],
                "state": item["state"] or "保持当前锁定状态",
                "owner": item["owner"] or "无指定持有者",
                "position": item["position"] or "按空间调度图固定位置",
                "action": item["action"] or "不自行改变状态",
                "text": item["text"],
                "text_required": item["text_required"],
                "tier": item["tier"],
                "references": item["references"],
            })
    readable = shot.get("readable_text") or {}
    if _real_text_asset(readable) and not any(item["text_required"] for item in items):
        items.append({
            "name": readable.get("carrier") or "文字载体",
            "state": "打开并朝向镜头，保持可读",
            "owner": "按镜头合同",
            "position": "按构图固定",
            "action": "只保持画面，不重写文字",
            "text": "、".join(sanitize for sanitize in (readable.get("whitelist") or []) if str(sanitize).strip()),
            "text_required": True,
            "tier": "A",
            "references": [],
        })
    return {
        "schema": PROP_CONTRACT_SCHEMA,
        "required": bool(items),
        "items": items,
        "rules": [
            "只出现本镜明确列出的道具，不新增、不复制、不把参考图道具带入其他镜头",
            "道具状态、持有者、位置、接触关系与首尾帧一致",
            "文字只允许逐字白名单，禁止乱码、随机UI、字幕、Logo和水印",
        ],
    }
