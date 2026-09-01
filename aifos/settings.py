"""设置中心:AI Provider 的 CLI/API 配置、能力路由与连通性测试。

所有修改写入 workspace/config.json,下次加载(CLI 每次运行 /
Web 每个请求)即生效;api_key 对外只回传掩码,掩码回传不覆盖原值。
"""

import json
import shlex
import shutil
from pathlib import Path

from .config import (CODEX_PROFILE_LIMIT, Config, normalize_codex_profile)
from .errors import AifosError

CAPABILITY_CN = {
    "prompt_review": "Codex提示词审核优化",
    "script": "剧本", "storyboard": "分镜", "image": "图片",
    "frames": "首尾帧", "video": "视频", "voice": "配音",
    "edit": "剪辑", "cover": "封面",
    "image_qc": "图片质检",
    "scene_annotate": "全景实测搭景",}

PROVIDER_CN = {
    "claude": "Claude CLI · 编剧",
    "claude_api": "Claude API · 编剧",
    "deepseek": "DeepSeek API · 编剧/分镜",
    "codex": "Codex CLI · 出图",
    "image_api": "出图 API · OpenAI 兼容",
    "seedream5_lite": "Seedream 5.0 Lite · 批量出图",
    "jimeng": "即梦 CLI · 视频(自带配音)",
    "ark": "火山方舟 API · 视频(Seedance2,自带配音)",
    "doubao_tts": "豆包 TTS · 配音备选",
    "say": "say 配音 · macOS(已弃用)",
    "jianying": "剪映草稿 · 剪辑(自动进剪映)",
    "api": "通用 API 备用",
    "mock": "内置模拟产线",
}

MODE_CN = {
    "cli": "CLI", "dreamina": "CLI",
    "api": "API", "claude_api": "API", "openai_chat": "API",
    "image_api": "API",
    "seedream_image": "API",
    "ark_video": "API", "doubao_tts": "API",
    "jianying_draft": "本机", "mock": "内置",
}

IMAGE_TASK_CLASSES = ("batch", "important", "final", "complex_text")
IMAGE_STRATEGIES = {
    "smart": {
        "batch": ["seedream5_lite", "image_api"],
        "important": ["codex", "image_api"],
        "final": ["codex", "image_api"],
        "complex_text": ["codex", "image_api"],
    },
    "codex": {
        key: ["codex", "image_api", "seedream5_lite"]
        for key in IMAGE_TASK_CLASSES
    },
    "seedream5_lite": {
        key: ["seedream5_lite", "image_api", "codex"]
        for key in IMAGE_TASK_CLASSES
    },
    "image_api": {
        key: ["image_api", "codex", "seedream5_lite"]
        for key in IMAGE_TASK_CLASSES
    },
}

# 允许经设置中心修改的字段(其余请直接编辑 workspace/config.json)
EDITABLE_FIELDS = {
    "enabled", "command", "endpoint", "api_key", "model", "model_version",
    "max_tokens", "video_resolution", "duration", "poll", "timeout",
    "cost_per_call", "quota", "appid", "cluster", "voice_type",
    "speed_ratio", "audio_in_video", "draft_dir", "codex_home",
    "thinking_mode",
}
_INT_FIELDS = {"max_tokens", "duration", "quota", "timeout"}
_FLOAT_FIELDS = {"cost_per_call", "poll"}
_BOOL_FIELDS = {"enabled", "audio_in_video"}


def mask_key(value):
    """API Key 掩码:只露末 4 位(短 key 全掩)。"""
    if not value:
        return ""
    return "****" + (value[-4:] if len(value) > 8 else "")


def is_masked(value):
    return isinstance(value, str) and value.startswith("****")


def _command_readiness(command):
    """只检查可执行文件是否可发现，不运行命令、不触碰认证数据。"""
    candidates = [command[0]] if command else []
    if "--codex" in command:
        index = command.index("--codex")
        if index + 1 < len(command):
            candidates.append(command[index + 1])
    for candidate in candidates:
        path = Path(candidate).expanduser()
        found = path.is_file() if "/" in candidate else bool(
            shutil.which(candidate))
        if not found:
            return False, f"命令不可用: {candidate}"
    return bool(candidates), "" if candidates else "未配置 command"


