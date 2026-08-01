"""系统中心·配置管理:默认配置 + workspace/config.json 覆盖 + 运行时覆盖。"""

import copy
import json
import shlex
from pathlib import Path

# 模型协同(总体设计方案·四):
#   Claude Code —— 总控、工作流、Agent 调度(即 AI 导演中心本体,亦可作剧本 Provider)
#   Codex       —— 平台开发、图片资产、Prompt 优化、一致性
#   即梦 CLI    —— 默认视频生成与配音,优先使用订阅额度
#   API         —— 高并发备用与扩容
#   剪映        —— 自动剪辑
# 外部 Provider 默认 enabled=false:未接入真实 CLI/API 时自动回退到内置 mock,
# 保证完整生产流程离线可跑;接入后把对应 enabled 置 true 即切换为真实产线。
DEFAULTS = {
    "defaults": {
        "aspect": "9:16",              # 全局默认画幅(抖音竖屏);项目可设 16:9
        # Seedance 逐镜视频默认 4 路并行；可在设置页按账号限流调低/调高。
        "parallel_videos": 4,
    },
    # Codex 多实例不注册成多个 Provider，避免破坏现有 routing。旧工作区
    # 没有 profiles 时会自动把 providers.codex 映射成一个兼容 profile；
    # 设置中心保存后可配置三个各自独立的 CODEX_HOME。
    "codex_parallel": {
        "max_parallel": 3,
        "profiles": [
            {
                "id": "codex_a",
                "name": "Codex A",
                "codex_home": "~/.codex",
                "command": [
                    "python3", "-m", "aifos.adapters.codex_image",
                    "--codex", "codex",
                ],
                "enabled": False,
            },
            {
                "id": "codex_b",
                "name": "Codex B",
                "codex_home": "~/.codex-account-b",
                "command": [
                    "python3", "-m", "aifos.adapters.codex_image",
                    "--codex", "codex",
                ],
                "enabled": False,
            },
            {
                "id": "codex_c",
                "name": "Codex C",
                "codex_home": "~/.codex-account-c",
                "command": [
                    "python3", "-m", "aifos.adapters.codex_image",
                    "--codex", "codex",
                ],
                "enabled": False,
            },
        ],
    },
    # 本地 artifacts 是唯一事实源；图片完成并登记后异步复制到 iCloud，
    # 只供手机/Finder 查看。默认关闭，避免测试或其他 workspace 意外写云盘。
    "icloud_sync": {
        "enabled": False,
        "root": "~/Library/Mobile Documents/com~apple~CloudDocs/AIFOS",
    },
    # SK 漫剧工业流的项目级硬门槛。每集另存 production_profile，避免
    # workspace 配置改变后无法还原当时使用的生成规格。
    "production": {
        "pipeline_version": "sk-manju-v5",
        "preferred_segment_seconds": [5, 8],
        "max_segment_seconds": 15,
        "voice": "jimeng_builtin",
        "lip_sync": True,
        "burn_subtitles": False,
        "text_lock_provider": "ChatGPT关键帧",
    },
    "budget": {
        # 0 表示不限额。成本仍完整记账和展示，但不再因单集累计金额熔断。
        "per_episode": 0.0,
    },
    "retry": {
        "max_retries": 2,              # 质检不通过时的自动重跑轮数
    },
    "qc": {
        "pass_score": 80,              # 质检通过分数线(0-100)
        "max_subtitle_len": 30,        # 单条字幕最大长度
        "sensitive_words": ["血腥", "赌博", "毒品"],
    },
    "providers": {
        "claude": {
            # 经 aifos.adapters.claude_script 桥接 claude -p 实际编剧
            "type": "cli", "enabled": False,
            "capabilities": ["script", "storyboard", "image_qc"],
            "command": ["python3", "-m", "aifos.adapters.claude_script",
                        "--claude", "claude"],
            "reference_images": True,
            "cost_per_call": 0.5, "timeout": 600,
        },
        "codex": {
            # 经 aifos.adapters.codex_image 桥接真实 codex exec;
            # 实机把 --codex 指到 codex 绝对路径后 enabled 置 true
            "type": "cli", "enabled": False,
            "capabilities": [
                "prompt_review", "image", "frames", "cover", "image_qc",
            ],
            "command": ["python3", "-m", "aifos.adapters.codex_image",
                        "--codex", "codex"],
            "reference_images": True,
            "cost_per_call": 1.0, "timeout": 900,
        },
        "jimeng": {
            # 即梦官方 CLI(dreamina)原生适配
            "type": "dreamina", "enabled": False,
            "capabilities": ["video"],
            "command": ["dreamina"],
            "model_version": "seedance2.0fast_vip",  # 必须 fast_vip,勿用旧 seedance2.0_vip
            "video_resolution": "720p",
            "duration": 8,
            "poll": 30,
            "audio_in_video": True,    # Seedance2 有声视频:配音随视频,免单独 TTS
            "cost_per_call": 2.0, "timeout": 1800,
            "quota": 1000,             # 订阅额度(次);耗尽后路由自动回退 API
        },
        "jianying": {
            # 剪映真实草稿:剪辑结果自动进剪映草稿库(需 pyJianYingDraft),
            # 打开剪映微调后导出;剪映无官方 CLI/新版无自动导出
            "type": "jianying_draft", "enabled": False,
            "capabilities": ["edit"],
            "draft_dir": "",   # 留空自动找 ~/Movies/JianyingPro Drafts 等
            "cost_per_call": 0.3,
        },
        "claude_api": {
            # Claude 官方 API 直连(Messages API):Claude CLI 的 API 模式
            "type": "claude_api", "enabled": False,
            "capabilities": ["script", "storyboard", "image_qc",
                             "scene_annotate"],
            "endpoint": "https://api.anthropic.com", "api_key": "",
            "model": "claude-opus-4-8", "max_tokens": 16000,
            "reference_images": True,
            "cost_per_call": 0.8, "timeout": 600,
        },
        "deepseek": {
            # DeepSeek V4 Flash 官方 OpenAI 兼容接口。默认关闭；填入 Key
            # 后作为剧本/分镜备用通道，不改变 Claude/Codex 主路由。
            "type": "openai_chat", "enabled": False,
            "capabilities": ["script", "storyboard"],
            "endpoint": "https://api.deepseek.com", "api_key": "",
            "model": "deepseek-v4-flash",
            # 旧 deepseek-chat 是非思考模式；升级后显式保持同一语义，
            # 避免默认思考导致响应时间、成本和 JSON 行为突然变化。
            "thinking_mode": "disabled",
            "max_tokens": 32768,
            "cost_per_call": 0.05, "timeout": 1800,
        },
        "image_api": {
            # GPT Image 2 高质量备用。普通批量只用 medium,
            # high 留给终稿/复杂文字等已明确分类的任务。
            "type": "image_api", "enabled": False,
            "capabilities": ["image", "frames", "cover", "image_qc"],
            "endpoint": "https://api.openai.com", "api_key": "",
            "model": "gpt-image-2",
            "reference_images": True,
            "cost_per_call": 0.28,
            "cost_by_quality": {
                "low": 0.07, "lite": 0.07,
                "medium": 0.28, "high": 1.12,
            },
            "timeout": 300,
        },
        "seedream5_lite": {
            # 火山方舟 Seedream 5.0 Lite:普通批量图的低成本首选。
            # image 参数真实上传单/多张参考图；强制单图模式，
            # 防止提示词触发组图导致隐性成本。
            "type": "seedream_image", "enabled": False,
            "capabilities": ["image", "frames", "cover"],
            "endpoint": "https://ark.cn-beijing.volces.com",
            "api_key": "",
            "model": "doubao-seedream-5-0-lite-260128",
            "reference_images": True,
            "image_size": "2K",
            "watermark": False,
            "max_reference_images": 10,
            "cost_per_call": 0.22, "timeout": 300,
        },
        "ark": {
            # 火山方舟 Ark 视频 API:Seedance2 的 API 模式(即梦 CLI 备用)
            "type": "ark_video", "enabled": False,
            "capabilities": ["video"],
            "endpoint": "https://ark.cn-beijing.volces.com", "api_key": "",
            # 模型 ID 必须从方舟控制台复制(形如 doubao-seedance-2-0-…
            # 带日期后缀,或推理接入点 ep-…);留空时设置页会明确提示
            "model": "",
            "video_resolution": "720p", "duration": 8, "poll": 5,
            "audio_in_video": True,    # Seedance2 有声视频:配音随视频生成
            "cost_per_call": 2.5, "timeout": 1800,
        },
        "doubao_tts": {
            # 豆包(火山引擎)语音合成:视频产线不带配音时的 TTS 备选
            "type": "doubao_tts", "enabled": False,
            "capabilities": ["voice"],
            "endpoint": "https://openspeech.bytedance.com/api/v1/tts",
            "appid": "", "api_key": "",     # api_key = access token
            "cluster": "volcano_tts",
            "voice_type": "BV700_streaming",
            "cost_per_call": 0.2, "timeout": 60,
        },
        "api": {
            "type": "api", "enabled": False,
            "capabilities": ["image", "video", "voice"],
            "endpoint": "", "api_key": "",
            "cost_per_call": 3.0, "timeout": 300,
        },
        "mock": {
            "type": "mock", "enabled": True,
            "capabilities": ["script", "storyboard", "image_qc", "image", "frames",
                             "video", "voice", "edit", "cover"],
            "reference_images": True,
            "cost_per_call": 0.1,
        },
    },
    # 能力路由:按顺序尝试,前者不可用/失败自动回退后者(CLI → API → mock)
    "routing": {
        # 所有真实图片在进入出图 Provider 前必须先由 Codex 审核并优化
        # 最终提示词；该能力失败时真实图片任务失败关闭，不回退 mock。
        "prompt_review": ["codex"],
        "script": ["claude", "claude_api", "deepseek", "mock"],
        "image_qc": ["codex", "image_api", "claude", "claude_api", "mock"],
        "storyboard": ["claude", "claude_api", "deepseek", "mock"],
        # 全景图实测标注必须走支持图片附件的真实视觉 API；
        # 必须真实携带图片，禁止回退文本 CLI / mock 猜测三维坐标，
        # 避免伪造家具坐标污染后续搭景。
        "scene_annotate": ["claude_api"],
        "image": ["codex", "image_api", "api", "mock"],
        "frames": ["codex", "image_api", "mock"],
        "video": ["jimeng", "ark", "api", "mock"],
        # 配音默认随 Seedance2 视频自动生成(有声视频),此链只在
        # 视频产线不带配音时才会用到;豆包 TTS 为首选备选
        "voice": ["doubao_tts", "api", "mock"],
        "edit": ["jianying", "mock"],
        "cover": ["codex", "image_api", "mock"],
    },
    # 图片任务的动态成本路由。只当 payload 显式携带
    # image_task_class 时生效；旧工作区/自定义 routing 仍保留为后续回退链。
    "image_routing": {
        "batch": ["seedream5_lite", "image_api"],
        "important": ["codex", "image_api"],
        "final": ["codex", "image_api"],
        "complex_text": ["codex", "image_api"],
    },
}


