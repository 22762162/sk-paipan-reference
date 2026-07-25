"""AIFOS 规则治理：唯一优先级、职责归属与门禁一致性审计。

本模块不再复制具体创作规则。它只回答三个问题：

1. 两条规则冲突时谁优先；
2. 每类事实由哪个文档/模块负责；
3. 标准中心展示的门禁是否与运行时能够执行的门禁一致。
"""

from __future__ import annotations

import copy


RULE_GOVERNANCE_SCHEMA = "aifos.rule-governance/v1"

RULE_PRECEDENCE = [
    {
        "id": "user_locked_fact",
        "label": "用户已锁定事实或逐项人工修订",
        "description": "必须形成新版本；不能静默关闭合规硬门。",
    },
    {
        "id": "episode_fact_bible",
        "label": "本集正式剧本、制作圣经与人物定版",
        "description": "人物、世界、道具、文字白名单和身份事实的唯一来源。",
    },
    {
        "id": "shot_local_contract",
        "label": "当前镜头局部合同",
        "description": "只能从本集事实源派生，且只描述当前镜头。",
    },
    {
        "id": "episode_standard_and_policy",
        "label": "本集制作标准快照与质量/资产策略",
        "description": "续产不自动切换标准；人工单项策略优先于自动建议。",
    },
    {
        "id": "provider_capability",
        "label": "Provider 能力边界",
        "description": "只能验证或阻断，不能静默改人物、参考图、质量或规格。",
    },
    {
        "id": "system_default",
        "label": "系统默认与历史兼容回退",
        "description": "只补缺，绝不能覆盖以上已锁定事实。",
    },
]

RULE_OWNERS = {
    "story_world_and_causality": {
        "owner": "script_development",
        "consumer": "Director._stage_script",
        "stage": "script",
    },
    "visual_identity": {
        "owner": "cast_selection",
        "consumer": "Director._locked_identity",
        "stage": "cast",
    },
    "character_asset_depth": {
        "owner": "character_asset_policy",
        "consumer": "Director.character_asset_policy",
        "stage": "cast",
    },
    "shot_composition_and_timing": {
        "owner": "storyboard",
        "consumer": "enrich_storyboard",
        "stage": "storyboard",
    },
    "spatial_blocking": {
        "owner": "blocking",
        "consumer": "build_spatial_plan",
        "stage": "blocking",
    },
    "visible_text": {
        "owner": "text_assets",
        "consumer": "lock_text_assets",
        "stage": "text_assets",
    },
    "continuity": {
        "owner": "continuity",
        "consumer": "build_continuity_bible",
        "stage": "continuity",
    },
    "image_and_video_quality": {
        "owner": "quality_policy",
        "consumer": "resolve_image_quality/resolve_video_quality",
        "stage": "images/videos",
    },
    "prompt_compilation": {
        "owner": "prompt_contract",
        "consumer": "compile_shot_prompt",
        "stage": "images/frames/videos",
    },
    "provider_routing": {
        "owner": "runtime_config",
        "consumer": "ProductionRouter",
        "stage": "dispatch",
    },
    "delivery": {
        "owner": "delivery",
        "consumer": "write_delivery_verifier",
        "stage": "qc/archive",
    },
}

# ``mandatory`` 表示任何标准版本都不能关闭。创作启发式允许导演覆盖，
# 但身份、人数、文字、物理、连续性、参考图和实际生产规格不能被降级。
MANDATORY_GATE_IDS = {
    "script_bible",
    "character_assets",
    "continuity",
    "spatial",
    "spatial_seedance",
    "five_dimensions",
    "duration",
    "dialogue",
    "people",
    "text",
    "frames",
    "audio",
    "profile",
}

ADVISORY_GATE_IDS = {"performance", "camera"}