def _profile_settings_view(profile, runtime=None):
    """设置页只返回目录和命令，不读取 CODEX_HOME 下任何认证文件。"""
    runtime = runtime or {}
    home = profile["codex_home"]
    home_ready = bool(home) and Path(home).expanduser().is_dir()
    command_ready, command_reason = _command_readiness(profile["command"])
    if not profile["enabled"]:
        status = "disabled"
        reason = "未启用"
    elif not home_ready:
        status = "missing"
        reason = "CODEX_HOME 路径不存在" if home else "未配置 CODEX_HOME"
    elif not command_ready:
        status = "missing"
        reason = command_reason
    else:
        status = "ready"
        reason = "就绪"
    active_jobs = list(runtime.get("task_ids") or [])
    return {
        "id": profile["id"],
        "name": profile["name"],
        "codex_home": profile["codex_home"],
        "command": " ".join(profile["command"]),
        "enabled": profile["enabled"],
        "status": status,
        "reason": reason,
        "assigned": bool(active_jobs),
        "active_jobs": active_jobs,
        "runtime_state": runtime.get("state", "idle"),
        "parallel_limit": int(runtime.get("parallel_limit") or 0),
        "available_slots": int(runtime.get("available_slots") or 0),
    }


def codex_profiles_payload(config, status=None):
    """返回设置页可编辑的安全多 Codex 配置。"""
    status = status or config.codex_parallel_status()
    runtime_by_id = {
        profile["id"]: profile
        for profile in status.get("profiles", [])
    }
    return [
        _profile_settings_view(
            profile, runtime=runtime_by_id.get(profile["id"]))
        for profile in config.codex_profiles()
    ]


def settings_payload(app):
    """设置页视图:所有 Provider 的配置(key 掩码)+ 能力路由。"""
    providers = []
    for name, provider in app.router.providers.items():
        conf = provider.conf
        checks = []
        for cap in sorted(provider.capabilities):
            ok, reason = provider.available(cap)
            checks.append({"capability": cap, "ok": bool(ok),
                           "reason": reason or "就绪"})
        providers.append({
            "name": name,
            "label": PROVIDER_CN.get(name, name),
            "type": conf.get("type"),
            "mode": MODE_CN.get(conf.get("type"), conf.get("type")),
            "enabled": provider.enabled,
            "capabilities": sorted(provider.capabilities),
            "command": " ".join(conf.get("command") or []),
            "endpoint": conf.get("endpoint", ""),
            "model": conf.get("model", ""),
            "model_version": conf.get("model_version", ""),
            "thinking_mode": conf.get("thinking_mode", ""),
            "appid": conf.get("appid", ""),
            "voice_type": conf.get("voice_type", ""),
            "draft_dir": conf.get("draft_dir", ""),
            "codex_home": (
                conf.get("codex_home", "") if name == "codex" else ""),
            "api_key_masked": mask_key(conf.get("api_key", "")),
            "api_key_set": bool(conf.get("api_key")),
            "timeout": conf.get("timeout"),
            "cost_per_call": provider.cost_per_call,
            "checks": checks,
            "ready": bool(checks) and all(c["ok"] for c in checks),
        })
    image_routing = app.config.get("image_routing") or {}
    codex_status = app.config.codex_parallel_status()
    codex_profiles = codex_profiles_payload(
        app.config, status=codex_status)
    codex_status = dict(codex_status)
    codex_status["profiles"] = list(codex_profiles)
    try:
        candidate_rounds = int(app.config.get(
            "defaults", "shot_max_candidate_rounds", default=10))
    except (TypeError, ValueError):
        candidate_rounds = 10
    candidate_rounds = max(1, min(candidate_rounds, 10))
    return {
        "providers": providers,
        "codex_profiles": codex_profiles,
        "codex_parallel": codex_status,
        "routing": app.config.get("routing") or {},
        "image_routing": image_routing,
        "image_strategy": image_strategy_name(image_routing),
        "capabilities": CAPABILITY_CN,
        "defaults": {
            "parallel_images": app.config.get(
                "defaults", "parallel_images", default=3),
            "parallel_videos": app.config.get(
                "defaults", "parallel_videos", default=4),
            # 内容质检保留但不阻断；镜头每轮一张、总轮数（含首轮）回显。
            "selection_mode": _coerce_bool(
                "selection_mode", app.config.get(
                    "defaults", "selection_mode", default=True)),
            "image_content_qc": _coerce_bool(
                "image_content_qc", app.config.get(
                    "defaults", "image_content_qc", default=True)),
            "video_content_qc": _coerce_bool(
                "video_content_qc", app.config.get(
                    "defaults", "video_content_qc", default=True)),
            "shot_candidate_count": int(app.config.get(
                "defaults", "shot_candidate_count", default=1)),
            "shot_repair_candidate_count": 1,
            "shot_max_candidate_rounds": candidate_rounds,
            # 兼容值由正式总轮数推导，绝不与它相加。
            "shot_auto_repair_batches": candidate_rounds - 1,
            # 行为位只读回显：质检参与诊断/返修，但不得成为生产闸门。
            "content_qc_blocking": False,
            "content_qc_auto_retry": True,
        },
        "icloud_sync": app.icloud_sync.status(),
        "config_path": str(app.workspace.config_path),
    }