def _deep_merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


_DEEPSEEK_OFFICIAL_ENDPOINTS = frozenset({
    "https://api.deepseek.com",
    "https://api.deepseek.com/v1",
    "https://api.deepseek.com/chat/completions",
    "https://api.deepseek.com/v1/chat/completions",
})


def is_official_deepseek_endpoint(value):
    """识别官方根地址、兼容 base URL 与完整 ChatCompletions 地址。"""
    return str(value or "").strip().lower().rstrip("/") in \
        _DEEPSEEK_OFFICIAL_ENDPOINTS


def _migrate_saved_deepseek(data):
    """只迁移磁盘旧配置；调用方显式 overrides 始终保持最高优先级。"""
    providers = data.get("providers") or {}
    deepseek = providers.get("deepseek")
    if not (isinstance(deepseek, dict)
            and deepseek.get("type") == "openai_chat"
            and is_official_deepseek_endpoint(deepseek.get("endpoint"))):
        return data
    legacy_model = str(deepseek.get("model") or "").strip()
    legacy_modes = {
        "deepseek-chat": "disabled",
        "deepseek-reasoner": "enabled",
    }
    if legacy_model not in legacy_modes:
        return data
    deepseek["model"] = "deepseek-v4-flash"
    deepseek["thinking_mode"] = legacy_modes[legacy_model]
    if deepseek.get("max_tokens") in (None, 8192):
        deepseek["max_tokens"] = 32768
    return data


