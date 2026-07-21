"""能力路由:按 routing 配置的优先级选择 Provider,自动降级回退。

- 即梦 CLI 优先使用订阅额度(quota 表计数),额度耗尽自动回退 API;
- Provider 未启用 / 命令缺失 / 执行失败 → 记录日志并回退下一个;
- 全部不可用 → ProviderUnavailable。
"""

from ..errors import ProviderError, ProviderUnavailable
from .api_providers import (ArkVideoProvider, ClaudeApiProvider,
                            DoubaoTtsProvider, OpenAIImageProvider)
from .base import ProviderResult  # noqa: F401  (re-export for callers)
from .dreamina import DreaminaProvider
from .external import ApiProvider, CliProvider
from .jianying_draft import JianyingDraftProvider
from .mock import MockProvider

PROVIDER_TYPES = {
    "cli": CliProvider,
    "api": ApiProvider,
    "dreamina": DreaminaProvider,
    "claude_api": ClaudeApiProvider,
    "image_api": OpenAIImageProvider,
    "ark_video": ArkVideoProvider,
    "doubao_tts": DoubaoTtsProvider,
    "jianying_draft": JianyingDraftProvider,
    "mock": MockProvider,
}


class ProviderRouter:
    def __init__(self, config, db, logger):
        self.config = config
        self.db = db
        self.log = logger
        self.providers = {}
        for name, conf in (config.get("providers") or {}).items():
            cls = PROVIDER_TYPES.get(conf.get("type"))
            if cls is None:
                logger.warn("router", f"未知 provider 类型: {name}")
                continue
            provider = cls(name, conf)
            self.providers[name] = provider
            if provider.quota_limit > 0:
                self._ensure_quota_row(name, provider.quota_limit)

    # ---- 订阅额度 ----
    def _ensure_quota_row(self, name, limit):
        row = self.db.query_one(
            "SELECT * FROM quota WHERE provider=?", (name,))
        if row is None:
            self.db.execute(
                "INSERT INTO quota(provider, used, quota_limit) VALUES(?,0,?)",
                (name, limit))
        elif row["quota_limit"] != limit:
            self.db.execute(
                "UPDATE quota SET quota_limit=? WHERE provider=?",
                (limit, name))

    def quota_remaining(self, name):
        row = self.db.query_one(
            "SELECT * FROM quota WHERE provider=?", (name,))
        if row is None:
            return None  # 无额度限制
        return row["quota_limit"] - row["used"]

    def _consume_quota(self, name):
        self.db.execute(
            "UPDATE quota SET used = used + 1 WHERE provider=?", (name,))

    # ---- 调用 ----
    def call(self, capability, payload, out_dir, cancel=None):
        chain = self.config.get("routing", capability, default=None) or ["mock"]
        fallbacks = []
        for name in chain:
            provider = self.providers.get(name)
            if provider is None:
                self.log.warn("router", f"routing 引用了未定义 provider: {name}")
                fallbacks.append({"provider": name, "reason": "未定义"})
                continue
            if provider.quota_limit > 0:
                remaining = self.quota_remaining(name)
                if remaining is not None and remaining <= 0:
                    self.log.warn(
                        "router",
                        f"{name} 订阅额度已耗尽,回退下一 Provider({capability})")
                    fallbacks.append(
                        {"provider": name, "reason": "订阅额度已耗尽"})
                    continue
            ok, reason = provider.available(capability)
            if not ok:
                self.log.info(
                    "router", f"{name} 不可用({reason}),回退({capability})")
                fallbacks.append({"provider": name, "reason": reason})
                continue
            try:
                result = provider.generate(capability, payload, out_dir,
                                           cancel=cancel)
            except ProviderError as exc:
                self.log.warn(
                    "router", f"{name} 执行失败({exc}),回退({capability})")
                fallbacks.append(
                    {"provider": name, "reason": f"执行失败: {exc}"})
                continue
            if provider.quota_limit > 0:
                self._consume_quota(name)
            result.fallbacks = fallbacks
            if name == "mock" and fallbacks:
                self.log.warn(
                    "router",
                    f"⚠️ {capability} 已回退到内置占位产线(mock),"
                    "产出为示意内容而非真实 AI 生成;原因: "
                    + ";".join(f"{f['provider']}({f['reason']})"
                               for f in fallbacks))
            return result
        raise ProviderUnavailable(
            f"能力 {capability} 没有可用 Provider(链: {chain})")