def _load_file(config_path):
    path = Path(config_path)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_file(config_path, data):
    Path(config_path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def set_codex_profiles(config_path, profiles):
    """安全保存 1-3 个 Codex profile，并同步首个到旧 providers.codex。

    profile 只接受 name/codex_home/command/enabled；认证继续由每个
    CODEX_HOME 自己的 auth.json 管理，本函数既不读取也不落盘其内容。
    """
    if not isinstance(profiles, list) or not profiles:
        raise AifosError("codex_profiles 必须包含至少一个配置")
    if len(profiles) > CODEX_PROFILE_LIMIT:
        raise AifosError(
            f"Codex profile 最多 {CODEX_PROFILE_LIMIT} 个")
    normalized = []
    ids = set()
    names = set()
    for index, profile in enumerate(profiles):
        try:
            clean = normalize_codex_profile(
                profile, index=index, strict=True)
        except ValueError as exc:
            raise AifosError(str(exc))
        if clean["id"] in ids:
            raise AifosError(f"Codex profile id 重复: {clean['id']}")
        if clean["name"] in names:
            raise AifosError(f"Codex profile name 重复: {clean['name']}")
        ids.add(clean["id"])
        names.add(clean["name"])
        normalized.append(clean)

    data = _load_file(config_path)
    parallel = data.setdefault("codex_parallel", {})
    parallel["max_parallel"] = min(
        CODEX_PROFILE_LIMIT, len(normalized))
    parallel["profiles"] = normalized

    # 旧 router 仍把 codex 视为单 Provider；镜像首个 profile 保证保存
    # 多配置后，未升级的调用路径继续使用第一路而不丢 enabled/command。
    # Router 仍只有一个 codex Provider；只要任一路开启就必须让 Provider
    # 可用，实际任务再由导演按 profile 分片。优先把首个已启用通道镜像
    # 到旧字段，兼容尚未支持多 profile 的调用路径。
    primary = next((profile for profile in normalized
                    if profile["enabled"]), normalized[0])
    legacy = data.setdefault("providers", {}).setdefault("codex", {})
    legacy.update({
        "enabled": any(profile["enabled"] for profile in normalized),
        "command": list(primary["command"]),
        "codex_home": primary["codex_home"],
    })
    _save_file(config_path, data)
    return [_profile_settings_view(profile) for profile in normalized]


def update_provider(config_path, name, fields):
    """把设置写入 config.json 的 providers.<name>,返回实际写入的字段。"""
    merged = Config.load(config_path)
    if name not in (merged.get("providers") or {}):
        raise AifosError(f"未知 Provider: {name}")
    clean = {}
    for key, value in (fields or {}).items():
        if key not in EDITABLE_FIELDS:
            raise AifosError(f"字段不可在设置中心修改: {key}")
        if value is None:
            continue
        if key == "api_key":
            if is_masked(value):
                continue           # 掩码原样回传 → 保持已存的 key
            clean[key] = str(value).strip()
        elif key in _BOOL_FIELDS:
            clean[key] = value if isinstance(value, bool) else \
                str(value).lower() in ("1", "true", "on", "yes")
        elif key == "command":
            clean[key] = (shlex.split(value) if isinstance(value, str)
                          else list(value))
        elif key in _INT_FIELDS:
            clean[key] = int(value)
        elif key in _FLOAT_FIELDS:
            clean[key] = float(value)
        elif key == "thinking_mode":
            mode = str(value).strip().lower()
            if mode not in ("enabled", "disabled"):
                raise AifosError(
                    "thinking_mode 只允许 enabled 或 disabled")
            clean[key] = mode
        else:
            clean[key] = str(value).strip()
    if "codex_home" in clean and name != "codex":
        raise AifosError("codex_home 只能用于 Codex Provider")
    if name == "codex" and clean:
        current = (merged.get("providers") or {}).get("codex") or {}
        profile_source = {
            "name": "codex",
            "codex_home": clean.get(
                "codex_home", current.get("codex_home", "")),
            "command": clean.get("command", current.get("command")),
            "enabled": clean.get("enabled", current.get("enabled", False)),
        }
        try:
            safe_profile = normalize_codex_profile(
                profile_source, strict=True)
        except ValueError as exc:
            raise AifosError(str(exc))
        for key in ("codex_home", "command", "enabled"):
            if key in clean:
                clean[key] = safe_profile[key]
    if not clean:
        return {}
    # 填了 Key 就是要用:未显式给 enabled 时自动启用,省一步开关
    if clean.get("api_key") and "enabled" not in clean:
        current = (merged.get("providers") or {}).get(name) or {}
        if not current.get("enabled"):
            clean["enabled"] = True
    data = _load_file(config_path)
    data.setdefault("providers", {}).setdefault(name, {}).update(clean)
    if name == "codex":
        saved_profiles = (
            (data.get("codex_parallel") or {}).get("profiles") or [])
        if saved_profiles:
            saved_profiles[0].update({
                key: clean[key]
                for key in ("codex_home", "command", "enabled")
                if key in clean
            })
    _save_file(config_path, data)
    return clean


def set_routing(config_path, capability, chain):
    """设置某能力的 Provider 调用顺序(逐个校验存在)。"""
    merged = Config.load(config_path)
    if capability not in (merged.get("routing") or {}):
        raise AifosError(f"未知能力: {capability}")
    if not chain:
        raise AifosError("路由链不能为空")
    known = set(merged.get("providers") or {})
    unknown = [n for n in chain if n not in known]
    if unknown:
        raise AifosError(f"未知 Provider: {', '.join(unknown)}")
    data = _load_file(config_path)
    data.setdefault("routing", {})[capability] = list(chain)
    _save_file(config_path, data)
    return list(chain)


def image_strategy_name(image_routing):
    """从实际分层路由反推快捷策略；不匹配预设时保留为高级自定义。"""
    first = {
        key: ((image_routing.get(key) or [None])[0])
        for key in IMAGE_TASK_CLASSES
    }
    if (first["batch"] == "seedream5_lite"
            and all(first[key] == "codex"
                    for key in IMAGE_TASK_CLASSES if key != "batch")):
        return "smart"
    values = set(first.values())
    if len(values) == 1:
        value = next(iter(values))
        if value in ("codex", "seedream5_lite", "image_api"):
            return value
    return "custom"


def set_image_strategy(config_path, strategy):
    """原子切换整套图片策略，分类图片与未分类旧调用同步生效。"""
    if strategy not in IMAGE_STRATEGIES:
        allowed = "、".join(IMAGE_STRATEGIES)
        raise AifosError(f"未知出图策略: {strategy}；只允许 {allowed}")
    merged = Config.load(config_path)
    known = set(merged.get("providers") or {})
    image_routing = {
        key: list(chain)
        for key, chain in IMAGE_STRATEGIES[strategy].items()
    }
    unknown = sorted({
        provider for chain in image_routing.values() for provider in chain
        if provider not in known
    })
    if unknown:
        raise AifosError(f"未知 Provider: {', '.join(unknown)}")

    if strategy == "smart":
        base = ["codex", "image_api", "api", "mock"]
    else:
        first = strategy
        base = [first] + [
            provider for provider in
            ("codex", "image_api", "seedream5_lite", "api", "mock")
            if provider != first
        ]
    base = [provider for provider in base if provider in known]
    data = _load_file(config_path)
    data["image_routing"] = image_routing
    routes = data.setdefault("routing", {})
    for capability in ("image", "frames", "cover"):
        routes[capability] = list(base)
    _save_file(config_path, data)
    return {"strategy": strategy, "image_routing": image_routing,
            "routing": {cap: list(base)
                        for cap in ("image", "frames", "cover")}}


def _coerce_bool(key, value):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "on", "yes", "开", "开启"):
        return True
    if text in ("0", "false", "off", "no", "关", "关闭"):
        return False
    raise AifosError(f"{key} 需为布尔值")


