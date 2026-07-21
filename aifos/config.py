"""系统中心·配置管理:默认配置 + workspace/config.json 覆盖 + 运行时覆盖。"""

import copy
import json
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
        "per_episode": 200.0,          # 单集成本预算(成本单位)
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
            "capabilities": ["script", "storyboard"],
            "command": ["python3", "-m", "aifos.adapters.claude_script",
                        "--claude", "claude"],
            "cost_per_call": 0.5, "timeout": 600,
        },
        "codex": {
            # 经 aifos.adapters.codex_image 桥接真实 codex exec;
            # 实机把 --codex 指到 codex 绝对路径后 enabled 置 true
            "type": "cli", "enabled": False,
            "capabilities": ["image", "frames", "cover"],
            "command": ["python3", "-m", "aifos.adapters.codex_image",
                        "--codex", "codex"],
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
            "capabilities": ["script", "storyboard"],
            "endpoint": "https://api.anthropic.com", "api_key": "",
            "model": "claude-opus-4-8", "max_tokens": 16000,
            "cost_per_call": 0.8, "timeout": 600,
        },
        "image_api": {
            # OpenAI 兼容出图 API:Codex 出图的 API 模式
            "type": "image_api", "enabled": False,
            "capabilities": ["image", "frames", "cover"],
            "endpoint": "https://api.openai.com", "api_key": "",
            "model": "gpt-image-2",
            "cost_per_call": 1.5, "timeout": 300,
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
            "capabilities": ["script", "storyboard", "image", "frames",
                             "video", "voice", "edit", "cover"],
            "cost_per_call": 0.1,
        },
    },
    # 能力路由:按顺序尝试,前者不可用/失败自动回退后者(CLI → API → mock)
    "routing": {
        "script": ["claude", "claude_api", "mock"],
        "storyboard": ["claude", "claude_api", "mock"],
        "image": ["codex", "image_api", "api", "mock"],
        "frames": ["codex", "image_api", "mock"],
        "video": ["jimeng", "ark", "api", "mock"],
        # 配音默认随 Seedance2 视频自动生成(有声视频),此链只在
        # 视频产线不带配音时才会用到;豆包 TTS 为首选备选
        "voice": ["doubao_tts", "api", "mock"],
        "edit": ["jianying", "mock"],
        "cover": ["codex", "image_api", "mock"],
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
        if path.exists():
            data = _deep_merge(data, json.loads(path.read_text(encoding="utf-8")))
        if overrides:
            data = _deep_merge(data, overrides)
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