def _strip_inherited_custom_deepseek_thinking(
        data, saved=None, overrides=None):
    """自定义 OpenAI 网关未显式配置时不发送 DeepSeek 专用字段。"""
    deepseek = (data.get("providers") or {}).get("deepseek")
    if not isinstance(deepseek, dict) or \
            is_official_deepseek_endpoint(deepseek.get("endpoint")):
        return data
    layers = (saved or {}, overrides or {})
    explicit = any(
        "thinking_mode" in (
            (layer.get("providers") or {}).get("deepseek") or {})
        for layer in layers if isinstance(layer, dict)
    )
    if not explicit:
        deepseek.pop("thinking_mode", None)
    return data


CODEX_PROFILE_LIMIT = 3
CODEX_PROFILE_FIELDS = ("id", "name", "codex_home", "command", "enabled")
_CODEX_SECRET_FIELDS = {
    "api_key", "apikey", "auth", "auth_json", "credentials", "password",
    "secret", "token",
}
_CODEX_SECRET_FLAGS = {
    "--api-key", "--api_key", "--auth", "--auth-token", "--password",
    "--secret", "--token",
}
_CODEX_SECRET_ASSIGNMENTS = (
    "API_KEY=", "AUTH_TOKEN=", "PASSWORD=", "SECRET=", "TOKEN=",
)