def set_defaults(config_path, mapping):
    """写入 defaults（并行度、非阻断质检、单图返修与总轮数预算）。"""
    int_keys = {"parallel_images", "parallel_videos"}
    bool_keys = {"selection_mode", "image_content_qc", "video_content_qc"}
    updates = {}
    for key, value in (mapping or {}).items():
        if key in int_keys:
            try:
                updates[key] = max(1, min(int(value), 8))
            except (TypeError, ValueError):
                raise AifosError(f"{key} 需为 1-8 的整数")
        elif key in bool_keys:
            updates[key] = _coerce_bool(key, value)
        elif key == "shot_candidate_count":
            try:
                count = int(value)
            except (TypeError, ValueError):
                raise AifosError("shot_candidate_count 需为整数")
            if isinstance(value, bool) or count != 1:
                raise AifosError(
                    "shot_candidate_count 当前版本固定为 1"
                    "(镜头关键帧首轮只生成1张)")
            updates[key] = 1
        elif key == "shot_repair_candidate_count":
            try:
                count = int(value)
            except (TypeError, ValueError):
                raise AifosError("shot_repair_candidate_count 需为整数")
            if isinstance(value, bool) or count != 1:
                raise AifosError(
                    "shot_repair_candidate_count 当前版本固定为 1"
                    "(问题镜头每个返修轮编辑后生成1张)")
            updates[key] = 1
        elif key == "shot_max_candidate_rounds":
            try:
                rounds = int(value)
            except (TypeError, ValueError):
                raise AifosError("shot_max_candidate_rounds 需为整数")
            if isinstance(value, bool) or not 1 <= rounds <= 10:
                raise AifosError(
                    "shot_max_candidate_rounds 需为 1-10"
                    "（首轮计入，总计最多10张）")
            updates[key] = rounds
        elif key == "shot_auto_repair_batches":
            try:
                batches = int(value)
            except (TypeError, ValueError):
                raise AifosError("shot_auto_repair_batches 需为整数")
            if isinstance(value, bool) or not 0 <= batches <= 9:
                raise AifosError(
                    "shot_auto_repair_batches 需为 0-9"
                    "(兼容字段；总轮数=返修批次+首轮)")
            updates[key] = batches
        else:
            raise AifosError(f"不支持的默认项: {key}")
    if not updates:
        raise AifosError("没有要保存的默认项")
    # 两个键是同一预算的两种表达，不能相加。正式总轮数优先；只收到
    # 旧字段时同步迁移为明确总轮数，避免旧客户端请求制造第11轮。
    if "shot_max_candidate_rounds" in updates:
        expected_batches = updates["shot_max_candidate_rounds"] - 1
        supplied_batches = updates.get("shot_auto_repair_batches")
        if supplied_batches is not None and supplied_batches != expected_batches:
            raise AifosError(
                "shot_max_candidate_rounds 与 shot_auto_repair_batches 冲突")
        updates["shot_auto_repair_batches"] = expected_batches
    elif "shot_auto_repair_batches" in updates:
        updates["shot_max_candidate_rounds"] = (
            updates["shot_auto_repair_batches"] + 1)
    data = _load_file(config_path)
    data.setdefault("defaults", {}).update(updates)
    _save_file(config_path, data)
    return updates


