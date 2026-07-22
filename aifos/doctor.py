"""自检与本机 CLI 自动接线。

aifos config detect --apply   找到本机的 claude/codex/dreamina 等 CLI,
                              自动写入配置并启用
aifos doctor [--ping]         体检:每个环节现在实际由谁生产
                              (真实产线 or 内置模拟),并给接入提示
"""

import os
import shutil
from pathlib import Path

from .settings import CAPABILITY_CN, PROVIDER_CN, update_provider

# 各 CLI 的常见安装位置(PATH 之外的兜底;按顺序找)
CLI_CANDIDATES = {
    "claude": ["~/.local/bin/claude", "~/.claude/local/claude",
               "/opt/homebrew/bin/claude", "/usr/local/bin/claude"],
    "codex": ["~/.local/node22/bin/codex", "~/.local/bin/codex",
              "/opt/homebrew/bin/codex", "/usr/local/bin/codex"],
    "dreamina": ["~/.local/bin/dreamina", "/opt/homebrew/bin/dreamina",
                 "/usr/local/bin/dreamina"],
}

# CLI → (provider 名, 生成 command 的方式)
_BRIDGES = {
    "claude": ("claude", lambda p: ["python3", "-m",
                                    "aifos.adapters.claude_script",
                                    "--claude", p]),
    "codex": ("codex", lambda p: ["python3", "-m",
                                  "aifos.adapters.codex_image",
                                  "--codex", p]),
    "dreamina": ("jimeng", lambda p: [p]),
}


def find_cli(name):
    """PATH 里找,找不到再翻常见安装位置;返回绝对路径或 None。"""
    path = shutil.which(name)
    if path:
        return str(Path(path).resolve())
    for candidate in CLI_CANDIDATES.get(name, []):
        p = Path(candidate).expanduser()
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None


def detect_clis():
    """→ {cli名: 绝对路径}(只含找到的)。"""
    return {name: path for name in CLI_CANDIDATES
            if (path := find_cli(name))}


def apply_detected(config_path, found):
    """把检测到的 CLI 写入配置并启用。→ [(provider, path), ...]"""
    applied = []
    for cli, path in found.items():
        provider, build = _BRIDGES[cli]
        update_provider(config_path, provider,
                        {"enabled": True, "command": build(path)})
        applied.append((provider, path))
    return applied


def _cli_binary(conf):
    """桥接命令(--claude/--codex 后面)或命令本体的真实可执行文件。"""
    command = conf.get("command") or []
    for flag in ("--claude", "--codex"):
        if flag in command:
            index = command.index(flag)
            if index + 1 < len(command):
                return command[index + 1]
    if "aifos.adapters.say_voice" in command:
        return "say"
    return command[0] if command else ""


def _provider_state(name, provider, do_ping):
    """→ (ok, detail)。ok=True 表示该 Provider 当前真的能干活。"""
    conf = provider.conf
    if conf.get("type") == "mock":
        return True, "内置模拟(占位产物,永远可用)"
    if not provider.enabled:
        return False, "未启用"
    if conf.get("type") in ("cli", "dreamina"):
        binary = _cli_binary(conf)
        if not binary:
            return False, "未配置 command"
        if shutil.which(binary) is None and not Path(binary).exists():
            return False, f"命令不存在: {binary}(装好后运行 " \
                          f"aifos config detect --apply)"
        return True, f"CLI 就绪: {binary}"
    ok, reason = provider.available(
        next(iter(provider.capabilities), ""))
    if not ok:
        return False, reason
    if do_ping and hasattr(provider, "ping"):
        try:
            ping_ok, detail = provider.ping()
        except Exception as exc:  # 体检自身不许崩
            ping_ok, detail = False, str(exc)
        return ping_ok, detail
    return True, "已配置(用 --ping 或设置页「测试连接」真实验证)"


_HINTS = {
    "script": "接 Claude:aifos config detect --apply(有 CLI)或 "
              "aifos config set --provider claude_api --enable --api-key …",
    "storyboard": "同「剧本」:接 Claude CLI 或 claude_api",
    "image": "批量图优先接 Seedream 5.0 Lite:aifos config set "
             "--provider seedream5_lite --enable --api-key …;"
             "或接 Codex CLI(detect) / image_api",
    "frames": "同「图片」:Seedream 5.0 Lite / image_api / Codex",
    "cover": "终稿封面优先 Codex，备用 image_api high",
    "video": "接即梦 CLI(detect)或火山方舟:aifos config set "
             "--provider ark --enable --api-key …",
    "voice": "接好即梦/Ark 后配音随视频自动生成(有声视频);"
             "或配豆包 TTS:aifos config set --provider doubao_tts "
             "--enable --appid … --api-key …",
    "edit": "pip3 install pyJianYingDraft 并在设置里启用「剪映草稿」:"
            "剪辑后草稿自动进剪映,打开剪映导出即可",
}


def run_doctor(app, do_ping=False):
    """全面体检 → dict(供 CLI 打印与 /api/doctor)。"""
    providers = {}
    for name, provider in app.router.providers.items():
        ok, detail = _provider_state(name, provider, do_ping)
        providers[name] = {
            "name": name, "label": PROVIDER_CN.get(name, name),
            "enabled": provider.enabled, "ok": ok, "detail": detail,
        }
    capabilities = []
    real = 0
    for capability, label in CAPABILITY_CN.items():
        chain = app.config.get("routing", capability, default=None) or []
        chosen, chosen_detail = "mock", "内置模拟"
        for name in chain:
            state = providers.get(name)
            if state and state["ok"] and capability in \
                    app.router.providers[name].capabilities:
                chosen, chosen_detail = name, state["detail"]
                break
        is_real = chosen != "mock"
        real += 1 if is_real else 0
        capabilities.append({
            "capability": capability, "label": label,
            "provider": chosen,
            "provider_label": PROVIDER_CN.get(chosen, chosen),
            "real": is_real, "detail": chosen_detail,
            "hint": None if is_real else _HINTS.get(capability, ""),
        })
    # 配音默认随 Seedance2 视频生成:视频产线可用且自带配音 → 配音视为已接
    voice = next((c for c in capabilities if c["capability"] == "voice"),
                 None)
    video = next((c for c in capabilities if c["capability"] == "video"),
                 None)
    if voice and not voice["real"] and video and video["real"]:
        video_provider = app.router.providers.get(video["provider"])
        if video_provider is not None and \
                video_provider.conf.get("audio_in_video"):
            real += 1
            voice.update({
                "real": True, "provider": video["provider"],
                "provider_label": "随视频自动配音(Seedance2 有声视频)",
                "detail": "配音在视频生成时自动完成,无需单独 TTS",
                "hint": None,
            })
    return {
        "providers": list(providers.values()),
        "capabilities": capabilities,
        "real_count": real,
        "total": len(capabilities),
        "pinged": do_ping,
    }
