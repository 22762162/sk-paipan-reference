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
            "type": "cli", "enabled": False,
            "capabilities": ["script", "storyboard"],
            "command": ["claude", "-p"],
            "cost_per_call": 0.5, "timeout": 600,
        },
        "codex": {
            "type": "cli", "enabled": False,
            "capabilities": ["image", "frames", "cover"],
            "command": ["codex", "exec", "--json"],
            "cost_per_call": 1.0, "timeout": 600,
        },
        "jimeng": {
            "type": "cli", "enabled": False,
            "capabilities": ["video", "voice"],
            "command": ["jimeng"],
            "cost_per_call": 2.0, "timeout": 1200,
            "quota": 1000,             # 订阅额度(次);耗尽后路由自动回退 API
        },
        "jianying": {
            "type": "cli", "enabled": False,
            "capabilities": ["edit"],
            "command": ["jianying-cli"],
            "cost_per_call": 0.5, "timeout": 1200,
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
    # 能力路由:按顺序尝试,前者不可用/失败自动回退后者
    "routing": {
        "script": ["claude", "mock"],
        "storyboard": ["claude", "mock"],
        "image": ["codex", "api", "mock"],
        "frames": ["codex", "mock"],
        "video": ["jimeng", "api", "mock"],
        "voice": ["jimeng", "api", "mock"],
        "edit": ["jianying", "mock"],
        "cover": ["codex", "mock"],
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
        return cls(data)

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
