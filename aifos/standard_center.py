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


DEFAULT_PROFILE_KEY = "sk-manju-v5"
STANDARD_BUNDLE_SCHEMA = "aifos.production-standard/v1"


DEFAULT_STANDARD = {
    "profile_key": DEFAULT_PROFILE_KEY,
    "name": "SK AI 漫剧工业制作标准 V5",
    "description": "五维分镜驱动的高密度、强连续性、无字幕精品漫剧生产标准。",
    "source_skill": {
        "id": "sk-manju-storyboard-skill",
        "name": "SK 漫剧五维分镜制作 Skill",
        "version": "5.0",
        "reference": "five-dimension-storyboard-template-v5.txt",
        "principle": "先完成五维分镜和硬门校验，再进入关键帧与 Seedance 生产。",
    },
    "rules": {
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
            "fast_vip_real_face_conflict": "pause_for_confirmation",
        },
        "story_analysis": {
            "required_before_images": True,
            "auto_analyze_uploaded_script": True,
            "user_style_is_hard_constraint": True,
            "distinguish_world_from_render_medium": True,
            "editable_before_lock": True,
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
        "character_assets": {
            "background": "pure_background_no_text_no_scene",
            "reference_identity_priority": ["face", "hair"],
            "workwear_required_for_occupational_roles": True,
            "candidate_targets": {
                "main": 5,
                "important_supporting": 3,
                "non_main": 1,
                "non_main_max": 1,
                "background": 0,
            },
            "initial_portrait_quality_gate": False,
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
            {"id": "script_bible", "label": "剧情事实源", "enabled": True, "severity": "block", "description": "故事世界、前情、人物介绍及场次人物名单完整且不互相冲突。"},
            {"id": "continuity", "label": "连续性", "enabled": True, "severity": "block", "description": "人物、服装、道具、站位及段间状态无跳变。"},
            {"id": "spatial", "label": "空间调度", "enabled": True, "severity": "block", "description": "逐场锁定人物走位、机位、视锥和屏幕轴线，防止多人漂移或增殖。"},
            {"id": "five_dimensions", "label": "五维分镜", "enabled": True, "severity": "block", "description": "每镜包含主体、环境、摄影机、时间状态和审美信息。"},
            {"id": "duration", "label": "时长与时间码", "enabled": True, "severity": "block", "description": "分段时长及 0.5 秒精度符合生产规格。"},
            {"id": "dialogue", "label": "台词与语速", "enabled": True, "severity": "block", "description": "台词逐字一致、自然拆分，并按情绪计算语速。"},
            {"id": "performance", "label": "表演空间", "enabled": True, "severity": "block", "description": "关键台词有反应镜，高潮有留白镜，动作独立成镜。"},
            {"id": "camera", "label": "镜头语言", "enabled": True, "severity": "block", "description": "角度、景别、机位和视觉钩子满足影视化规则。"},
            {"id": "people", "label": "人物数量", "enabled": True, "severity": "block", "description": "镜头人物与在场角色清单及数量锁一致。"},
            {"id": "text", "label": "画面文字", "enabled": True, "severity": "block", "description": "可读文字先由关键帧锁字，视频模型不得重写。"},
            {"id": "frames", "label": "首尾帧", "enabled": True, "severity": "block", "description": "首尾帧与前后镜头状态可无缝衔接。"},
            {"id": "audio", "label": "声音设计", "enabled": True, "severity": "block", "description": "环境声不为空，Seedance2 随视频人声与口型同步符合规格。"},
            {"id": "profile", "label": "生产规格", "enabled": True, "severity": "block", "description": "模型、分辨率、配音、口型与无字幕硬规则未漂移。"},
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
    "script_bible", "continuity", "spatial", "five_dimensions", "duration",
    "dialogue", "performance", "camera", "people", "text", "frames", "audio",
    "profile",
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
        for key in (
                "required_before_images", "auto_analyze_uploaded_script",
                "user_style_is_hard_constraint",
                "distinguish_world_from_render_medium",
                "editable_before_lock"):
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
            targets = asset_rules.get("candidate_targets")
            default_targets = defaults["candidate_targets"]
            if not isinstance(targets, dict):
                asset_rules["candidate_targets"] = copy.deepcopy(
                    default_targets)
                changed = True
            else:
                for key in ("non_main", "non_main_max", "background"):
                    if targets.get(key) != default_targets[key]:
                        targets[key] = default_targets[key]
                        changed = True
        story_defaults = DEFAULT_STANDARD["rules"]["story_analysis"]
        story_rules = rules.get("story_analysis")
        if not isinstance(story_rules, dict):
            rules["story_analysis"] = copy.deepcopy(story_defaults)
            changed = True
        else:
            for key, value in story_defaults.items():
                if key not in story_rules:
                    story_rules[key] = copy.deepcopy(value)
                    changed = True
        if not changed:
            return snapshot
        try:
            return self.save(
                content, change_note=(
                    "自动升级：加入剧本分析、剧情事实源、空间调度与角色分级资产规则"),
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
