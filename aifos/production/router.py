"""能力路由:按 routing 配置的优先级选择 Provider,自动降级回退。

- 即梦 CLI 优先使用订阅额度(quota 表计数),额度耗尽自动回退 API;
- Provider 未启用 / 命令缺失 / 执行失败 → 记录日志并回退下一个;
- 全部不可用 → ProviderUnavailable。
"""

import threading

from ..errors import ProviderError, ProviderUnavailable
from .api_providers import (ArkVideoProvider, ClaudeApiProvider,
                            DoubaoTtsProvider, OpenAIImageProvider,
                            SeedreamImageProvider)
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
    "seedream_image": SeedreamImageProvider,
    "ark_video": ArkVideoProvider,
    "doubao_tts": DoubaoTtsProvider,
    "jianying_draft": JianyingDraftProvider,
    "mock": MockProvider,
}


class ProviderRouter:
    IMAGE_CAPABILITIES = {"image", "frames", "cover"}
    IMAGE_TASK_CLASSES = {"batch", "important", "final", "complex_text"}
    API_IMAGE_TYPES = {"image_api", "seedream_image"}

    def __init__(self, config, db, logger):
        self.config = config
        self.db = db
        self.log = logger
        self.providers = {}
        try:
            self._codex_parallel_per_channel = max(
                1, min(int(config.get(
                    "defaults", "parallel_images", default=3)), 8))
        except (TypeError, ValueError):
            self._codex_parallel_per_channel = 3
        self._codex_profile_slots = {}
        for name, conf in (config.get("providers") or {}).items():
            cls = PROVIDER_TYPES.get(conf.get("type"))
            if cls is None:
                logger.warn("router", f"未知 provider 类型: {name}")
                continue
            # profile 配置位于顶层，桥接 Provider 仍只接收自己的配置，
            # 这样旧工作区的 providers.codex 结构完全兼容。
            if name == "codex":
                conf = dict(conf)
                conf["codex_profiles"] = (
                    config.get("codex_profiles", default=None)
                    or config.get("codex_parallel", "profiles", default=[])
                    or conf.get("codex_profiles") or [])
            provider = cls(name, conf)
            self.providers[name] = provider
            if name == "codex":
                for profile in conf.get("codex_profiles") or []:
                    if isinstance(profile, dict) and profile.get("id"):
                        self._codex_profile_slots.setdefault(
                            str(profile["id"]),
                            threading.BoundedSemaphore(
                                self._codex_parallel_per_channel))
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

    def _routing_chain(self, capability, payload):
        """显式分类的图片任务使用成本分层；未分类的旧请求保持
        routing.<capability> 原顺序。分层链只调整已知首选，其余
        自定义 Provider 仍按用户配置作为回退。
        """
        strict_provider = str(payload.get("strict_provider") or "").strip()
        if strict_provider:
            return [strict_provider]
        base = list(self.config.get(
            "routing", capability, default=None) or ["mock"])
        if capability not in self.IMAGE_CAPABILITIES:
            return base
        task_class = payload.get("image_task_class")
        if task_class is None:
            return base
        if task_class not in self.IMAGE_TASK_CLASSES:
            allowed = "|".join(sorted(self.IMAGE_TASK_CLASSES))
            raise ProviderUnavailable(
                f"未知 image_task_class={task_class!r};只允许 {allowed}")
        preferred = list(self.config.get(
            "image_routing", task_class, default=None) or [])
        if task_class == "batch":
            # 普通批量先用两条按张计费 API；两者都未配置/
            # 失败时，Codex 订阅才作应急真实出图，避免直接落
            # mock 占位图。通用 api 不进批量链，免得绕过质量档闸。
            return list(dict.fromkeys(
                preferred
                + (["codex"] if "codex" in base else [])
                + (["mock"] if "mock" in base else [])))
        # 去重但保持顺序：分层首选 → 旧/用户 routing 回退。
        return list(dict.fromkeys(preferred + base))

    @staticmethod
    def _provider_models(provider):
        configured = provider.conf.get("models") or []
        values = []
        for item in configured:
            value = item.get("id") if isinstance(item, dict) else item
            if value and str(value) not in values:
                values.append(str(value))
        current = (provider.conf.get("model")
                   or getattr(provider, "DEFAULT_MODEL", ""))
        if current and str(current) not in values:
            values.insert(0, str(current))
        return values

    def image_api_options(self):
        """返回可供单批次严格选择的真实图片 API/模型。"""
        options = []
        for name, provider in self.providers.items():
            if provider.conf.get("type") not in self.API_IMAGE_TYPES:
                continue
            models = self._provider_models(provider)
            checks = []
            for capability in ("image", "frames"):
                ok, reason = provider.available(capability)
                checks.append({"capability": capability, "ok": bool(ok),
                               "reason": reason or "就绪"})
            ready = bool(models) and provider.reference_images \
                and any(item["ok"] for item in checks)
            reason = ""
            if not models:
                reason = "未配置模型"
            elif not provider.reference_images:
                reason = "不支持真实参考图输入"
            elif not ready:
                reason = next((item["reason"] for item in checks
                               if not item["ok"]), "API 未就绪")
            options.append({
                "provider": name,
                "type": provider.conf.get("type", ""),
                "models": models,
                "default_model": models[0] if models else "",
                "ready": ready,
                "reason": reason,
                "reference_images": bool(provider.reference_images),
                "cost_per_call": provider.cost_per_call,
                "checks": checks,
            })
        preferred = list(self.config.get(
            "image_routing", "batch", default=None) or [])
        rank = {name: index for index, name in enumerate(preferred)}
        options.sort(key=lambda item: (
            not item["ready"], rank.get(item["provider"], 999),
            item["provider"]))
        return options

    def validate_image_selection(self, provider_name, capability, payload,
                                 model):
        """只读校验所选 API/模型与真实 payload 契约是否可执行。"""
        provider = self.providers.get(provider_name)
        if provider is None:
            raise ProviderUnavailable(f"未知图片 API: {provider_name}")
        if provider.conf.get("type") not in self.API_IMAGE_TYPES:
            raise ProviderUnavailable(
                f"{provider_name} 不是可批量加速的图片 API")
        ok, reason = provider.available(capability)
        if not ok:
            raise ProviderUnavailable(f"{provider_name} 不可用: {reason}")
        if not provider.reference_images:
            raise ProviderUnavailable(
                f"{provider_name} 不支持真实参考图，禁止纯文字加速出图")
        models = self._provider_models(provider)
        if not model or model not in models:
            raise ProviderUnavailable(
                f"模型 {model or '未选择'} 与 {provider_name} 配置不匹配")
        validator = getattr(provider, "validate_request", None)
        if validator is not None:
            issues = validator(capability, payload, model=model) or []
            if issues:
                raise ProviderUnavailable("；".join(str(x) for x in issues))
        return {"provider": provider_name, "model": model,
                "capability": capability}

    # ---- 调用 ----
    def call(self, capability, payload, out_dir, cancel=None):
        strict_provider = str(payload.get("strict_provider") or "").strip()
        strict_model = str(payload.get("model_override") or "").strip()
        if strict_provider:
            self.validate_image_selection(
                strict_provider, capability, payload, strict_model)
        chain = self._routing_chain(capability, payload)
        fallbacks = []
        requires_refs = bool(payload.get("require_reference_images")
                             or payload.get("identity_required"))
        supplied_refs = []
        for key in ("style_ref", "scene_ref", "chain_first_uri"):
            if payload.get(key):
                supplied_refs.append(payload[key])
        for key in ("character_refs", "prop_refs", "reference_images"):
            supplied_refs.extend(payload.get(key) or [])
        supplied_refs.extend(
            ref.get("uri") for ref in (payload.get("identity_references") or [])
            if isinstance(ref, dict) and ref.get("uri"))
        if requires_refs and not supplied_refs:
            raise ProviderUnavailable(
                f"能力 {capability} 要求真实参考图，但请求未携带任何图片")
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
            if requires_refs and not provider.reference_images:
                reason = "不支持真实参考图输入，禁止退化为纯文字生图/质检"
                self.log.info(
                    "router", f"{name} {reason},回退({capability})")
                fallbacks.append({"provider": name, "reason": reason})
                continue
            # Paid image APIs may only execute an already-bound
            # prompt/reference contract. Initial identity exploration may
            # still use the Codex subscription without a reference, but
            # Seedream/OpenAI image APIs must never silently become text-only
            # generation.
            if (capability in self.IMAGE_CAPABILITIES
                    and provider.conf.get("type") in self.API_IMAGE_TYPES
                    and not supplied_refs):
                reason = "未携带参考图，禁止图片 API 纯文字生成"
                self.log.info(
                    "router", f"{name} {reason},回退({capability})")
                fallbacks.append({"provider": name, "reason": reason})
                continue
            try:
                profile_id = (str(payload.get("_codex_profile") or "").strip()
                              if name == "codex" else "")
                slots = self._codex_profile_slots.get(profile_id)
                if slots is None:
                    result = provider.generate(capability, payload, out_dir,
                                               cancel=cancel)
                else:
                    # parallel_images 是每条 Codex 通道的容量。独立
                    # CODEX_HOME 继续隔离登录态；单通道最多 8 路，
                    # A/B/C 三通道可合计 24 路。
                    with slots:
                        result = provider.generate(
                            capability, payload, out_dir, cancel=cancel)
            except ProviderError as exc:
                self.log.warn(
                    "router", f"{name} 执行失败({exc}),回退({capability})")
                fallbacks.append(
                    {"provider": name, "reason": f"执行失败: {exc}"})
                continue
            if strict_provider and (result.provider != strict_provider
                                    or result.model != strict_model):
                raise ProviderUnavailable(
                    "严格 API 加速实际产线/模型与预检不一致: "
                    f"请求 {strict_provider}/{strict_model}，"
                    f"实际 {result.provider}/{result.model or '未知'}")
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