def set_icloud_sync(config_path, enabled):
    """启停固定在 iCloud Drive/AIFOS 下的手机图片镜像。"""
    if not isinstance(enabled, bool):
        enabled = str(enabled).strip().lower() in ("1", "true", "on", "yes")
    data = _load_file(config_path)
    data.setdefault("icloud_sync", {})["enabled"] = enabled
    _save_file(config_path, data)
    return {"enabled": enabled}


def test_provider(app, name):
    """连通性诊断:按“已启用”真实探测命令/密钥是否就绪。

    这样即使「启用」开关还没打开,也能告诉你命令(如 claude CLI)能不能用,
    而不是只回一句“未启用”——测通了再由前端顺手打开开关即可。诊断只临时
    改内存里的 enabled,函数结束立即还原,不写配置文件。
    """
    provider = app.router.providers.get(name)
    if provider is None:
        raise AifosError(f"未知 Provider: {name}")
    was_enabled = provider.enabled
    provider.enabled = True
    try:
        results = [{"capability": cap, "ok": bool(ok),
                    "reason": reason or "就绪"}
                   for cap in sorted(provider.capabilities)
                   for ok, reason in [provider.available(cap)]]
        ok_overall = all(r["ok"] for r in results)
        extra = None
        if ok_overall and hasattr(provider, "credit"):
            try:
                extra = f"实时余额: {provider.credit()}"
            except Exception as exc:
                extra = f"✗ 余额查询失败: {exc}"
                ok_overall = False
        if ok_overall and hasattr(provider, "ping"):
            try:
                ping_ok, detail = provider.ping()
            except Exception as exc:
                ping_ok, detail = False, str(exc)
            extra = ("✓ " if ping_ok else "✗ ") + detail
            ok_overall = ok_overall and ping_ok
    finally:
        provider.enabled = was_enabled
    return {"provider": name, "ok": ok_overall, "results": results,
            "extra": extra, "disabled": not was_enabled}
