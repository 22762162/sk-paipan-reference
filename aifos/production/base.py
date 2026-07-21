"""Provider 抽象:统一的能力调用接口。

能力(capability)一览:
  script      剧本生成          storyboard  分镜生成
  image       镜头关键图        frames      首尾帧
  video       视频生成          voice       配音
  edit        剪辑合成          cover       封面
"""

from dataclasses import dataclass, field


@dataclass
class ProviderResult:
    provider: str
    cost: float
    data: dict = field(default_factory=dict)
    uri: str = ""
    # 路由回退轨迹:本次调用之前被跳过的真实产线及原因
    # [{"provider": "codex", "reason": "命令不存在: codex"}, ...]
    fallbacks: list = field(default_factory=list)


class Provider:
    def __init__(self, name, conf):
        self.name = name
        self.conf = conf
        self.capabilities = set(conf.get("capabilities", []))
        self.cost_per_call = float(conf.get("cost_per_call", 0.0))
        self.enabled = bool(conf.get("enabled", False))
        self.quota_limit = int(conf.get("quota", 0))

    def available(self, capability):
        """返回 (是否可用, 不可用原因)。"""
        if not self.enabled:
            return False, f"未启用(providers.{self.name}.enabled=false)"
        if capability not in self.capabilities:
            return False, f"不支持能力 {capability}"
        return True, ""

    def generate(self, capability, payload, out_dir, cancel=None):
        """cancel: 可选回调,返回 True 表示用户已请求停止,
        长调用(子进程/轮询)应尽快安全终止并抛 ProduceCancelled。"""
        raise NotImplementedError
