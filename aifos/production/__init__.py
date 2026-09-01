"""AI 生产中心:Provider 体系与能力路由。

模型协同:Codex(图片/开发)、即梦 CLI(视频/配音,优先订阅额度)、
API(备用扩容)、剪映(自动剪辑);内置 Mock Provider 保证离线全流程可跑。
"""

from .base import Provider, ProviderResult
from .router import ProviderRouter

__all__ = ["Provider", "ProviderResult", "ProviderRouter"]
