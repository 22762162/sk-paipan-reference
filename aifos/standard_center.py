"""可版本化的 AI 漫剧制作标准中心。

制作标准采用“不可变版本 + 可变激活指针”的模型。每次保存都会产生一个
新版本，历史版本由 SQLite 触发器保护；生产任务可记录 version_id 与
fingerprint，从而在标准调整后仍能还原当时的生产口径。
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import time

from .rule_governance import (
    ADVISORY_GATE_IDS,
    MANDATORY_GATE_IDS,
    audit_rule_configuration,
    default_rule_governance,
    gate_defaults,
)

DEFAULT_PROFILE_KEY = "sk-manju-v5"
STANDARD_BUNDLE_SCHEMA = "aifos.production-standard/v1"


MODEL_UPGRADE_POLICY = {
    "schema": "aifos.video-model-upgrade/v1",
    "enabled": True,
    "scope": "per_shot",
    "default_model": "seedance2.0fast_vip",
    "candidate_capability_key": "seedance2_5",
    "candidate_display_name": "Seedance 2.5",
    "normal_profile": {
        "preferred_segment_seconds": [5, 8],
        "max_segment_seconds": 15,
    },
    "upgrade_duration_range_seconds": [16, 30],
    "reported_limits": {
        "max_material_assets": 40,
        "max_total_references": 50,
        "max_duration_seconds": 30,
    },
    "allowed_reasons": [
        "indivisible_continuous_take_16_to_30_seconds",
        "required_references_exceed_seedance2_0_limit",
        "reference_video_required",
        "complex_continuous_action",
    ],
    "required_shot_fields": [
        "video_model",
        "video_model_reason",
        "runtime_capability_verified",
    ],
    "runtime_capability_required": True,
    "unverified_runtime_action": "BLOCK",
    "fallback": "split_or_block",
    "keep_resolution": "720p",
    "preserve_voice": "jimeng_builtin",
    "preserve_lip_sync": True,
    "silent_truncation_forbidden": True,
}


DEFAULT_STANDARD = {
    "profile_key": DEFAULT_PROFILE_KEY,
    "name": "SK AI 漫剧工业制作标准 V5",
    "description": "五维分镜驱动的高密度、强连续性、无字幕精品漫剧生产标准。",
    "source_skill": {
        "id": "sk-manju-storyboard-skill",
        "name": "SK 漫剧五维分镜制作 Skill",
        "version": "5.4",
        "reference": "five-dimension-storyboard-template-v5.txt",
        "principle": (
            "先把小说或梗概改编成因果、物理、时间、空间、人物信息与道具"
            "生命周期完整的可拍剧本，再推导人物视觉 DNA、完成全剧角色去重"
            "和人工定版，最后建立母资产、五维分镜与 Seedance 生产。"),
    },
    "rules": {
        "rule_governance": default_rule_governance(),
        "production": {
            "pipeline_version": "sk-manju-v5",
            "video_model": "seedance2.0fast_vip",
            "resolution": "720p",
            "voice": "jimeng_builtin",
            "lip_sync": True,
            "burn_subtitles": False,
            "text_lock_provider": "ChatGPT关键帧",
            "preferred_segment_seconds": [5, 8],
            "max_segment_seconds": 15,
            "time_precision_seconds": 0.5,
            "prompt_strategy": "five_dimensions_per_segment",
            "model_upgrade_policy": copy.deepcopy(MODEL_UPGRADE_POLICY),
            "prompt_contract": {
                "schema": "aifos.shot-prompt/v2.2",
                "authority": "highest_runtime_rule",
                "conflict_policy": "block_and_return_upstream_never_guess",
                "review_page_schema": "aifos.prompt-review/v1",
                "per_shot_review_variants": [
                    "keyframe", "first_frame", "last_frame", "video"],
                "explicit_identity_fields_required": [
                    "name", "gender", "age_range"],
                "order": [
                    "subject", "scene", "frame_target", "start",
                    "character_conditions", "prop_registry", "frame_props",
                    "single_action", "performance", "camera",
                    "prop_transitions", "end",
                    "dialogue", "text", "references", "hard_constraints",
                ],
                "single_primary_action": True,
                "single_camera_move": True,
                "static_frame_target_required": True,
                "validation_statuses": ["PASS", "WARN", "BLOCK"],
                "headwear_fields": [
                    "presence", "kind", "name", "hair_visibility"],
                "character_condition_dimensions": [
                    "life_state", "consciousness_state",
                    "embodiment", "mobility"],
                "prop_contract_schema": "aifos.prop-contract/v2.2",
                "compact_prompt_sent_to_model": True,
                "full_prompt_kept_for_audit": True,
                "pre_generation_review_required": True,
                "pre_generation_review_provider": "codex",
                "pre_generation_review_schema":
                    "aifos.codex-prompt-review/v1",
                "pre_generation_review_fail_closed": True,
                "optimized_prompt_sent_to_model": True,
                "reference_roles": [
                    "identity", "wardrobe", "prop", "scene", "composition",
                    "spatial_blocking", "continuity", "style",
                    "inner_persona",
                ],
            },
            "fast_vip_real_face_conflict": "pause_for_confirmation",
        },
        "script_development": {
            "required_before_any_visual_asset": True,
            "source_material_is_adaptable": True,
            "auto_adapt_imported_source": True,
            "writer_completes_missing_details": True,
            "single_integrated_review": True,
            "scene_boundary_contract_required": True,
            "local_rewrite_enabled": True,
            "impact_analysis_before_rewrite": True,
            "preserve_unaffected_assets": True,
            "human_approval_if_scope_expands": True,
            "local_rewrite_default_scope": "current_scene",
            "required_review_dimensions": [
                "causal_chain",
                "character_motivation",
                "information_state",
                "physical_reality",
                "spatial_continuity",
                "temporal_continuity",
                "prop_lifecycle",
                "world_rules",
                "shootability",
                "story_density",
            ],
            "scene_boundary_fields": [
                "entry_boundary",
                "exit_boundary",
                "immutable_facts",
                "prop_ledger",
                "knowledge_state",
                "time_state",
                "local_rewrite_scope",
            ],
        },
        "story_analysis": {
            "required_before_images": True,
            "auto_analyze_uploaded_script": True,
            "user_style_is_hard_constraint": True,
            "distinguish_world_from_render_medium": True,
            "editable_before_lock": True,
            "resolve_character_entities_before_images": True,
            "performance_cues_are_not_characters": True,
            "final_character_image_prompt_required": True,
            "compact_prompt_compilation": True,
            "required_sections": [
                "narrative", "world", "visual", "scenes", "characters",
                "prompt_bible",
            ],
            "downstream_consumers": [
                "character", "scene", "storyboard", "keyframe", "seedance",
            ],
            "visible_text_policy": "关键帧先锁字，视频模型只保持不重写",
            "default_visual_fallback": "剧情自适应、电影级半写实精品漫剧",
        },
        "text_assets": {
            "explicit_whitelist_only": True,
            "required_fields": [
                "carrier", "whitelist", "layout", "style",
                "perspective", "priority",
            ],
            "style_description_required": True,
            "keyframe_provider": "ChatGPT关键帧",
            "video_policy": "Seedance只保持首帧文字，不从零生成或改写",
            "dense_text_policy": "密集页面单独生成文字/UI资产后合成，不依赖视频模型重写",
            "forbid_prompt_metadata_as_text": True,
            "forbidden_system_fields": [
                "镜头合同", "主体", "场景", "起点", "终点", "单一主动作",
                "TASK", "WORLD / STYLE", "质检原因", "自动优化修订",
                "参考图职责", "硬约束",
            ],
            "no_readable_text_without_whitelist": True,
            "qc_requirements": [
                "逐字核对白名单",
                "核对载体、版式、字体风格、颜色和透视",
                "禁止乱码、字幕、Logo、水印和未授权系统字段",
            ],
        },
        "character_assets": {
            "background": "pure_background_no_text_no_scene",
            "reference_identity_priority": ["face", "hair"],
            "workwear_required_for_occupational_roles": True,
            "visual_dna_required": True,
            "visual_dna_sequence": [
                "story_evidence", "experience_and_situation",
                "personality_and_behavior", "visible_traits",
                "cast_dedup", "three_view_assets",
            ],
            "forbid_template_labels": True,
            "cast_dedup_required": True,
            "cast_dedup_dimensions": [
                "hair_silhouette", "clothing_structure",
                "body_or_occupation_marks", "story_visual_symbol",
                "signature_accessory", "temperament_keywords",
            ],
            "cast_dedup_overlap_threshold": 2,
            "character_asset_depth_source": "episode_character_asset_policy",
            "character_asset_default_mode": "auto",
            "canonical_views": [
                "face_closeup", "front", "profile", "back",
            ],
            "turnaround_review_board_aspect": "16:9",
            "turnaround_review_board_only": True,
            "canonical_views_are_separate_assets": True,
            "identity_and_wardrobe_separated": True,
            "clean_skin_default": True,
            "seedance_may_redesign_character": False,
            "candidate_targets": {
                "main": 4,
                "important_supporting": 4,
                "non_main": 4,
                "non_main_max": 4,
                "background": 0,
            },
            "core_prop_candidate_target": 4,
            "incidental_props_use_continuity_ledger_only": True,
            "initial_portrait_quality_gate": False,
        },
        "inner_persona": {
            "schema": "aifos.inner-persona/v1",
            "mode": "chibi_overlay",
            "physical_presence": False,
            "counts_as_real_character": False,
            "included_in_spatial_blocking": False,
            "visible_to": "host_only",
            "historical_characters_may_react": False,
            "real_form_forbidden_after_transition": True,
            "wardrobe_source": "locked_current_look",
            "inherit_signature_props": False,
            "allowed_functions": [
                "inner_monologue", "inner_commentary",
                "comic_reaction", "decision_conflict",
            ],
            "auto_after_every_dialogue": False,
            "expression_style": "exaggerated",
            "proportion_style": "oversized_head_tiny_body",
            "total_height_in_heads": 1.8,
            "head_height_ratio": 0.58,
            "body_smaller_than_head": True,
            "max_overlays_per_shot": 1,
            "host_mouth_closed_for_inner_voice": True,
            "forbid_burned_subtitles": True,
        },
        "dialogue": {
            "preserve_verbatim": True,
            "max_chars_per_shot": 25,
            "split_at_natural_pause": True,
            "duration_formula": "字数÷语速+情绪缓冲",
            "forbidden_placeholders": ["(略)", "见对话"],
            "speech_profiles": {
                "tense_angry": {
                    "label": "紧张/愤怒/争抢",
                    "chars_per_second": [6, 8],
                    "buffer_seconds": [0.3, 0.5],
                },
                "daily": {
                    "label": "日常/叙述/交代",
                    "chars_per_second": [4, 5],
                    "buffer_seconds": [0.5, 0.8],
                },
                "sad_gentle": {
                    "label": "悲伤/温柔/独白/告白",
                    "chars_per_second": [2, 3],
                    "buffer_seconds": [0.8, 1.2],
                },
                "trembling": {
                    "label": "哽咽/颤抖/濒临崩溃",
                    "chars_per_second": [1, 2],
                    "buffer_seconds": [1.0, 1.5],
                },
            },
        },
        "performance": {
            "reaction_after_key_dialogue": True,
            "listener_duration_ratio": 0.6666666667,
            "reaction_seconds": [1.5, 3],
            "beat_at_emotional_peak": True,
            "beat_seconds": [2, 4],
            "beat_requires_visible_acting": True,
            "physical_action_separate_shot": True,
            "performance_goal_required": True,
        },
        "storyboard": {
            "five_dimensions": [
                {"id": "subject_motion", "label": "主体运动", "description": "动作、表演与微表情"},
                {"id": "environment_light", "label": "环境与光线", "description": "空间、光源与氛围变化"},
                {"id": "camera_design", "label": "摄影机设计", "description": "景别、机位、焦段、运镜与构图"},
                {"id": "time_state", "label": "时间与状态", "description": "时间码、起止状态和连续性"},
                {"id": "aesthetic", "label": "审美控制", "description": "色彩、质感与视觉风格"},
            ],
            "required_columns": [
                "时间码", "景别", "角度", "焦段", "机位", "运镜", "构图",
                "拍摄速度", "站位", "视线", "画面内容描述", "台词", "表演重点",
                "微表情", "音效", "视觉钩子", "镜头功能",
            ],
            "scene_type_words": [
                "对峙冲突", "动作打斗", "独白抒情", "追逐紧张", "暧昧亲密",
                "群戏调度", "闪回回忆", "大场面定场",
            ],
            "shot_functions": [
                "铺垫", "蓄势", "爆发", "收束", "过渡", "信息交代", "反应", "留白",
            ],
            "minimum_vertical_angles_per_segment": 2,
            "adjacent_shot_scale_jump_levels": 2,
            "adjacent_camera_axis_change_degrees": 30,
            "forbid_repeated_scale_and_angle": True,
            "environment_sound_required": True,
            "visual_hook_required": True,
            "eye_line_required": True,
            "spatial_blocking_required_for_group": 3,
        },
        "continuity": {
            "state_labels": ["姿态", "伤势", "持有道具", "情绪", "朝向关系"],
            "end_state_to_next_start": True,
            "character_count_lock": True,
            "on_stage_characters_only": True,
            "canonical_entity_names": True,
            "costume_and_prop_lock": True,
            "scene_change_starts_new_segment": True,
            "position_uses_frame_geometry": True,
        },
        "camera_library": {
            "shot_scales": ["大特写", "特写", "近景", "中近景", "中景", "全景", "大全景"],
            "angles": ["平视", "俯拍", "仰拍", "顶拍", "低机位", "荷兰角", "过肩"],
            "focal_lengths_mm": [8, 16, 35, 50, 85, 100, 135, 200],
            "positions": ["正面", "侧面", "背面", "过肩", "主观", "低机位", "高机位"],
            "movements": ["固定", "推", "拉", "摇", "移", "跟", "环绕", "手持", "升降"],
            "compositions": ["黄金分割", "中心", "对称", "框架式", "引导线", "对角线", "荷兰角", "前景", "留白"],
            "speeds": ["正常", "升格", "降格", "延时", "定格"],
        },
        "quality_gates": [
            {"id": "script_bible", "label": "剧本第一道总闸门", "enabled": True, "severity": "block", "mandatory": True, "owner": "script_development", "description": "小说/梗概已完成影视化改编；世界、人物、因果、信息、物理、时间、空间、道具生命周期、可拍性及局部返编边界完整且不冲突。"},
            {"id": "character_assets", "label": "人物与核心道具母资产", "enabled": True, "severity": "block", "mandatory": True, "owner": "character_asset_policy", "description": "所有正式角色与核心道具都已从4张候选中人工锁定；简化模式只豁免人物四视图与细节资产。"},
            {"id": "continuity", "label": "连续性", "enabled": True, "severity": "block", "mandatory": True, "owner": "continuity", "description": "人物、服装、道具、站位及段间状态无跳变。"},
            {"id": "spatial", "label": "空间调度", "enabled": True, "severity": "block", "mandatory": True, "owner": "blocking", "description": "逐场锁定人物走位、机位、视锥和屏幕轴线，防止多人漂移或增殖。"},
            {"id": "spatial_seedance", "label": "Seedance 空间参考图", "enabled": True, "severity": "block", "mandatory": True, "owner": "blocking", "description": "多人走位或变机位镜头必须生成并绑定空间示意图。"},
            {"id": "five_dimensions", "label": "五维分镜", "enabled": True, "severity": "block", "mandatory": True, "owner": "storyboard", "description": "每镜包含主体、环境、摄影机、时间状态和审美信息。"},
            {"id": "duration", "label": "时长与时间码", "enabled": True, "severity": "block", "mandatory": True, "owner": "storyboard", "description": "分段时长及 0.5 秒精度符合生产规格。"},
            {"id": "dialogue", "label": "台词与语速", "enabled": True, "severity": "block", "mandatory": True, "owner": "storyboard", "description": "台词逐字一致、自然拆分，并按情绪计算语速。"},
            {"id": "performance", "label": "表演空间", "enabled": True, "severity": "warning", "mandatory": False, "owner": "storyboard", "description": "关键台词后的反应镜和高潮留白属于导演建议；有意长镜头可记录覆盖理由。"},
            {"id": "camera", "label": "镜头语言", "enabled": True, "severity": "warning", "mandatory": False, "owner": "storyboard", "description": "景别跳级和机位变化属于导演建议，不得为了机械达标破坏有意的长镜头或正反打。"},
            {"id": "people", "label": "人物数量", "enabled": True, "severity": "block", "mandatory": True, "owner": "continuity", "description": "镜头人物与在场角色清单及数量锁一致。"},
            {"id": "text", "label": "画面文字", "enabled": True, "severity": "block", "mandatory": True, "owner": "text_assets", "description": "可读文字先由关键帧锁字，视频模型不得重写。"},
            {"id": "frames", "label": "首尾帧", "enabled": True, "severity": "block", "mandatory": True, "owner": "frames", "description": "首尾帧已通过视觉质检，并与前后镜头状态无缝衔接。"},
            {"id": "audio", "label": "声音设计", "enabled": True, "severity": "block", "mandatory": True, "owner": "delivery", "description": "环境声不为空，Seedance2 随视频人声与口型同步符合规格。"},
            {"id": "profile", "label": "生产规格", "enabled": True, "severity": "block", "mandatory": True, "owner": "production_standard", "description": "实际 Provider、模型、质量、配音、口型与无字幕硬规则未漂移。"},
        ],
        "delivery": {
            "no_bgm": True,
            "no_burned_subtitles": True,
            "subtitle_track_must_be_empty": True,
            "external_voice_track_must_be_empty": True,
            "environment_sound_required": True,
            "content_review_required": True,
            "html_review_board_required": True,
            "delivery_verifier_required": True,
            "archive_standard_snapshot": True,
        },
    },
}


LOCKED_PRODUCTION_RULES = {
    "video_model": "seedance2.0fast_vip",
    "resolution": "720p",
    "voice": "jimeng_builtin",
    "lip_sync": True,
    "burn_subtitles": False,
    "fast_vip_real_face_conflict": "pause_for_confirmation",
}

REQUIRED_GATE_IDS = [
    "script_bible", "character_assets", "continuity", "spatial",
    "spatial_seedance", "five_dimensions", "duration", "dialogue",
    "performance", "camera", "people", "text", "frames", "audio", "profile",
]


class StandardValidationError(ValueError):
    """制作标准校验失败，``issues`` 可直接供 API/UI 定位字段。"""

    def __init__(self, issues):
        self.issues = list(issues)
        message = "制作标准校验失败: " + "; ".join(
            f"{item['path']}: {item['message']}" for item in self.issues)
        super().__init__(message)


class StandardConflictError(RuntimeError):
    """乐观锁冲突：保存期间激活版本已被其他操作者改变。"""

    def __init__(self, expected_active_id, actual_active_id):
        self.expected_active_id = expected_active_id
        self.actual_active_id = actual_active_id
        super().__init__(
            f"激活版本已变化（期望 {expected_active_id}，实际 {actual_active_id}）")


def _canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def _fingerprint(content):
    return hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class StandardCenter:
    def __init__(self, db, config=None):
        self.db = db
        self.config = config
        self.ensure_default()

    @staticmethod
    def validate(content):
        """返回 ``[{path, message}]``，不抛异常，便于表单逐项显示。"""
        issues = []

        def issue(path, message):
            issues.append({"path": path, "message": message})

        def required_dict(parent, key, path):
            value = parent.get(key) if isinstance(parent, dict) else None
            if not isinstance(value, dict):
                issue(path, "必须是对象")
                return {}
            return value

        def bool_field(parent, key, path):
            value = parent.get(key) if isinstance(parent, dict) else None
            if not isinstance(value, bool):
                issue(path, "必须是布尔值")

        def nonempty_string(parent, key, path):
            value = parent.get(key) if isinstance(parent, dict) else None
            if not isinstance(value, str) or not value.strip():
                issue(path, "必须是非空字符串")

        def numeric_pair(parent, key, path, minimum=0, maximum=None):
            value = parent.get(key) if isinstance(parent, dict) else None
            if (not isinstance(value, list) or len(value) != 2
                    or not all(_is_number(item) for item in value)):
                issue(path, "必须是包含两个数字的范围")
                return None
            if value[0] > value[1]:
                issue(path, "范围下限不能大于上限")
            if value[0] < minimum:
                issue(path, f"范围下限不能小于 {minimum}")
            if maximum is not None and value[1] > maximum:
                issue(path, f"范围上限不能大于 {maximum}")
            return value

        if not isinstance(content, dict):
            return [{"path": "$", "message": "制作标准必须是对象"}]
        try:
            _canonical_json(content)
        except (TypeError, ValueError) as exc:
            issue("$", f"必须可序列化为标准 JSON：{exc}")

        for key in ("profile_key", "name", "description"):
            nonempty_string(content, key, key)
        source = required_dict(content, "source_skill", "source_skill")
        for key in ("id", "name", "version", "reference", "principle"):
            nonempty_string(source, key, f"source_skill.{key}")
        rules = required_dict(content, "rules", "rules")
        governance = required_dict(
            rules, "rule_governance", "rules.rule_governance")
        nonempty_string(
            governance, "schema", "rules.rule_governance.schema")
        for key in (
                "single_integrated_qc", "collect_all_issues_before_repair",
                "unchanged_retry_forbidden", "current_shot_only_repair",
                "provider_may_silently_override",
                "qc_observation_auto_promotes_to_rule"):
            bool_field(
                governance, key, f"rules.rule_governance.{key}")
        attempts = governance.get("max_auto_repair_attempts")
        if (not isinstance(attempts, int) or isinstance(attempts, bool)
                or attempts != 1):
            issue(
                "rules.rule_governance.max_auto_repair_attempts",
                "默认只允许一次有针对性的自动修复")
        precedence = governance.get("precedence")
        if (not isinstance(precedence, list) or not precedence
                or any(not isinstance(item, dict) or not item.get("id")
                       for item in precedence)):
            issue(
                "rules.rule_governance.precedence",
                "必须定义非空且可追溯的规则优先级")
        scopes = governance.get("scopes")
        if (not isinstance(scopes, list) or not scopes
                or any(not isinstance(item, dict) or not item.get("id")
                       or not item.get("expires") for item in scopes)):
            issue(
                "rules.rule_governance.scopes",
                "永久、项目、镜头和临时修正规则必须分层并定义有效期")
        owners = governance.get("owners")
        if not isinstance(owners, dict) or not owners:
            issue(
                "rules.rule_governance.owners",
                "每类规则必须登记唯一 owner 与运行时 consumer")
        else:
            for owner_key, owner in owners.items():
                if (not isinstance(owner, dict) or not owner.get("owner")
                        or not owner.get("consumer")
                        or not owner.get("stage")):
                    issue(
                        f"rules.rule_governance.owners.{owner_key}",
                        "必须包含 owner、consumer 与 stage")
        pilot = governance.get("pilot")
        if not isinstance(pilot, dict):
            issue("rules.rule_governance.pilot", "必须定义小样片策略")
        else:
            for key in (
                    "enabled_by_default", "simple_project_may_waive",
                    "human_approval_required"):
                bool_field(
                    pilot, key, f"rules.rule_governance.pilot.{key}")
            duration = pilot.get("duration_seconds")
            if (not _is_number(duration) or not 5 <= duration <= 15):
                issue(
                    "rules.rule_governance.pilot.duration_seconds",
                    "小样片时长必须为 5 到 15 秒")

        production = required_dict(rules, "production", "rules.production")
        for key, locked in LOCKED_PRODUCTION_RULES.items():
            value = production.get(key)
            if type(value) is not type(locked) or value != locked:
                issue(
                    f"rules.production.{key}",
                    f"硬规则已锁定为 {json.dumps(locked, ensure_ascii=False)}")
        for key in ("pipeline_version", "text_lock_provider", "prompt_strategy",
                    "fast_vip_real_face_conflict"):
            nonempty_string(production, key, f"rules.production.{key}")
        # 旧标准在迁移前允许暂时缺少该字段；一旦存在，就必须严格符合
        # 逐镜按需升级合同，避免把 2.5 当成全局默认或静默截断素材/时长。
        model_upgrade_policy = production.get("model_upgrade_policy")
        if model_upgrade_policy is not None:
            if not isinstance(model_upgrade_policy, dict):
                issue(
                    "rules.production.model_upgrade_policy",
                    "必须是逐镜模型升级策略对象")
            else:
                for key, expected in MODEL_UPGRADE_POLICY.items():
                    value = model_upgrade_policy.get(key)
                    if type(value) is not type(expected) or value != expected:
                        issue(
                            f"rules.production.model_upgrade_policy.{key}",
                            "升级规则已锁定为 "
                            f"{json.dumps(expected, ensure_ascii=False)}")
        prompt_contract = required_dict(
            production, "prompt_contract", "rules.production.prompt_contract")
        nonempty_string(
            prompt_contract, "schema", "rules.production.prompt_contract.schema")
        if prompt_contract.get("schema") != "aifos.shot-prompt/v2.2":
            issue(
                "rules.production.prompt_contract.schema",
                "镜头提示词合同必须是 aifos.shot-prompt/v2.2")
        for key in (
                "single_primary_action", "single_camera_move",
                "static_frame_target_required",
                "compact_prompt_sent_to_model", "full_prompt_kept_for_audit",
                "pre_generation_review_required",
                "pre_generation_review_fail_closed",
                "optimized_prompt_sent_to_model"):
            bool_field(
                prompt_contract, key,
                f"rules.production.prompt_contract.{key}")
        if prompt_contract.get(
                "pre_generation_review_provider") != "codex":
            issue(
                "rules.production.prompt_contract."
                "pre_generation_review_provider",
                "所有真实图片提示词必须先由Codex审核优化")
        if prompt_contract.get(
                "pre_generation_review_schema") != \
                "aifos.codex-prompt-review/v1":
            issue(
                "rules.production.prompt_contract."
                "pre_generation_review_schema",
                "Codex提示词审核合同版本不正确")
        order = prompt_contract.get("order")
        if (not isinstance(order, list) or len(order) < 5
                or len(set(order)) != len(order)
                or not all(isinstance(item, str) and item for item in order)):
            issue(
                "rules.production.prompt_contract.order",
                "必须是无重复的镜头提示词字段顺序列表")
        roles = prompt_contract.get("reference_roles")
        if (not isinstance(roles, list) or not roles
                or not all(isinstance(item, str) and item for item in roles)):
            issue(
                "rules.production.prompt_contract.reference_roles",
                "必须是非空参考图职责列表")
        if prompt_contract.get("validation_statuses") != [
                "PASS", "WARN", "BLOCK"]:
            issue(
                "rules.production.prompt_contract.validation_statuses",
                "必须严格为 PASS/WARN/BLOCK")
        for field, required in (
                ("headwear_fields", {
                    "presence", "kind", "name", "hair_visibility"}),
                ("character_condition_dimensions", {
                    "life_state", "consciousness_state",
                    "embodiment", "mobility"})):
            values = prompt_contract.get(field)
            if not isinstance(values, list) or set(values) != required:
                issue(
                    f"rules.production.prompt_contract.{field}",
                    "字段集合与 v2.2 规范不一致")
        if (prompt_contract.get("prop_contract_schema")
                != "aifos.prop-contract/v2.2"):
            issue(
                "rules.production.prompt_contract.prop_contract_schema",
                "必须是 aifos.prop-contract/v2.2")
        preferred = numeric_pair(
            production, "preferred_segment_seconds",
            "rules.production.preferred_segment_seconds", minimum=0.5,
            maximum=15)
        max_segment = production.get("max_segment_seconds")
        if not _is_number(max_segment) or not 0.5 <= max_segment <= 15:
            issue("rules.production.max_segment_seconds", "必须是 0.5 到 15 秒")
        elif preferred and preferred[1] > max_segment:
            issue(
                "rules.production.preferred_segment_seconds",
                "优选分段上限不能超过单段最大时长")
        precision = production.get("time_precision_seconds")
        if not _is_number(precision) or not 0 < precision <= 1:
            issue("rules.production.time_precision_seconds", "必须大于 0 且不超过 1 秒")
        elif _is_number(max_segment):
            quotient = max_segment / precision
            if abs(quotient - round(quotient)) > 1e-9:
                issue(
                    "rules.production.time_precision_seconds",
                    "单段最大时长必须能被时间精度整除")

        story_analysis = required_dict(
            rules, "story_analysis", "rules.story_analysis")
        script_development = required_dict(
            rules, "script_development", "rules.script_development")
        for key in (
                "required_before_any_visual_asset",
                "source_material_is_adaptable",
                "auto_adapt_imported_source",
                "writer_completes_missing_details",
                "single_integrated_review",
                "scene_boundary_contract_required",
                "local_rewrite_enabled",
                "impact_analysis_before_rewrite",
                "preserve_unaffected_assets",
                "human_approval_if_scope_expands"):
            bool_field(
                script_development, key,
                f"rules.script_development.{key}")
        for key in ("required_review_dimensions", "scene_boundary_fields"):
            values = script_development.get(key)
            if (not isinstance(values, list) or not values
                    or not all(isinstance(item, str) and item.strip()
                               for item in values)):
                issue(
                    f"rules.script_development.{key}",
                    "必须是非空字符串列表")
        nonempty_string(
            script_development, "local_rewrite_default_scope",
            "rules.script_development.local_rewrite_default_scope")
        for key in (
                "required_before_images", "auto_analyze_uploaded_script",
                "user_style_is_hard_constraint",
                "distinguish_world_from_render_medium",
                "editable_before_lock",
                "resolve_character_entities_before_images",
                "performance_cues_are_not_characters",
                "final_character_image_prompt_required",
                "compact_prompt_compilation"):
            bool_field(
                story_analysis, key, f"rules.story_analysis.{key}")
        for key in ("required_sections", "downstream_consumers"):
            values = story_analysis.get(key)
            if (not isinstance(values, list) or not values
                    or not all(isinstance(item, str) and item.strip()
                               for item in values)):
                issue(
                    f"rules.story_analysis.{key}",
                    "必须是非空字符串列表")
        for key in ("visible_text_policy", "default_visual_fallback"):
            nonempty_string(
                story_analysis, key, f"rules.story_analysis.{key}")

        text_assets = required_dict(
            rules, "text_assets", "rules.text_assets")
        for key in (
                "explicit_whitelist_only", "style_description_required",
                "forbid_prompt_metadata_as_text",
                "no_readable_text_without_whitelist"):
            bool_field(text_assets, key, f"rules.text_assets.{key}")
        for key in (
                "keyframe_provider", "video_policy", "dense_text_policy"):
            nonempty_string(text_assets, key, f"rules.text_assets.{key}")
        for key in ("required_fields", "forbidden_system_fields", "qc_requirements"):
            values = text_assets.get(key)
            if (not isinstance(values, list) or not values
                    or not all(isinstance(item, str) and item.strip()
                               for item in values)):
                issue(f"rules.text_assets.{key}", "必须是非空字符串列表")

        character_assets = required_dict(
            rules, "character_assets", "rules.character_assets")
        for key in (
                "workwear_required_for_occupational_roles",
                "visual_dna_required", "forbid_template_labels",
                "cast_dedup_required",
                "turnaround_review_board_only",
                "canonical_views_are_separate_assets",
                "identity_and_wardrobe_separated", "clean_skin_default"):
            bool_field(
                character_assets, key, f"rules.character_assets.{key}")
        if character_assets.get(
                "character_asset_depth_source") != \
                "episode_character_asset_policy":
            issue(
                "rules.character_assets.character_asset_depth_source",
                "四视图与细节图必须只由本集人物资产策略决定")
        if character_assets.get(
                "character_asset_default_mode") not in (
                    "auto", "simple", "full"):
            issue(
                "rules.character_assets.character_asset_default_mode",
                "人物资产默认模式只能是 auto、simple 或 full")
        if character_assets.get("seedance_may_redesign_character") is not False:
            issue(
                "rules.character_assets.seedance_may_redesign_character",
                "Seedance 只能动画化锁定资产，不能重新设计人物")
        sequence = character_assets.get("visual_dna_sequence")
        required_sequence = [
            "story_evidence", "experience_and_situation",
            "personality_and_behavior", "visible_traits",
            "cast_dedup", "three_view_assets",
        ]
        if sequence != required_sequence:
            issue(
                "rules.character_assets.visual_dna_sequence",
                "人物设计顺序必须为剧情证据→经历处境→性格行为→可见特征"
                "→全剧去重→三视图资产")
        dimensions = character_assets.get("cast_dedup_dimensions")
        if (not isinstance(dimensions, list) or len(dimensions) < 6
                or len(set(dimensions)) != len(dimensions)
                or not all(isinstance(item, str) and item.strip()
                           for item in dimensions)):
            issue(
                "rules.character_assets.cast_dedup_dimensions",
                "必须定义至少 6 个不重复的角色视觉去重维度")
        threshold = character_assets.get("cast_dedup_overlap_threshold")
        if (not isinstance(threshold, int) or isinstance(threshold, bool)
                or threshold != 2):
            issue(
                "rules.character_assets.cast_dedup_overlap_threshold",
                "两个及以上主要视觉维度重叠时必须重设计")
        if character_assets.get("canonical_views") != [
                "face_closeup", "front", "profile", "back"]:
            issue(
                "rules.character_assets.canonical_views",
                "正式人物母资产必须依次为面部近景、正面、严格侧面和完整背面")
        if character_assets.get(
                "turnaround_review_board_aspect") != "16:9":
            issue(
                "rules.character_assets.turnaround_review_board_aspect",
                "三视图审核板必须为 16:9")
        targets = required_dict(
            character_assets, "candidate_targets",
            "rules.character_assets.candidate_targets")
        required_targets = {
            "main": 4, "important_supporting": 4,
            "non_main": 4, "non_main_max": 4, "background": 0,
        }
        if targets != required_targets:
            issue(
                "rules.character_assets.candidate_targets",
                "候选额度必须为所有正式角色4张、背景路人0张")
        if character_assets.get("core_prop_candidate_target") != 4:
            issue(
                "rules.character_assets.core_prop_candidate_target",
                "核心道具必须统一四选一")
        if character_assets.get(
                "incidental_props_use_continuity_ledger_only") is not True:
            issue(
                "rules.character_assets.incidental_props_use_continuity_ledger_only",
                "一次性普通小物只能进入连续性台账，不能批量建立候选资产")

        inner_persona = required_dict(
            rules, "inner_persona", "rules.inner_persona")
        if inner_persona.get("schema") != "aifos.inner-persona/v1":
            issue(
                "rules.inner_persona.schema",
                "内心人格规则必须使用 aifos.inner-persona/v1")
        if inner_persona.get("mode") != "chibi_overlay":
            issue(
                "rules.inner_persona.mode",
                "内心人格必须采用非现实 Q 版叠层")
        for key in (
                "physical_presence", "counts_as_real_character",
                "included_in_spatial_blocking",
                "historical_characters_may_react",
                "inherit_signature_props", "auto_after_every_dialogue"):
            if inner_persona.get(key) is not False:
                issue(
                    f"rules.inner_persona.{key}",
                    "内心 Q 版不得成为实体、真实人数、空间角色、历史人物"
                    "反应对象、默认道具来源或逐句自动插入项")
        for key in (
                "real_form_forbidden_after_transition",
                "host_mouth_closed_for_inner_voice",
                "forbid_burned_subtitles"):
            if inner_persona.get(key) is not True:
                issue(
                    f"rules.inner_persona.{key}",
                    "该内心人格硬规则必须开启")
        if inner_persona.get("visible_to") != "host_only":
            issue(
                "rules.inner_persona.visible_to",
                "Q 版内心人格只能被宿主内心感知")
        if inner_persona.get("wardrobe_source") != "locked_current_look":
            issue(
                "rules.inner_persona.wardrobe_source",
                "Q 版衣着必须继承已锁定的当前造型")
        if inner_persona.get("expression_style") != "exaggerated":
            issue(
                "rules.inner_persona.expression_style",
                "Q 版内心表演必须允许夸张表达")
        if inner_persona.get(
                "proportion_style") != "oversized_head_tiny_body":
            issue(
                "rules.inner_persona.proportion_style",
                "Q版必须采用头部明显大于身体的大头小身比例")
        total_heads = inner_persona.get("total_height_in_heads")
        if (not isinstance(total_heads, (int, float))
                or isinstance(total_heads, bool)
                or not 1.7 <= float(total_heads) <= 1.9):
            issue(
                "rules.inner_persona.total_height_in_heads",
                "Q版总高必须控制在1.7至1.9个头高")
        head_ratio = inner_persona.get("head_height_ratio")
        if (not isinstance(head_ratio, (int, float))
                or isinstance(head_ratio, bool)
                or not 0.55 <= float(head_ratio) <= 0.60):
            issue(
                "rules.inner_persona.head_height_ratio",
                "Q版头部必须占总高55%至60%")
        if inner_persona.get("body_smaller_than_head") is not True:
            issue(
                "rules.inner_persona.body_smaller_than_head",
                "Q版身体必须在视觉上小于头部")
        if inner_persona.get("max_overlays_per_shot") != 1:
            issue(
                "rules.inner_persona.max_overlays_per_shot",
                "每镜最多一个内心 Q 版叠层")
        functions = inner_persona.get("allowed_functions")
        required_functions = {
            "inner_monologue", "inner_commentary",
            "comic_reaction", "decision_conflict",
        }
        if (not isinstance(functions, list)
                or not required_functions.issubset(set(functions))):
            issue(
                "rules.inner_persona.allowed_functions",
                "必须覆盖内心独白、内心吐槽、喜剧反应与决策冲突")

        dialogue = required_dict(rules, "dialogue", "rules.dialogue")
        bool_field(dialogue, "preserve_verbatim", "rules.dialogue.preserve_verbatim")
        bool_field(dialogue, "split_at_natural_pause", "rules.dialogue.split_at_natural_pause")
        max_chars = dialogue.get("max_chars_per_shot")
        if (not isinstance(max_chars, int) or isinstance(max_chars, bool)
                or not 1 <= max_chars <= 25):
            issue("rules.dialogue.max_chars_per_shot", "必须是 1 到 25 的整数")
        nonempty_string(dialogue, "duration_formula", "rules.dialogue.duration_formula")
        profiles = required_dict(dialogue, "speech_profiles", "rules.dialogue.speech_profiles")
        for key in ("tense_angry", "daily", "sad_gentle", "trembling"):
            profile = required_dict(
                profiles, key, f"rules.dialogue.speech_profiles.{key}")
            nonempty_string(profile, "label", f"rules.dialogue.speech_profiles.{key}.label")
            numeric_pair(
                profile, "chars_per_second",
                f"rules.dialogue.speech_profiles.{key}.chars_per_second",
                minimum=0.1, maximum=20)
            numeric_pair(
                profile, "buffer_seconds",
                f"rules.dialogue.speech_profiles.{key}.buffer_seconds",
                minimum=0, maximum=5)

        performance = required_dict(rules, "performance", "rules.performance")
        for key in (
                "reaction_after_key_dialogue", "beat_at_emotional_peak",
                "beat_requires_visible_acting", "physical_action_separate_shot",
                "performance_goal_required"):
            bool_field(performance, key, f"rules.performance.{key}")
        ratio = performance.get("listener_duration_ratio")
        if not _is_number(ratio) or not (2 / 3) <= ratio <= 1:
            issue("rules.performance.listener_duration_ratio", "必须介于 2/3 与 1 之间")
        numeric_pair(
            performance, "reaction_seconds", "rules.performance.reaction_seconds",
            minimum=0.5, maximum=10)
        beat = numeric_pair(
            performance, "beat_seconds", "rules.performance.beat_seconds",
            minimum=0.5, maximum=10)
        if beat and (beat[0] < 2 or beat[1] > 4):
            issue("rules.performance.beat_seconds", "SK V5 留白镜必须在 2 到 4 秒内")

        storyboard = required_dict(rules, "storyboard", "rules.storyboard")
        dimensions = storyboard.get("five_dimensions")
        if not isinstance(dimensions, list) or len(dimensions) != 5:
            issue("rules.storyboard.five_dimensions", "必须完整定义五个维度")
        elif any(not isinstance(item, dict) or not item.get("id")
                 or not item.get("label") or not item.get("description")
                 for item in dimensions):
            issue("rules.storyboard.five_dimensions", "每个维度必须包含 id、label、description")
        columns = storyboard.get("required_columns")
        if (not isinstance(columns, list) or len(columns) != 17
                or len(set(columns)) != 17
                or not all(isinstance(item, str) and item for item in columns)):
            issue("rules.storyboard.required_columns", "必须是 17 个不重复的中文镜头表列名")
        words = storyboard.get("scene_type_words")
        if (not isinstance(words, list) or len(words) != 8
                or len(set(words)) != 8
                or not all(isinstance(item, str) and item for item in words)):
            issue("rules.storyboard.scene_type_words", "必须是 8 个不重复的类型词")
        for key in (
                "forbid_repeated_scale_and_angle", "environment_sound_required",
                "visual_hook_required", "eye_line_required"):
            bool_field(storyboard, key, f"rules.storyboard.{key}")
        vertical = storyboard.get("minimum_vertical_angles_per_segment")
        if (not isinstance(vertical, int) or isinstance(vertical, bool)
                or vertical < 2):
            issue("rules.storyboard.minimum_vertical_angles_per_segment", "必须是至少 2 的整数")
        jump = storyboard.get("adjacent_shot_scale_jump_levels")
        if not isinstance(jump, int) or isinstance(jump, bool) or jump < 1:
            issue("rules.storyboard.adjacent_shot_scale_jump_levels", "必须是正整数")
        axis = storyboard.get("adjacent_camera_axis_change_degrees")
        if not _is_number(axis) or not 0 < axis <= 180:
            issue("rules.storyboard.adjacent_camera_axis_change_degrees", "必须大于 0 且不超过 180")
        spatial_group = storyboard.get("spatial_blocking_required_for_group")
        if (not isinstance(spatial_group, int)
                or isinstance(spatial_group, bool) or spatial_group < 2):
            issue("rules.storyboard.spatial_blocking_required_for_group",
                  "必须是至少 2 的整数")

        continuity = required_dict(rules, "continuity", "rules.continuity")
        state_labels = continuity.get("state_labels")
        required_labels = ["姿态", "伤势", "持有道具", "情绪", "朝向关系"]
        if state_labels != required_labels:
            issue("rules.continuity.state_labels", "必须保留五段状态标签及固定顺序")
        for key in (
                "end_state_to_next_start", "character_count_lock",
                "on_stage_characters_only", "canonical_entity_names",
                "costume_and_prop_lock", "scene_change_starts_new_segment",
                "position_uses_frame_geometry"):
            bool_field(continuity, key, f"rules.continuity.{key}")

        camera = required_dict(rules, "camera_library", "rules.camera_library")
        for key in (
                "shot_scales", "angles", "focal_lengths_mm", "positions",
                "movements", "compositions", "speeds"):
            values = camera.get(key)
            if not isinstance(values, list) or not values:
                issue(f"rules.camera_library.{key}", "必须是非空选项列表")
            elif key == "focal_lengths_mm":
                if not all(_is_number(item) and item > 0 for item in values):
                    issue(f"rules.camera_library.{key}", "焦段必须全部为正数")
            elif not all(isinstance(item, str) and item.strip() for item in values):
                issue(f"rules.camera_library.{key}", "选项必须全部为非空字符串")

        gates = rules.get("quality_gates")
        if not isinstance(gates, list):
            issue("rules.quality_gates", "必须是质检门列表")
        else:
            ids = [item.get("id") if isinstance(item, dict) else None for item in gates]
            if ids != REQUIRED_GATE_IDS:
                issue("rules.quality_gates", "质检门 id 及顺序必须为 " + "、".join(REQUIRED_GATE_IDS))
            for index, gate in enumerate(gates):
                path = f"rules.quality_gates.{index}"
                if not isinstance(gate, dict):
                    issue(path, "必须是对象")
                    continue
                nonempty_string(gate, "label", f"{path}.label")
                bool_field(gate, "enabled", f"{path}.enabled")
                if gate.get("severity") not in ("block", "warning"):
                    issue(f"{path}.severity", "必须是 block 或 warning")
                nonempty_string(gate, "description", f"{path}.description")
                bool_field(gate, "mandatory", f"{path}.mandatory")
                nonempty_string(gate, "owner", f"{path}.owner")
                defaults = gate_defaults(gate.get("id"))
                if gate.get("mandatory") != defaults["mandatory"]:
                    issue(
                        f"{path}.mandatory",
                        "门禁 mandatory 属性与统一门禁注册表不一致")
                if gate.get("id") in MANDATORY_GATE_IDS:
                    if gate.get("enabled") is not True:
                        issue(f"{path}.enabled", "合规硬门不能关闭")
                    if gate.get("severity") != "block":
                        issue(f"{path}.severity", "合规硬门不能降为警告")
                elif gate.get("id") in ADVISORY_GATE_IDS \
                        and gate.get("severity") != "warning":
                    issue(
                        f"{path}.severity",
                        "创作启发式默认只能警告，避免机械规则阻断生产")

        delivery = required_dict(rules, "delivery", "rules.delivery")
        for key in (
                "no_bgm", "no_burned_subtitles", "subtitle_track_must_be_empty",
                "external_voice_track_must_be_empty", "environment_sound_required",
                "content_review_required", "html_review_board_required",
                "delivery_verifier_required", "archive_standard_snapshot"):
            bool_field(delivery, key, f"rules.delivery.{key}")
        if delivery.get("no_burned_subtitles") is not True:
            issue("rules.delivery.no_burned_subtitles", "无字幕交付必须开启")
        if delivery.get("subtitle_track_must_be_empty") is not True:
            issue("rules.delivery.subtitle_track_must_be_empty", "字幕轨必须为空")

        existing = {(item["path"], item["message"]) for item in issues}
        for conflict in audit_rule_configuration(content):
            key = (conflict["path"], conflict["message"])
            if key not in existing:
                issues.append({
                    "path": conflict["path"],
                    "message": conflict["message"],
                })
                existing.add(key)
        return issues

    def ensure_default(self):
        row = self.db.query_one(
            "SELECT id FROM production_standard_versions "
            "WHERE profile_key=? ORDER BY version DESC LIMIT 1",
            (DEFAULT_PROFILE_KEY,))
        if row is None:
            try:
                return self.save(
                    copy.deepcopy(DEFAULT_STANDARD),
                    change_note="初始化 SK 漫剧五维分镜 V5 标准")
            except sqlite3.IntegrityError:
                # 多进程同时首次启动时，另一进程可能已完成初始化。
                pass
        state = self.db.query_one(
            "SELECT active_version_id FROM production_standard_state "
            "WHERE profile_key=?", (DEFAULT_PROFILE_KEY,))
        if state is None:
            latest = self.db.query_one(
                "SELECT id FROM production_standard_versions "
                "WHERE profile_key=? ORDER BY version DESC LIMIT 1",
                (DEFAULT_PROFILE_KEY,))
            if latest is not None:
                return self._upgrade_spatial_standard(
                    self.activate(latest["id"]))
        return self._upgrade_spatial_standard(self.active())

    def _upgrade_spatial_standard(self, snapshot):
        """无损补齐新增规则并创建新版本，历史标准仍可追溯。"""
        if not snapshot:
            return snapshot
        content = copy.deepcopy(snapshot.get("content") or {})
        rules = content.get("rules") if isinstance(content, dict) else None
        if not isinstance(rules, dict):
            return snapshot
        changed = False
        governance_defaults = DEFAULT_STANDARD["rules"]["rule_governance"]
        governance = rules.get("rule_governance")
        if not isinstance(governance, dict):
            rules["rule_governance"] = copy.deepcopy(
                governance_defaults)
            changed = True
        else:
            for key, value in governance_defaults.items():
                if key not in governance:
                    governance[key] = copy.deepcopy(value)
                    changed = True
        production_rules = rules.get("production")
        production_defaults = DEFAULT_STANDARD["rules"]["production"]
        if not isinstance(production_rules, dict):
            rules["production"] = copy.deepcopy(production_defaults)
            changed = True
        else:
            for key, value in production_defaults.items():
                if key not in production_rules:
                    production_rules[key] = copy.deepcopy(value)
                    changed = True
            prompt_contract = production_rules.get("prompt_contract")
            prompt_defaults = production_defaults["prompt_contract"]
            if not isinstance(prompt_contract, dict):
                production_rules["prompt_contract"] = copy.deepcopy(
                    prompt_defaults)
                changed = True
            else:
                if prompt_contract.get("schema") != (
                        "aifos.shot-prompt/v2.2"):
                    prompt_contract["schema"] = "aifos.shot-prompt/v2.2"
                    changed = True
                for key, value in prompt_defaults.items():
                    if key not in prompt_contract:
                        prompt_contract[key] = copy.deepcopy(value)
                        changed = True
        source = content.get("source_skill")
        source_defaults = DEFAULT_STANDARD["source_skill"]
        if (isinstance(source, dict)
                and source.get("id") == source_defaults["id"]):
            def version_tuple(value):
                return tuple(
                    int(part) for part in str(value or "0").split(".")
                    if part.isdigit())

            if version_tuple(source.get("version")) < version_tuple(
                    source_defaults["version"]):
                source["version"] = source_defaults["version"]
                changed = True
            if not str(source.get("principle") or "").strip():
                source["principle"] = copy.deepcopy(
                    source_defaults["principle"])
                changed = True
        text_rules = rules.get("text_assets")
        text_defaults = DEFAULT_STANDARD["rules"]["text_assets"]
        if not isinstance(text_rules, dict):
            rules["text_assets"] = copy.deepcopy(text_defaults)
            changed = True
        else:
            for key, value in text_defaults.items():
                if key not in text_rules:
                    text_rules[key] = copy.deepcopy(value)
                    changed = True
        storyboard = rules.get("storyboard")
        if isinstance(storyboard, dict) \
                and "spatial_blocking_required_for_group" not in storyboard:
            storyboard["spatial_blocking_required_for_group"] = 3
            changed = True
        gates = rules.get("quality_gates")
        if isinstance(gates, list) and not any(
                isinstance(gate, dict) and gate.get("id") == "script_bible"
                for gate in gates):
            gate = next(copy.deepcopy(item)
                        for item in DEFAULT_STANDARD["rules"]["quality_gates"]
                        if item.get("id") == "script_bible")
            gates.insert(0, gate)
            changed = True
        if isinstance(gates, list) and not any(
                isinstance(gate, dict) and gate.get("id") == "spatial"
                for gate in gates):
            gate = next(copy.deepcopy(item)
                        for item in DEFAULT_STANDARD["rules"]["quality_gates"]
                        if item.get("id") == "spatial")
            insert_at = next((index + 1 for index, item in enumerate(gates)
                              if isinstance(item, dict)
                              and item.get("id") == "continuity"), 0)
            gates.insert(insert_at, gate)
            changed = True
        if isinstance(gates, list) and not any(
                isinstance(gate, dict) and gate.get("id") == "character_assets"
                for gate in gates):
            gate = next(copy.deepcopy(item)
                        for item in DEFAULT_STANDARD["rules"]["quality_gates"]
                        if item.get("id") == "character_assets")
            insert_at = next((index + 1 for index, item in enumerate(gates)
                              if isinstance(item, dict)
                              and item.get("id") == "script_bible"), 0)
            gates.insert(insert_at, gate)
            changed = True
        if isinstance(gates, list) and not any(
                isinstance(gate, dict)
                and gate.get("id") == "spatial_seedance"
                for gate in gates):
            gate = next(copy.deepcopy(item)
                        for item in DEFAULT_STANDARD["rules"]["quality_gates"]
                        if item.get("id") == "spatial_seedance")
            insert_at = next((index + 1 for index, item in enumerate(gates)
                              if isinstance(item, dict)
                              and item.get("id") == "spatial"), 0)
            gates.insert(insert_at, gate)
            changed = True
        if isinstance(gates, list):
            default_gates = {
                item["id"]: item
                for item in DEFAULT_STANDARD["rules"]["quality_gates"]
            }
            for gate in gates:
                if (not isinstance(gate, dict)
                        or gate.get("id") not in default_gates):
                    continue
                default_gate = default_gates[gate["id"]]
                for key in ("mandatory", "owner"):
                    if gate.get(key) != default_gate[key]:
                        gate[key] = copy.deepcopy(default_gate[key])
                        changed = True
                if gate.get("severity") != default_gate["severity"]:
                    gate["severity"] = default_gate["severity"]
                    changed = True
                if default_gate["mandatory"] \
                        and gate.get("enabled") is not True:
                    gate["enabled"] = True
                    changed = True
        asset_rules = rules.get("character_assets")
        defaults = DEFAULT_STANDARD["rules"]["character_assets"]
        if not isinstance(asset_rules, dict):
            changed = True
            rules["character_assets"] = copy.deepcopy(defaults)
        else:
            for key, value in defaults.items():
                if key not in asset_rules:
                    asset_rules[key] = copy.deepcopy(value)
                    changed = True
            if "three_view_required_after_lock" in asset_rules:
                asset_rules.pop("three_view_required_after_lock", None)
                changed = True
            targets = asset_rules.get("candidate_targets")
            default_targets = defaults["candidate_targets"]
            if not isinstance(targets, dict):
                asset_rules["candidate_targets"] = copy.deepcopy(
                    default_targets)
                changed = True
            else:
                for key in (
                        "main", "important_supporting",
                        "non_main", "non_main_max", "background"):
                    if targets.get(key) != default_targets[key]:
                        targets[key] = default_targets[key]
                        changed = True
        story_defaults = DEFAULT_STANDARD["rules"]["story_analysis"]
        script_defaults = DEFAULT_STANDARD["rules"]["script_development"]
        script_rules = rules.get("script_development")
        if not isinstance(script_rules, dict):
            rules["script_development"] = copy.deepcopy(script_defaults)
            changed = True
        else:
            for key, value in script_defaults.items():
                if key not in script_rules:
                    script_rules[key] = copy.deepcopy(value)
                    changed = True
        story_rules = rules.get("story_analysis")
        if not isinstance(story_rules, dict):
            rules["story_analysis"] = copy.deepcopy(story_defaults)
            changed = True
        else:
            for key, value in story_defaults.items():
                if key not in story_rules:
                    story_rules[key] = copy.deepcopy(value)
                    changed = True
        inner_defaults = DEFAULT_STANDARD["rules"]["inner_persona"]
        inner_rules = rules.get("inner_persona")
        if not isinstance(inner_rules, dict):
            rules["inner_persona"] = copy.deepcopy(inner_defaults)
            changed = True
        else:
            for key, value in inner_defaults.items():
                if key not in inner_rules:
                    inner_rules[key] = copy.deepcopy(value)
                    changed = True
        if not changed:
            return snapshot
        try:
            return self.save(
                content, change_note=(
                    "自动升级：加入视觉 DNA、全剧角色去重、三视图母资产、"
                    "剧本第一道总闸门、道具生命周期、局部返编边界、"
                    "剧本分析、剧情事实源、空间调度与非现实Q版内心人格规则"),
                expected_active_id=snapshot.get("version_id"))
        except StandardConflictError:
            # 多个 App 同时启动时由先完成者负责升级。
            return self.active()

    def _row_to_snapshot(self, row):
        if row is None:
            return None
        active = self.db.query_one(
            "SELECT 1 FROM production_standard_state "
            "WHERE profile_key=? AND active_version_id=?",
            (row["profile_key"], row["id"]))
        return {
            "version_id": row["id"],
            "profile_key": row["profile_key"],
            "version": row["version"],
            "name": row["name"],
            "description": row["description"],
            "content": json.loads(row["content"]),
            "change_note": row["change_note"],
            "fingerprint": row["fingerprint"],
            "created_at": row["created_at"],
            "active": active is not None,
        }

    def active(self):
        row = self.db.query_one(
            "SELECT v.* FROM production_standard_state s "
            "JOIN production_standard_versions v ON v.id=s.active_version_id "
            "WHERE s.profile_key=?", (DEFAULT_PROFILE_KEY,))
        return self._row_to_snapshot(row)

    def history(self, profile_key=None):
        if profile_key is None:
            rows = self.db.query(
                "SELECT * FROM production_standard_versions "
                "ORDER BY created_at DESC, id DESC")
        else:
            rows = self.db.query(
                "SELECT * FROM production_standard_versions "
                "WHERE profile_key=? ORDER BY version DESC", (profile_key,))
        return [self._row_to_snapshot(row) for row in rows]

    def get(self, version_id):
        row = self.db.query_one(
            "SELECT * FROM production_standard_versions WHERE id=?",
            (version_id,))
        if row is None:
            raise KeyError(f"制作标准版本不存在: {version_id}")
        return self._row_to_snapshot(row)

    def save(self, content, change_note="", activate=True,
             expected_active_id=None):
        candidate = copy.deepcopy(content)
        issues = self.validate(candidate)
        if issues:
            raise StandardValidationError(issues)
        profile_key = candidate["profile_key"].strip()
        name = candidate["name"].strip()
        description = candidate["description"].strip()
        candidate["profile_key"] = profile_key
        candidate["name"] = name
        candidate["description"] = description
        serialized = _canonical_json(candidate)
        fingerprint = _fingerprint(candidate)
        conn = self.db.conn
        self.db._lock.acquire()   # 与并行产线共享连接,事务期间独占
        try:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute(
                "SELECT active_version_id FROM production_standard_state "
                "WHERE profile_key=?", (profile_key,)).fetchone()
            actual_active_id = state["active_version_id"] if state else None
            if (expected_active_id is not None
                    and actual_active_id != expected_active_id):
                raise StandardConflictError(
                    expected_active_id, actual_active_id)
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
                "FROM production_standard_versions WHERE profile_key=?",
                (profile_key,)).fetchone()
            version = row["next_version"]
            created_at = time.time()
            cursor = conn.execute(
                "INSERT INTO production_standard_versions "
                "(profile_key, version, name, description, content, change_note, "
                " fingerprint, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (profile_key, version, name, description, serialized,
                 str(change_note or ""), fingerprint, created_at))
            version_id = cursor.lastrowid
            if activate:
                conn.execute(
                    "INSERT INTO production_standard_state "
                    "(profile_key, active_version_id, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(profile_key) DO UPDATE SET "
                    "active_version_id=excluded.active_version_id, "
                    "updated_at=excluded.updated_at",
                    (profile_key, version_id, created_at))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self.db._lock.release()
        return self.get(version_id)

    def activate(self, version_id):
        conn = self.db.conn
        self.db._lock.acquire()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT profile_key FROM production_standard_versions WHERE id=?",
                (version_id,)).fetchone()
            if row is None:
                raise KeyError(f"制作标准版本不存在: {version_id}")
            conn.execute(
                "INSERT INTO production_standard_state "
                "(profile_key, active_version_id, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(profile_key) DO UPDATE SET "
                "active_version_id=excluded.active_version_id, "
                "updated_at=excluded.updated_at",
                (row["profile_key"], version_id, time.time()))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self.db._lock.release()
        return self.get(version_id)

    def reset(self, change_note=""):
        return self.save(
            copy.deepcopy(DEFAULT_STANDARD),
            change_note=change_note or "恢复 SK 漫剧五维分镜 V5 默认标准",
            activate=True)

    def export_bundle(self, version_id=None):
        standard = self.get(version_id) if version_id is not None else self.active()
        if standard is None:
            raise KeyError("当前没有激活的制作标准")
        return {
            "schema": STANDARD_BUNDLE_SCHEMA,
            "exported_at": time.time(),
            "standard": copy.deepcopy(standard),
        }

    def import_bundle(self, bundle, change_note="", activate=True):
        if isinstance(bundle, str):
            try:
                bundle = json.loads(bundle)
            except json.JSONDecodeError as exc:
                raise StandardValidationError([{
                    "path": "$", "message": f"导入内容不是有效 JSON：{exc}"}]) from exc
        if not isinstance(bundle, dict):
            raise StandardValidationError([{
                "path": "$", "message": "导入包必须是对象"}])
        if "standard" in bundle:
            if bundle.get("schema") not in (None, STANDARD_BUNDLE_SCHEMA):
                raise StandardValidationError([{
                    "path": "schema", "message": "不支持的制作标准包版本"}])
            source = bundle["standard"]
            content = source.get("content") if isinstance(source, dict) else None
            expected_fingerprint = (
                source.get("fingerprint") if isinstance(source, dict) else None)
        elif "content" in bundle:
            content = bundle.get("content")
            expected_fingerprint = bundle.get("fingerprint")
        else:
            content = bundle
            expected_fingerprint = None
        if not isinstance(content, dict):
            raise StandardValidationError([{
                "path": "standard.content", "message": "缺少制作标准内容"}])
        if (expected_fingerprint
                and expected_fingerprint != _fingerprint(content)):
            raise StandardValidationError([{
                "path": "standard.fingerprint",
                "message": "指纹不匹配，导入包可能已损坏或被篡改"}])
        return self.save(
            content, change_note=change_note or "导入制作标准",
            activate=activate)