def _profile_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def _safe_codex_home(value, strict=False):
    """仅保留 CODEX_HOME 目录路径；绝不读取或接受 auth 文件内容。"""
    home = str(value or "").strip()
    lowered_name = Path(home).name.lower() if home else ""
    unsafe = (
        any(char in home for char in ("\x00", "\r", "\n"))
        or lowered_name in {
            "auth.json", "credentials.json", "token.json", "secrets.json",
        }
    )
    if unsafe:
        if strict:
            raise ValueError("codex_home 必须是目录路径，不能指向认证文件")
        return ""
    return home


def _safe_codex_command(value, strict=False):
    """规范命令并剔除可能内嵌的认证参数。

    Codex 认证只能经 CODEX_HOME 隔离，不能把 token/auth 参数写进
    workspace/config.json 或回传到设置页。
    """
    if isinstance(value, str):
        try:
            parts = shlex.split(value)
        except ValueError:
            if strict:
                raise ValueError("Codex command 不是有效的 shell 命令")
            parts = []
    elif isinstance(value, (list, tuple)):
        parts = [str(part) for part in value]
    elif value in (None, ""):
        parts = []
    else:
        if strict:
            raise ValueError("Codex command 必须是字符串或字符串列表")
        parts = []

    safe = []
    index = 0
    while index < len(parts):
        part = parts[index]
        lowered = part.lower()
        secret_flag = lowered in _CODEX_SECRET_FLAGS
        secret_inline = any(
            lowered.startswith(flag + "=") for flag in _CODEX_SECRET_FLAGS)
        secret_assignment = any(
            assignment.lower() in lowered
            for assignment in _CODEX_SECRET_ASSIGNMENTS)
        auth_file = "auth.json" in lowered
        if secret_flag or secret_inline or secret_assignment or auth_file:
            if strict:
                raise ValueError(
                    "Codex command 不得包含 token/auth；请使用 codex_home")
            index += 2 if secret_flag and index + 1 < len(parts) else 1
            continue
        safe.append(part)
        index += 1
    return safe


def normalize_codex_profile(profile, index=0, strict=False):
    """把一个 Codex profile 规范为可安全对外返回/调度的稳定字段。"""
    if not isinstance(profile, dict):
        if strict:
            raise ValueError("Codex profile 必须是对象")
        profile = {}
    if strict:
        unknown = set(profile) - set(CODEX_PROFILE_FIELDS)
        secret = {
            key for key in unknown
            if str(key).strip().lower() in _CODEX_SECRET_FIELDS
            or any(word in str(key).strip().lower()
                   for word in ("auth", "token", "secret", "password"))
        }
        if secret:
            raise ValueError("Codex profile 不保存认证字段")
        if unknown:
            raise ValueError(
                "Codex profile 不支持字段: "
                + ", ".join(sorted(str(key) for key in unknown)))

    channel = chr(ord("a") + min(max(int(index), 0), 25))
    default_id = f"codex_{channel}"
    profile_id = str(profile.get("id") or default_id).strip()
    if (not profile_id or len(profile_id) > 64
            or any(char in profile_id for char in ("\x00", "\r", "\n"))):
        if strict:
            raise ValueError("Codex profile id 必须是 1-64 个可见字符")
        profile_id = default_id
    name = str(profile.get("name") or f"Codex {channel.upper()}").strip()
    if (not name or len(name) > 64
            or any(char in name for char in ("\x00", "\r", "\n"))):
        if strict:
            raise ValueError("Codex profile name 必须是 1-64 个可见字符")
        name = "codex" if index == 0 else f"codex-{index + 1}"
    command = _safe_codex_command(profile.get("command"), strict=strict)
    if not command:
        command = copy.deepcopy(DEFAULTS["providers"]["codex"]["command"])
    return {
        "id": profile_id,
        "name": name,
        "codex_home": _safe_codex_home(
            profile.get("codex_home", ""), strict=strict),
        "command": command,
        "enabled": _profile_bool(profile.get("enabled", False)),
    }


