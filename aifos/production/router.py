"""能力路由:按 routing 配置的优先级选择 Provider,自动降级回退。

- 即梦 CLI 优先使用订阅额度(quota 表计数),额度耗尽自动回退 API;
- Provider 未启用 / 命令缺失 / 执行失败 → 记录日志并回退下一个;
- 全部不可用 → ProviderUnavailable。
"""

import hashlib
import json
import re
import threading
import time

from ..errors import ProduceCancelled, ProviderError, ProviderUnavailable
from .api_providers import (ArkVideoProvider, ClaudeApiProvider,
                            DoubaoTtsProvider, OpenAIChatProvider,
                            OpenAIImageProvider, SeedreamImageProvider)
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
    "openai_chat": OpenAIChatProvider,
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
    PROMPT_REVIEW_SCHEMA = "aifos.codex-prompt-review/v1"

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
        # 通道取用优先级 = 配置列表顺序(只含启用通道):前面的通道并发
        # 占满后才溢出到后面的通道(如 B、C 先行,A 兜底)。
        self._codex_profile_order = []
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
                    if not (isinstance(profile, dict)
                            and profile.get("id")):
                        continue
                    profile_id = str(profile["id"])
                    self._codex_profile_slots.setdefault(
                        profile_id,
                        threading.BoundedSemaphore(
                            self._codex_parallel_per_channel))
                    if (bool(profile.get("enabled", False))
                            and profile_id
                            not in self._codex_profile_order):
                        self._codex_profile_order.append(profile_id)
            if provider.quota_limit > 0:
                self._ensure_quota_row(name, provider.quota_limit)

    # ---- 订阅额度 ----
    # ---- Codex 通道溢出调度 ----
    def _acquire_codex_slot(self, cancel=None):
        """按通道优先级抢一个并发槽:B、C 先行,占满才溢出到 A。

        非阻塞扫描全部启用通道;全占满则每 0.5s 重扫一轮(响应用户
        停止)。返回 (profile_id, slot);未配置启用通道时返回
        ("", None),调用方走无槽路径(保持旧配置兼容)。
        """
        order = self._codex_profile_order
        if not order:
            return "", None
        while True:
            for profile_id in order:
                slot = self._codex_profile_slots.get(profile_id)
                if slot is not None and slot.acquire(blocking=False):
                    return profile_id, slot
            if cancel is not None and cancel():
                raise ProduceCancelled("已手动停止(等待 Codex 通道空槽)")
            time.sleep(0.5)

    def _generate_via_codex_slot(self, provider, capability, payload,
                                 out_dir, cancel=None):
        """给 codex 调用套通道槽,并把实际使用的通道写回 payload——
        CliProvider 据此选择 CODEX_HOME,通道统计记到真实账号。"""
        profile_id, slot = self._acquire_codex_slot(cancel)
        if slot is None:
            return provider.generate(capability, payload, out_dir,
                                     cancel=cancel)
        payload["_codex_profile"] = profile_id
        try:
            return provider.generate(capability, payload, out_dir,
                                     cancel=cancel)
        finally:
            slot.release()

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
        required_provider = str(
            payload.get("required_provider") or "").strip()
        if strict_provider or required_provider:
            return [strict_provider or required_provider]
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

    @staticmethod
    def _stable_hash(value):
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _prompt_with_feedback(prompt, feedback):
        prompt = str(prompt or "").strip()
        feedback = str(feedback or "").strip()
        return (
            f"{prompt}。修改意见(必须落实):{feedback}"
            if feedback else prompt)

    def _real_image_provider_available(self, capability, payload):
        """是否存在能执行当前图片任务的真实 Provider。

        纯 mock 离线占位不消耗真实图片额度，也不冒充正式产物；只有真实
        图片会进入 Codex 提示词审核硬门禁。
        """
        for name in self._routing_chain(capability, payload):
            if name == "mock":
                continue
            provider = self.providers.get(name)
            if provider is None:
                continue
            ok, _reason = provider.available(capability)
            if ok:
                return True
        return False

    @staticmethod
    def _prompt_review_context(capability, payload):
        """只给 Codex 当前图片所需事实，避免整集背景反向污染提示词。"""
        explicit = payload.get("prompt_review_context")
        if isinstance(explicit, dict) and explicit:
            # 候选图等特殊任务已经把当前可执行事实编译成独立合同。
            # 此时不能再把 character_background 中互斥的全局服装、
            # 标志道具或其他剧情造型重新混入不可变审核上下文。
            context = {**dict(explicit), "capability": capability}
            for key in ("reference_manifest", "identity_references"):
                value = payload.get(key)
                if value not in (None, "", [], {}) and key not in context:
                    context[key] = value
            return context
        keys = (
            "title", "episode", "tagline",
            "art_name", "role", "shot_no", "scene_no", "frame_kind",
            "character_sheet", "sheet_label", "characters",
            "character_count", "functional_figures", "location", "action",
            "camera", "start_state", "end_state", "readable_text",
            "composition_contract", "prompt_contract",
            "reference_manifest", "identity_references",
            "character_background", "story_world", "story_background",
            "style", "aspect",
        )
        return {
            "capability": capability,
            **{
                key: payload.get(key) for key in keys
                if payload.get(key) not in (None, "", [], {})
            },
        }

    def _build_review_payload(self, source, context, payload):
        """审核请求 = 提示词 + 事实上下文 + 必留词 + 统一裁决条款。

        必留词:下游派发合同会逐字校验这些词,审核不知道就会把
        「旧靛青举人青袍」优化成「青袍」后卡死——先说清楚再校验。
        裁决条款:事实并列冲突时按治理优先级执行,不得再以
        「无优先级条款」为由整批熔断(rule_governance 是唯一事实源)。
        """
        from ..rule_governance import prompt_adjudication_clause
        required_tokens = self._prompt_review_required_tokens(source, payload)
        return {
            "review_schema": self.PROMPT_REVIEW_SCHEMA,
            "review_prompt": source,
            "review_context": context,
            "must_keep_verbatim": required_tokens,
            "must_keep_policy": (
                "must_keep_verbatim 中的每一项都是下游合同逐字校验的"
                "不可变事实,优化稿必须原样保留(可另加修饰,但不得改写、"
                "缩写、拆分或删除);删除任何一项都会导致整张图被拒绝生成"),
            "count_policy": (
                "人数是逐字校验的决定性事实:优化稿必须保留一句明确的"
                "人数字面表述——0人写「空镜/画面中不出现人物」,1人写"
                "「单人/1名人物」,N人写「共N人」;不得改写成只有具体"
                "人名或隐含表达,否则整张图被拒绝生成"),
            "adjudication": prompt_adjudication_clause(),
        }

    @staticmethod
    def _prompt_review_required_tokens(source, payload):
        tokens = []
        tokens.extend(
            str(name).strip() for name in payload.get("characters") or []
            if str(name).strip())
        for value in (
                payload.get("title"), payload.get("art_name"),
                payload.get("prop_name"), payload.get("location")):
            value = str(value or "").strip()
            # 标题、资产文件名等调度元数据不一定是画面事实。只有原始
            # 提示词明确写入时才要求优化稿逐字保留，避免把
            # ``林川_candidate_01`` 之类内部文件名塞进生图提示词。
            # prop_name 必须在列:下游派发合同逐字校验道具名,漏掉它
            # 会让优化稿把「旧靛青举人青袍」改写成「青袍」后卡死。
            if value and value in source:
                tokens.append(value)
        readable = payload.get("readable_text") or {}
        tokens.extend(
            str(value).strip() for value in readable.get("whitelist") or []
            if str(value).strip())
        # 下游派发合同逐字校验的结构标记:出现在原稿里就必须整块保留。
        # 「结构标题可由审核去掉」不适用于这些被合同点名的标记——
        # 删掉【质检同源合同】会直接判"未携带质检同源合同"并卡死。
        for marker in ("【质检同源合同】",):
            if marker in source:
                tokens.append(marker)
        # 服装/头饰是最容易被“优化”误删的镜头事实；若已有明确状态，
        # 优化稿必须继续逐字携带。结构标题和版本号属于审计元数据，可由
        # Codex去掉，真正的参考图职责仍由独立reference_manifest传输。
        for states in (
                payload.get("start_state"), payload.get("end_state")):
            if not isinstance(states, dict):
                continue
            for state in states.values():
                if not isinstance(state, dict):
                    continue
                for key in ("wardrobe", "headwear", "hair_makeup"):
                    value = state.get(key)
                    if isinstance(value, str) and value.strip():
                        tokens.append(value.strip())
        return list(dict.fromkeys(token for token in tokens if token))

    # 中文数字与阿拉伯数字都算数——审核优化稿写「两名人物」不等于
    # 丢了人数事实。
    _CN_NUM = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五",
               6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}

    @classmethod
    def _prompt_review_count_preserved(cls, optimized, payload):
        count = payload.get("character_count")
        if type(count) is not int or count < 0:
            return True
        functional = sum(
            item.get("count", 0)
            for item in payload.get("functional_figures") or []
            if isinstance(item, dict)
            and type(item.get("count")) is int
            and item.get("count") > 0)
        expected = count + functional
        if expected == 0:
            return any(token in optimized for token in (
                "无人", "空镜", "不出现人物", "无人物",
                "不出现任何人")) or bool(re.search(r"0\s*人", optimized))
        if expected == 1 and any(
                token in optimized for token in (
                    "单人", "一人", "一名人物", "一个人", "单一人物",
                    "单一角色", "同一角色", "一名角色", "单名人物")):
            return True
        numerals = str(expected)
        cn = cls._CN_NUM.get(expected)
        if cn:
            numerals = f"(?:{expected}|{cn})"
        return bool(re.search(
            rf"(?<!\d){numerals}\s*(?:人|名人物|个人|名角色|位人物|位角色)"
            r"(?!\d)", optimized))

    def review_image_prompt(self, capability, payload, out_dir, cancel=None):
        """用 Codex 审核并优化真实出图前的最终提示词，原地冻结优化稿。

        返回本次 Codex ProviderResult；若同一输入已经审核则返回 None。
        任何审核失败、事实丢失或结构损坏都失败关闭，不能继续生图。
        """
        if capability not in self.IMAGE_CAPABILITIES:
            return None
        source = self._prompt_with_feedback(
            payload.get("prompt_compact") or payload.get("prompt") or "",
            payload.get("feedback"))
        if not source:
            # 兼容仅测试Provider路由/额度的历史低层调用；正式AIFOS任务在
            # dispatch contract处仍会因“最终提示词为空”失败关闭。
            payload["prompt_review"] = {
                "schema": self.PROMPT_REVIEW_SCHEMA,
                "approved": False,
                "status": "not_applicable_legacy_empty_prompt",
                "blocking_reason": "低层兼容调用未提供AIFOS生图提示词",
            }
            return None
        context = self._prompt_review_context(capability, payload)
        input_hash = self._stable_hash({
            "prompt": source,
            "context": context,
            "schema": self.PROMPT_REVIEW_SCHEMA,
        })
        audit = payload.get("prompt_review") or {}
        if (audit.get("approved") is True
                and audit.get("reviewed_input_hash") == input_hash
                and str(payload.get("prompt_compact")
                        or payload.get("prompt") or "").strip()):
            return None
        if not self._real_image_provider_available(capability, payload):
            payload["prompt_review"] = {
                "schema": self.PROMPT_REVIEW_SCHEMA,
                "approved": False,
                "status": "not_applicable_mock_only",
                "input_hash": input_hash,
                "original_prompt": source,
                "optimized_prompt": source,
                "issues_found": [],
                "changes_made": [],
                "blocking_reason": (
                    "当前只有mock离线占位产线；该产物不属于正式图片"),
            }
            return None
        provider = self.providers.get("codex")
        if provider is None:
            raise ProviderUnavailable(
                "真实图片已被阻止：未配置Codex提示词审核产线")
        ok, reason = provider.available("prompt_review")
        if not ok:
            raise ProviderUnavailable(
                "真实图片已被阻止：Codex提示词审核不可用：" + reason)
        review_payload = self._build_review_payload(source, context, payload)
        profile_id = str(
            payload.get("_prompt_review_profile")
            or payload.get("_codex_profile") or "").strip()
        if profile_id:
            review_payload["_codex_profile"] = profile_id
        # 审核与出图共用同一套通道槽:B、C 先行,占满溢出到 A。
        result = self._generate_via_codex_slot(
            provider, "prompt_review", review_payload, out_dir,
            cancel=cancel)
        data = result.data or {}
        if data.get("schema") != self.PROMPT_REVIEW_SCHEMA:
            raise ProviderError(
                "Codex提示词审核返回schema不匹配："
                + str(data.get("schema") or "缺失"))
        if data.get("approved") is not True:
            reason = str(
                data.get("blocking_reason")
                or "Codex未批准该提示词进入图片生成")
            raise ProviderError("真实图片已被阻止：" + reason)
        optimized = str(data.get("optimized_prompt") or "").strip()
        if not optimized:
            raise ProviderError("Codex提示词审核通过但优化稿为空")
        if "```" in optimized:
            raise ProviderError("Codex优化稿含Markdown代码围栏，拒绝生图")
        missing = [
            token for token in self._prompt_review_required_tokens(
                source, payload)
            if token not in optimized
        ]
        if not self._prompt_review_count_preserved(optimized, payload):
            missing.append("人物总数")
        if missing:
            raise ProviderError(
                "Codex优化稿删除了不可变事实，拒绝生图："
                + "、".join(missing[:20]))
        audit_record = {
            "schema": self.PROMPT_REVIEW_SCHEMA,
            "approved": True,
            "status": "approved",
            "provider": result.provider,
            "model": result.model or "Codex 提示词审核优化",
            "input_hash": input_hash,
            "original_prompt": source,
            "optimized_prompt": optimized,
            "optimized_hash": self._stable_hash(optimized),
            "issues_found": list(data.get("issues_found") or []),
            "changes_made": list(data.get("changes_made") or []),
            "blocking_reason": "",
        }
        payload.setdefault("prompt_aifos_original", source)
        payload["prompt"] = optimized
        if "prompt_compact" in payload:
            payload["prompt_compact"] = optimized
        if payload.get("feedback"):
            payload["prompt_review_feedback_applied"] = payload["feedback"]
            payload["feedback"] = ""
        audit_record["reviewed_input_hash"] = self._stable_hash({
            "prompt": optimized,
            "context": context,
            "schema": self.PROMPT_REVIEW_SCHEMA,
        })
        payload["prompt_review"] = audit_record
        payload["prompt_review_schema"] = self.PROMPT_REVIEW_SCHEMA
        return result

    # ---- 调用 ----
    def call(self, capability, payload, out_dir, cancel=None):
        prompt_review_result = None
        if capability in self.IMAGE_CAPABILITIES:
            prompt_review_result = self.review_image_prompt(
                capability, payload, out_dir, cancel=cancel)
        strict_provider = str(payload.get("strict_provider") or "").strip()
        required_provider = str(
            payload.get("required_provider") or "").strip()
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
                if name == "codex":
                    # parallel_images 是每条 Codex 通道的容量,独立
                    # CODEX_HOME 隔离登录态。通道按配置列表序取用:
                    # 前面的通道(B、C)并发占满后才溢出到后面的(A),
                    # 实际用哪个通道由执行时空槽决定,不再静态轮询。
                    result = self._generate_via_codex_slot(
                        provider, capability, payload, out_dir,
                        cancel=cancel)
                else:
                    result = provider.generate(capability, payload, out_dir,
                                               cancel=cancel)
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
            if required_provider and result.provider != required_provider:
                raise ProviderUnavailable(
                    "指定分析产线与实际产线不一致: "
                    f"请求 {required_provider}，实际 {result.provider}")
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
            if prompt_review_result is not None:
                result.cost += float(prompt_review_result.cost or 0.0)
            if not isinstance(result.data, dict):
                result.data = {}
            result.data.setdefault(
                "prompt_review", payload.get("prompt_review") or {})
            result.data.setdefault(
                "prompt_aifos_original",
                payload.get("prompt_aifos_original") or "")
            result.data.setdefault(
                "prompt_optimized",
                payload.get("prompt_compact") or payload.get("prompt") or "")
            return result
        raise ProviderUnavailable(
            f"能力 {capability} 没有可用 Provider(链: {chain})")
