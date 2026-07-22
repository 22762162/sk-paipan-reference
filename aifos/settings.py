"""设置中心:AI Provider 的 CLI/API 配置、能力路由与连通性测试。

所有修改写入 workspace/config.json,下次加载(CLI 每次运行 /
Web 每个请求)即生效;api_key 对外只回传掩码,掩码回传不覆盖原值。
"""

import json
import shlex
from pathlib import Path

from .config import Config
from .errors import AifosError

CAPABILITY_CN = {
    "script": "剧本", "storyboard": "分镜", "image": "图片",
    "frames": "首尾帧", "video": "视频", "voice": "配音",
    "edit": "剪辑", "cover": "封面",
    "image_qc": "图片质检",}

PROVIDER_CN = {
    "claude": "Claude CLI · 编剧",
    "claude_api": "Claude API · 编剧",
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
    "api": "API", "claude_api": "API", "image_api": "API",
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
    "speed_ratio", "audio_in_video", "draft_dir",
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
            "appid": conf.get("appid", ""),
            "voice_type": conf.get("voice_type", ""),
            "draft_dir": conf.get("draft_dir", ""),
            "api_key_masked": mask_key(conf.get("api_key", "")),
            "api_key_set": bool(conf.get("api_key")),
            "timeout": conf.get("timeout"),
            "cost_per_call": provider.cost_per_call,
            "checks": checks,
            "ready": bool(checks) and all(c["ok"] for c in checks),
        })
    image_routing = app.config.get("image_routing") or {}
    return {
        "providers": providers,
        "routing": app.config.get("routing") or {},
        "image_routing": image_routing,
        "image_strategy": image_strategy_name(image_routing),
        "capabilities": CAPABILITY_CN,
        "defaults": {
            "parallel_images": app.config.get(
                "defaults", "parallel_images", default=3),
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
        else:
            clean[key] = str(value).strip()
    if not clean:
        return {}
    # 填了 Key 就是要用:未显式给 enabled 时自动启用,省一步开关
    if clean.get("api_key") and "enabled" not in clean:
        current = (merged.get("providers") or {}).get(name) or {}
        if not current.get("enabled"):
            clean["enabled"] = True
    data = _load_file(config_path)
    data.setdefault("providers", {}).setdefault(name, {}).update(clean)
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


def set_defaults(config_path, mapping):
    """写入 defaults 配置(如 parallel_images 并行出图路数)。"""
    allowed = {"parallel_images"}
    updates = {}
    for key, value in (mapping or {}).items():
        if key not in allowed:
            raise AifosError(f"不支持的默认项: {key}")
        try:
            updates[key] = max(1, min(int(value), 8))
        except (TypeError, ValueError):
            raise AifosError(f"{key} 需为 1-8 的整数")
    if not updates:
        raise AifosError("没有要保存的默认项")
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