def get_codex_profiles(config):
    """纯函数：读取安全 Codex profiles；旧单 Provider 自动兼容为一个。"""
    data = getattr(config, "data", config)
    data = data if isinstance(data, dict) else {}
    parallel = data.get("codex_parallel")
    raw_profiles = (
        parallel.get("profiles")
        if isinstance(parallel, dict) else None
    )
    if not isinstance(raw_profiles, list) or not raw_profiles:
        provider = (data.get("providers") or {}).get("codex") or {}
        raw_profiles = copy.deepcopy(
            DEFAULTS["codex_parallel"]["profiles"])
        raw_profiles[0].update({
            "codex_home": provider.get(
                "codex_home", raw_profiles[0]["codex_home"]),
            "command": provider.get("command", raw_profiles[0]["command"]),
            "enabled": provider.get("enabled", False),
        })

    profiles = []
    seen_ids = set()
    seen_names = set()
    for index, raw in enumerate(raw_profiles[:CODEX_PROFILE_LIMIT]):
        profile = normalize_codex_profile(raw, index=index)
        if (profile["id"] in seen_ids or profile["name"] in seen_names):
            continue
        seen_ids.add(profile["id"])
        seen_names.add(profile["name"])
        profiles.append(profile)
    return profiles


def _assignment_profile(value):
    if isinstance(value, dict):
        value = (
            value.get("profile_id") or value.get("id")
            or value.get("profile") or value.get("profile_name")
        )
    return str(value or "").strip()


def codex_parallel_status(
        profiles, assignments=None, max_parallel=CODEX_PROFILE_LIMIT,
        parallel_per_channel=8):
    """汇总 Codex 通道及槽位状态；每条通道可容纳多项并行任务。"""
    normalized = [
        normalize_codex_profile(profile, index=index)
        for index, profile in enumerate(list(profiles or [])
                                      [:CODEX_PROFILE_LIMIT])
    ]
    try:
        parallel_limit = max(1, min(int(max_parallel), CODEX_PROFILE_LIMIT))
    except (TypeError, ValueError):
        parallel_limit = CODEX_PROFILE_LIMIT
    try:
        per_channel_limit = max(1, min(int(parallel_per_channel), 8))
    except (TypeError, ValueError):
        per_channel_limit = 8
    eligible = [
        profile["id"] for profile in normalized if profile["enabled"]
    ][:parallel_limit]
    active = {profile_id: [] for profile_id in eligible}
    id_by_alias = {
        alias: profile["id"]
        for profile in normalized
        for alias in (profile["id"], profile["name"])
    }
    clean_assignments = {}
    for task_id, value in (assignments or {}).items():
        profile_id = id_by_alias.get(_assignment_profile(value))
        if profile_id not in active:
            continue
        task_key = str(task_id)
        active[profile_id].append(task_key)
        clean_assignments[task_key] = profile_id

    profile_status = []
    for profile in normalized:
        item = copy.deepcopy(profile)
        tasks = active.get(profile["id"], [])
        if not profile["enabled"]:
            state = "disabled"
        elif profile["id"] not in eligible:
            state = "standby"
        else:
            state = "busy" if tasks else "idle"
        item.update({
            "state": state,
            "task_ids": tasks,
            "active_count": len(tasks),
            "parallel_limit": (
                per_channel_limit if profile["id"] in eligible else 0),
            "available_slots": (
                max(0, per_channel_limit - len(tasks))
                if profile["id"] in eligible else 0),
        })
        profile_status.append(item)
    busy_count = sum(1 for tasks in active.values() if tasks)
    total_parallel = per_channel_limit * len(eligible)
    active_count = sum(len(tasks) for tasks in active.values())
    return {
        "max_parallel": parallel_limit,
        "parallel_per_channel": per_channel_limit,
        "total_parallel": total_parallel,
        "configured_count": len(normalized),
        "enabled_count": len(eligible),
        "active_count": active_count,
        "busy_profile_count": busy_count,
        "available_count": sum(
            1 for tasks in active.values()
            if len(tasks) < per_channel_limit),
        "available_slots": max(0, total_parallel - active_count),
        "profiles": profile_status,
        "assignments": clean_assignments,
    }


