"""AIFOS 规则治理：唯一优先级、职责归属与门禁一致性审计。

本模块不再复制具体创作规则。它只回答三个问题：

1. 两条规则冲突时谁优先；
2. 每类事实由哪个文档/模块负责；
3. 标准中心展示的门禁是否与运行时能够执行的门禁一致。
"""

from __future__ import annotations

import copy
import re


RULE_GOVERNANCE_SCHEMA = "aifos.rule-governance/v1"

RULE_PRECEDENCE = [
    {
        "id": "user_locked_fact",
        "label": "用户已锁定事实或逐项人工修订",
        "description": "必须形成新版本；不能静默关闭合规硬门。",
    },
    {
        "id": "shot_local_contract",
        "label": "当前镜头明确事实与相位合同",
        "description": (
            "只作用于当前镜头；明确的时代、相位、可见状态和空间事实覆盖"
            "本集与本剧的宽泛默认。"),
    },
    {
        "id": "episode_temporary_rules",
        "label": "本集临时规则",
        "description": "只在当前集有效；冲突时覆盖本剧贯穿规则。",
    },
    {
        "id": "episode_fact_bible",
        "label": "本集正式剧本、制作圣经与人物定版",
        "description": "人物、世界、道具、文字白名单和身份事实的本集来源。",
    },
    {
        "id": "project_series_rules",
        "label": "本剧贯穿规则",
        "description": "只在当前作品内跨集继承，绝不传播到其他作品。",
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
        "label": "基础通用规则、系统默认与历史兼容回退",
        "description": "只补缺，绝不能覆盖本剧、本集或当前镜头事实。",
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

PROMPT_ADJUDICATION_SCHEMA = "aifos.prompt-adjudication/v1"

# 审核上下文常见字段 → 唯一优先级六级的归属。审核据此对并列冲突
# 直接取高层级执行,不再把"两条事实同时出现"当成需要猜测的死局。
CONTEXT_FIELD_PRECEDENCE = {
    "user_locked_fact": (
        "identity_references", "reference_manifest", "locked_identity",
        "user_locked_fields", "manual_revision", "feedback",
        "master_state_precedence", "prompt_conflict_resolution"),
    "shot_local_contract": (
        "action", "camera", "location", "start_state", "end_state",
        "composition", "composition_contract", "prompt_contract",
        "shot_contract", "story_phase", "active_realm_id", "era_context",
        "sanctioned_anachronisms", "readable_text", "frame_kind",
        "shot_no", "scene_no", "functional_figures", "character_count",
        "initial_character_state", "variant_axis"),
    "episode_temporary_rules": (
        "episode_rules", "episode_temporary_rules"),
    "episode_fact_bible": (
        "story_world", "story_background", "character_background",
        "characters", "identity_lock", "prop_facts", "style", "era"),
    "project_series_rules": (
        "project_rules", "series_rules", "world_realms",
        "transition_rules", "cross_realm_props"),
    "episode_standard_and_policy": (
        "image_task_class", "image_quality", "aspect",
        "candidate_policy", "source_precedence"),
}


def prompt_adjudication_clause():
    """统一冲突裁决条款:提示词审核遇到并列事实时的执行版优先级。

    《雨夜凶杀》连续熔断的共同病理是"多份事实源各说各话、无人裁决";
    审核最后一次的阻断原因甚至明写「现有事实源直接冲突且无优先级
    条款」。裁决标准其实一直存在(RULE_PRECEDENCE),只是从未随审核
    请求送达——本函数就是把宪法送到法官手里的那份文书。
    """
    levels = "；".join(
        f"{index}.{rule['label']}"
        for index, rule in enumerate(RULE_PRECEDENCE, 1))
    return {
        "schema": PROMPT_ADJUDICATION_SCHEMA,
        "policy": (
            "事实并列冲突时禁止靠猜,但必须先按本条裁决,而不是直接阻断:"
            "(a) 上下文中写明「优先级最高/本条优先」的显式裁决条款"
            "(如 master_state_precedence、prompt_conflict_resolution、"
            "text_policy),是平台对该任务的"
            "既定裁决,直接执行,不算冲突;"
            "(a2) 同一词语的繁体/简体/异体字形(如 縣/县、臺/台)是同一"
            "事实的两种书写,不构成事实冲突,也不算新增或修改文字;执行"
            "字形一律以 must_keep_verbatim 或已定稿母版的字面为准,除非"
            "修改意见明确要求更改字形本身;"
            "(b) 其余并列冲突按唯一优先级取高层级事实执行,低层级只能"
            f"补缺不得覆盖:{levels};"
            "(b2) 同级冲突的两条事实若都来自逐项人工修订/质检反馈"
            "(带「第N轮修订」标记),按轮次取最后一条执行,前面的轮次"
            "视为已被本人替换、不再生效,也不构成冲突——人工在后一轮"
            "改口是修订的正常形态,不是自相矛盾;"
            "(c) 只有同一层级内两条事实互斥、且没有任何显式裁决条款、"
            "且不属于 (b2) 的可排序修订时,才允许 approved=false,并在"
            "阻断原因里写明是哪两条同级事实、各自出处——不得再以"
            "「无优先级条款」为由阻断。"),
        "field_precedence": {
            level: list(fields)
            for level, fields in CONTEXT_FIELD_PRECEDENCE.items()},
    }


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
                "id": "project_series",
                "expires": "project_end",
                "may_contain": [
                    "era", "style", "wardrobe", "props",
                    "sanctioned_anachronisms",
                ],
            },
            {
                "id": "episode_temporary",
                "expires": "episode_end",
                "may_contain": [
                    "era", "style", "wardrobe", "props",
                    "sanctioned_anachronisms", "story_phase",
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


# 逐项人工修订/质检反馈是「同级可排序事实」：后一轮改口是修订的正常
# 形态，不是自相矛盾。裁决条款 (b2) 靠这个标记来判定谁在后。
REVISION_ROUND_PREFIX = "【第{round}轮修订·后条覆盖前条】"
REVISION_FEEDBACK_BUDGET = 2400
_REVISION_ROUND_RE = re.compile(r"【第(\d+)轮修订")


def next_revision_round(*texts):
    """下一轮轮次 = 已出现过的最大轮次 + 1。

    轮次必须跨「人工重画」与「质检自动重画」两条路径全局单调,否则
    (b2)「取轮次最大的一条」会把最新的意见判成已失效。质检重试在单次
    调用内被 ``_qc_retries()`` 钳为 1 轮,真正的多轮来自跨次人工重画,
    两边共用同一个计数来源:已有文本里的标记本身。
    """
    highest = 0
    for text in texts:
        for match in _REVISION_ROUND_RE.finditer(str(text or "")):
            try:
                highest = max(highest, int(match.group(1)))
            except ValueError:
                continue
    return highest + 1


def stack_revision_feedback(previous, patch, round_no,
                            budget=REVISION_FEEDBACK_BUDGET):
    """把新一轮修订叠到既有反馈上，并保证最新一轮永远不被截断。

    旧实现是 ``f"{old}\\n{patch}"[:2400]``——纯累加，且截断保留的是**最旧**
    的那几轮、丢掉最新一轮，与优先级正好相反。累积几轮后两条人工修订
    互斥，按治理条款 (c) 同级互斥只能熔断，单镜就永久卡死。

    现在每轮带上轮次标记，超预算时从**最旧**的一端丢起，新一轮一定完整
    保留；下游按条款 (b2) 取轮次最大的一条执行。
    """
    patch = str(patch or "").strip()
    if not patch:
        return str(previous or "").strip()
    head = REVISION_ROUND_PREFIX.format(round=max(1, int(round_no or 1)))
    latest = f"{head}{patch}"
    blocks = [
        block for block in str(previous or "").split("\n") if block.strip()]
    blocks.append(latest)
    # 最新一轮独占预算也要保住；不够就只留它。
    while len(blocks) > 1 and len("\n".join(blocks)) > budget:
        blocks.pop(0)
    if len(blocks) == 1 and len(latest) > budget:
        # 单轮就超预算时截正文，但轮次标记必须留住——否则条款 (b2)
        # 无从判断谁在后。
        return head + patch[:max(0, budget - len(head))]
    return "\n".join(blocks)