def default_rule_governance():
    return {
        "schema": RULE_GOVERNANCE_SCHEMA,
        "precedence": copy.deepcopy(RULE_PRECEDENCE),
        "owners": copy.deepcopy(RULE_OWNERS),
        "single_integrated_qc": True,
        "collect_all_issues_before_repair": True,
        "max_auto_repair_attempts": 1,
        "unchanged_retry_forbidden": True,
        "current_shot_only_repair": True,
        "provider_may_silently_override": False,
        "scopes": [
            {
                "id": "system_permanent",
                "expires": "never",
                "may_contain": [
                    "identity", "gender", "count", "physics",
                    "text_whitelist", "reference_binding",
                    "file_integrity",
                ],
            },
            {
                "id": "project_or_episode",
                "expires": "project_or_episode_end",
                "may_contain": [
                    "era", "style", "wardrobe", "props",
                    "sanctioned_anachronisms",
                ],
            },
            {
                "id": "shot_contract",
                "expires": "shot_replaced_or_version_changed",
                "may_contain": [
                    "camera", "action", "blocking", "start_end_state",
                ],
            },
            {
                "id": "retry_patch",
                "expires": "after_one_retry_or_pass",
                "may_contain": [
                    "targeted_prompt_patch", "reference_adjustment",
                ],
            },
        ],
        "qc_observation_auto_promotes_to_rule": False,
        "pilot": {
            "enabled_by_default": True,
            "duration_seconds": 15,
            "representative_shots": [3, 5],
            "simple_project_may_waive": True,
            "human_approval_required": True,
        },
    }


def gate_defaults(gate_id):
    mandatory = gate_id in MANDATORY_GATE_IDS
    return {
        "mandatory": mandatory,
        "severity": "block" if mandatory else "warning",
        "owner": {
            "script_bible": "script_development",
            "character_assets": "character_asset_policy",
            "continuity": "continuity",
            "spatial": "blocking",
            "spatial_seedance": "blocking",
            "five_dimensions": "storyboard",
            "duration": "storyboard",
            "dialogue": "storyboard",
            "performance": "storyboard",
            "camera": "storyboard",
            "people": "continuity",
            "text": "text_assets",
            "frames": "frames",
            "audio": "delivery",
            "profile": "production_standard",
        }.get(gate_id, "production_standard"),
    }


def audit_rule_configuration(content):
    """返回确定性的规则冲突；结果可直接显示在标准中心和测试中。"""
    issues = []
    rules = content.get("rules") if isinstance(content, dict) else {}
    if not isinstance(rules, dict):
        return [{
            "path": "rules",
            "message": "规则主体必须是对象",
            "severity": "block",
        }]
    governance = rules.get("rule_governance") or {}
    if governance.get("schema") != RULE_GOVERNANCE_SCHEMA:
        issues.append({
            "path": "rules.rule_governance.schema",
            "message": f"规则治理版本必须为 {RULE_GOVERNANCE_SCHEMA}",
            "severity": "block",
        })
    gates = rules.get("quality_gates") or []
    ids = [
        gate.get("id") for gate in gates
        if isinstance(gate, dict) and gate.get("id")
    ]
    if len(ids) != len(set(ids)):
        issues.append({
            "path": "rules.quality_gates",
            "message": "门禁 id 不得重复",
            "severity": "block",
        })
    missing = sorted(MANDATORY_GATE_IDS - set(ids))
    if missing:
        issues.append({
            "path": "rules.quality_gates",
            "message": "缺少不可关闭的运行门禁：" + "、".join(missing),
            "severity": "block",
        })
    for index, gate in enumerate(gates):
        if not isinstance(gate, dict):
            continue
        gate_id = gate.get("id")
        if gate_id in MANDATORY_GATE_IDS:
            if gate.get("enabled") is not True:
                issues.append({
                    "path": f"rules.quality_gates.{index}.enabled",
                    "message": f"{gate_id} 是合规硬门，不能关闭",
                    "severity": "block",
                })
            if gate.get("severity") != "block":
                issues.append({
                    "path": f"rules.quality_gates.{index}.severity",
                    "message": f"{gate_id} 是合规硬门，不能降为警告",
                    "severity": "block",
                })
    production = rules.get("production") or {}
    text_assets = rules.get("text_assets") or {}
    delivery = rules.get("delivery") or {}
    storyboard = rules.get("storyboard") or {}
    if (production.get("text_lock_provider")
            != text_assets.get("keyframe_provider")):
        issues.append({
            "path": "rules.text_assets.keyframe_provider",
            "message": "文字锁定方与生产规则不一致",
            "severity": "block",
        })
    if bool(production.get("burn_subtitles")) == bool(
            delivery.get("no_burned_subtitles")):
        issues.append({
            "path": "rules.delivery.no_burned_subtitles",
            "message": "字幕生产与交付规则互相冲突",
            "severity": "block",
        })
    if bool(storyboard.get("environment_sound_required")) != bool(
            delivery.get("environment_sound_required")):
        issues.append({
            "path": "rules.delivery.environment_sound_required",
            "message": "环境声在分镜与交付阶段口径不一致",
            "severity": "block",
        })
    return issues