def assign_codex_task(
        profiles, assignments, task_id, max_parallel=CODEX_PROFILE_LIMIT,
        parallel_per_channel=8):
    """幂等地把任务分给负载最低且仍有槽位的 profile。"""
    task_key = str(task_id or "").strip()
    if not task_key:
        raise ValueError("task_id 不能为空")
    current = {
        str(key): _assignment_profile(value)
        for key, value in (assignments or {}).items()
    }
    status = codex_parallel_status(
        profiles, assignments=current, max_parallel=max_parallel,
        parallel_per_channel=parallel_per_channel)
    existing = status["assignments"].get(task_key)
    if existing:
        selected = next(
            profile for profile in status["profiles"]
            if profile["id"] == existing)
        return {
            "assigned": True,
            "reason": "already_assigned",
            "task_id": task_key,
            "profile_id": existing,
            "profile_name": selected["name"],
            "profile": selected,
            "assignments": current,
        }
    candidates = [
        profile for profile in status["profiles"]
        if profile["state"] in {"idle", "busy"}
        and profile.get("available_slots", 0) > 0
    ]
    selected = min(
        candidates,
        key=lambda profile: (
            int(profile.get("active_count") or 0),
            str(profile.get("id") or "")),
        default=None)
    if selected is None:
        return {
            "assigned": False,
            "reason": (
                "no_enabled_profile"
                if status["enabled_count"] == 0 else "all_busy"),
            "task_id": task_key,
            "profile_id": None,
            "profile_name": None,
            "profile": None,
            "assignments": current,
        }
    updated = dict(current)
    updated[task_key] = selected["id"]
    return {
        "assigned": True,
        "reason": "assigned",
        "task_id": task_key,
        "profile_id": selected["id"],
        "profile_name": selected["name"],
        "profile": selected,
        "assignments": updated,
    }


def _normalize_legacy(data):
    """兼容旧工作区：移除平台曾内置的低质量 macOS say 过渡产线。

    覆盖两代遗留形态:更早的内置类型(type="say")与后来的桥接命令
    (aifos.adapters.say_voice);用户手工注册的其他同名自定义
    Provider 不动,避免升级时吞掉自定义能力。清除后同步剔除所有
    路由链里的 say 引用,否则 router 每次请求都会告警刷日志。
    """
    providers = data.get("providers") or {}
    legacy_say = providers.get("say")
    remove = False
    if isinstance(legacy_say, dict):
        command = legacy_say.get("command") or []
        command_parts = command if isinstance(command, list) else [command]
        command_text = " ".join(str(part) for part in command_parts)
        builtin_signature = (
            command_text == "python3 -m aifos.adapters.say_voice "
                            "--voice Tingting"
            and legacy_say.get("cost_per_call") == 0.0
            and legacy_say.get("timeout") == 120)
        # type="say" 是平台最早的内置类型,从未开放自定义,放心移除;
        # 桥接命令形态只清除与内置完全一致的签名(自定义 --say 保留)
        remove = legacy_say.get("type") == "say" or builtin_signature
    for name in ("claude", "claude_api"):
        conf = providers.get(name)
        caps = conf.get("capabilities") if isinstance(conf, dict) else None
        if isinstance(caps, list) and "script" in caps \
                and "image_qc" not in caps:
            caps.append("image_qc")
        # 旧工作区可能把 scene_annotate 留在只收文本的 Claude CLI，
        # 或因保存过 capabilities 而让 Claude API 缺少这项能力。
        # 场景几何必须由能真实携带全景图的视觉 API 测量。
        if name == "claude" and isinstance(caps, list):
            caps[:] = [cap for cap in caps if cap != "scene_annotate"]
        if name == "claude_api" and isinstance(caps, list) \
                and "scene_annotate" not in caps:
            caps.append("scene_annotate")
    for name in ("codex", "image_api"):
        conf = providers.get(name)
        caps = conf.get("capabilities") if isinstance(conf, dict) else None
        if isinstance(caps, list) and "image" in caps \
                and "image_qc" not in caps:
            caps.append("image_qc")
        if name == "codex" and isinstance(caps, list) \
                and "image" in caps and "prompt_review" not in caps:
            caps.append("prompt_review")
    routing = data.setdefault("routing", {})
    if "prompt_review" not in routing:
        routing["prompt_review"] = ["codex"]
    # 这不是普通可降级能力：场景坐标若回退到只收文本的 CLI 或 mock，
    # 后续所有机位都会建立在伪几何上。旧工作区即使保存过危险链也收紧。
    routing["scene_annotate"] = ["claude_api"]
    mock_conf = providers.get("mock")
    mock_caps = (mock_conf.get("capabilities")
                 if isinstance(mock_conf, dict) else None)
    if isinstance(mock_caps, list) and "image_qc" not in mock_caps:
        mock_caps.append("image_qc")
    if isinstance(mock_caps, list):
        mock_caps[:] = [
            cap for cap in mock_caps if cap != "scene_annotate"]
    if not remove:
        return data
    providers.pop("say", None)
    routing = data.get("routing") or {}
    if routing.get("voice") == ["jimeng", "say", "api", "mock"]:
        routing["voice"] = copy.deepcopy(DEFAULTS["routing"]["voice"])
    for capability, chain in list(routing.items()):
        if isinstance(chain, list) and "say" in chain:
            cleaned = [name for name in chain if name != "say"]
            routing[capability] = cleaned or copy.deepcopy(
                DEFAULTS["routing"].get(capability, ["mock"]))
    return data


class Config:
    def __init__(self, data):
        self.data = data

    @classmethod
    def load(cls, config_path, overrides=None):
        data = copy.deepcopy(DEFAULTS)
        path = Path(config_path)
        saved = {}
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            data = _deep_merge(data, saved)
            data = _migrate_saved_deepseek(data)
        if overrides:
            data = _deep_merge(data, overrides)
        data = _strip_inherited_custom_deepseek_thinking(
            data, saved=saved, overrides=overrides)
        # 旧工作区只有 providers.codex：第一路继承原命令/开关，
        # 第二路保持安全关闭并使用约定的备用 CODEX_HOME。
        explicit_profiles = (
            (saved.get("codex_parallel") or {}).get("profiles")
            if isinstance(saved.get("codex_parallel"), dict) else None
        )
        override_profiles = (
            (overrides.get("codex_parallel") or {}).get("profiles")
            if isinstance(overrides, dict)
            and isinstance(overrides.get("codex_parallel"), dict)
            else None
        )
        if not explicit_profiles and not override_profiles:
            provider = (data.get("providers") or {}).get("codex") or {}
            profiles = copy.deepcopy(
                DEFAULTS["codex_parallel"]["profiles"])
            profiles[0].update({
                "codex_home": provider.get(
                    "codex_home", profiles[0]["codex_home"]),
                "command": provider.get(
                    "command", profiles[0]["command"]),
                "enabled": provider.get("enabled", False),
            })
            data.setdefault("codex_parallel", {})["profiles"] = profiles
        return cls(_normalize_legacy(data))

    @staticmethod
    def write_default(config_path):
        """初始化 workspace 时落盘一份可编辑的默认配置(已存在则不覆盖)。"""
        path = Path(config_path)
        if not path.exists():
            path.write_text(
                json.dumps(DEFAULTS, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    def get(self, *path, default=None):
        node = self.data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def codex_profiles(self):
        """返回安全的 Codex 调度配置（最多三个）。"""
        return get_codex_profiles(self.data)

    def codex_parallel_status(self, assignments=None):
        """按当前配置汇总 Codex 并行槽状态。"""
        return codex_parallel_status(
            self.codex_profiles(),
            assignments=assignments,
            max_parallel=self.get(
                "codex_parallel", "max_parallel",
                default=CODEX_PROFILE_LIMIT),
            parallel_per_channel=self.get(
                "defaults", "parallel_images", default=3),
        )

    def assign_codex_task(self, task_id, assignments=None):
        """按当前配置为任务选择 Codex profile。"""
        return assign_codex_task(
            self.codex_profiles(),
            assignments=assignments,
            task_id=task_id,
            max_parallel=self.get(
                "codex_parallel", "max_parallel",
                default=CODEX_PROFILE_LIMIT),
            parallel_per_channel=self.get(
                "defaults", "parallel_images", default=3),
        )
