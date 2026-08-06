"""能力路由:按 routing 配置的优先级选择 Provider,自动降级回退。

- 即梦 CLI 优先使用订阅额度(quota 表计数),额度耗尽自动回退 API;
- Provider 未启用 / 命令缺失 / 执行失败 → 记录日志并回退下一个;
- 全部不可用 → ProviderUnavailable。
"""

import hashlib
import json
import re
import struct
import threading
import time
from pathlib import Path

from .. import knowledge_apply
from ..errors import ProduceCancelled, ProviderError, ProviderUnavailable
from ..prompt_contract import (sanitize_scene_prompt_value,
                               scene_prompt_contaminants)
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

    def __init__(self, config, db, logger, knowledge=None):
        self.config = config
        self.db = db
        self.log = logger
        # 知识大脑(可选):写作类能力在派发前挂上已激活知识,见
        # knowledge_apply。传 None 时行为与接线前完全一致。
        self.knowledge = knowledge
        self.providers = {}
        try:
            self._codex_parallel_per_channel = max(
                1, min(int(config.get(
                    "defaults", "parallel_images", default=3)), 8))
        except (TypeError, ValueError):
            self._codex_parallel_per_channel = 3
        self._codex_profile_slots = {}
        # 本次运行内因题材安全策略拒收的 (provider, capability):
        # 悬疑/凶案类剧集会被某些图片 API 整集拒绝,记一次即绕开。
        self._safety_blocked = set()
        # 本服务进程内已经证实“能力根本不存在”的 Provider/能力组合。
        # 例如 Codex 子会话没有内置 image_gen：继续给同批每张图都启动
        # 一次几十秒的必败进程没有任何抽卡价值，只会延迟后续真实 API。
        # 这里仅熔断确定性的能力缺失；普通单图失败仍照常逐张回退。
        self._capability_blocked = set()
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
    def _is_deterministic_capability_failure(name, capability, error):
        if name != "codex" or capability not in \
                ProviderRouter.IMAGE_CAPABILITIES:
            return False
        text = str(error or "").lower()
        signals = (
            "built-in image_gen capability is unavailable",
            "no image_gen",
            "image_gen 图像生成能力",
            "未提供可调用的内置 `image_gen`",
            "未提供内置 `image_gen`",
            "缺少内置 image_gen",
            "没有图像生成能力",
        )
        return any(signal.lower() in text for signal in signals)

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

    @staticmethod
    def _image_dimensions(uri):
        """Read PNG/JPEG dimensions without adding a Pillow dependency.

        Some image APIs return JPEG bytes while the requested destination
        still has a .png suffix, so detection must use the file signature.
        """
        path = Path(str(uri or ""))
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if (len(data) >= 24
                and data[:8] == b"\x89PNG\r\n\x1a\n"
                and data[12:16] == b"IHDR"):
            return struct.unpack(">II", data[16:24])
        if len(data) < 4 or data[:2] != b"\xff\xd8":
            return None
        offset = 2
        sof_markers = {
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        }
        while offset + 4 <= len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in (0x01, *range(0xD0, 0xDA)):
                continue
            if offset + 2 > len(data):
                break
            length = struct.unpack(">H", data[offset:offset + 2])[0]
            if length < 2 or offset + length > len(data):
                break
            if marker in sof_markers and length >= 7:
                height, width = struct.unpack(
                    ">HH", data[offset + 3:offset + 7])
                return width, height
            offset += length
        return None

    @classmethod
    def _validate_generated_image_shape(cls, capability, payload, result):
        """Fail a provider result when a panorama silently becomes a square.

        The 720° scene master is the geometry source for all later camera
        slices. Accepting a 1:1 image here makes every derived direction a
        different room, so a bad result must re-enter the normal provider
        fallback chain instead of being registered as a usable panorama.
        """
        if (capability not in cls.IMAGE_CAPABILITIES
                or str(payload.get("aspect") or "") != "2:1"):
            return
        # Mock artifacts are deliberately non-production SVG placeholders,
        # sometimes stored behind a .png path for legacy tests. They never
        # become a formal scene geometry source.
        if getattr(result, "provider", "") == "mock":
            return
        dimensions = cls._image_dimensions(getattr(result, "uri", ""))
        if dimensions is None:
            raise ProviderError(
                f"{getattr(result, 'provider', '图片产线')} 的 2:1 全景产物"
                "无法读取 PNG/JPEG 尺寸，拒绝登记为全景母版")
        width, height = dimensions
        if height <= 0 or width != height * 2:
            raise ProviderError(
                f"{getattr(result, 'provider', '图片产线')} 的全景产物尺寸"
                f"为 {width}x{height}，不是严格 2:1；"
                "拒绝登记并回退下一图片产线")

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
        requires_refs = bool(payload.get("require_reference_images")
                             or payload.get("identity_required"))
        allows_text_bootstrap = bool(
            payload.get("allow_text_to_image_bootstrap"))
        supplied_refs = self._supplied_reference_uris(payload)
        for name in self._routing_chain(capability, payload):
            if name == "mock":
                continue
            provider = self.providers.get(name)
            if provider is None:
                continue
            ok, _reason = provider.available(capability)
            if not ok:
                continue
            if requires_refs and not provider.reference_images:
                continue
            if (provider.conf.get("type") in self.API_IMAGE_TYPES
                    and not supplied_refs and not allows_text_bootstrap):
                continue
            return True
        return False

    @staticmethod
    def _supplied_reference_uris(payload):
        """Return the concrete reference inputs used by routing gates."""
        supplied = []
        for key in ("style_ref", "scene_ref", "chain_first_uri",
                    "image_uri"):
            if payload.get(key):
                supplied.append(payload[key])
        for key in ("character_refs", "prop_refs", "reference_images"):
            supplied.extend(payload.get(key) or [])
        supplied.extend(
            ref.get("uri")
            for ref in (payload.get("identity_references") or [])
            if isinstance(ref, dict) and ref.get("uri"))
        return supplied

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
        if payload.get("prompt_contract_complete"):
            # The compact prompt is the executable projection.  Raw action,
            # start/end state, shot_contract and character biography remain
            # useful audit data, but an LLM reviewer must not see them: old
            # episode fields once reintroduced a half-hung gauze curtain into
            # a compact contract that had correctly removed it.  Pass only
            # provider-visible, already-sanitized contract fields.
            contract = payload.get("prompt_contract") or {}
            contract = contract if isinstance(contract, dict) else {}
            executable_contract = {
                key: contract.get(key)
                for key in (
                    "schema", "output", "frame_target", "subject",
                    "population", "scene", "scene_layout", "style",
                    "style_direction", "start", "action", "performance",
                    "camera", "lighting", "spatial_staging", "end",
                    "physical", "frame_props", "readable_text_current",
                    "text_rule", "reference_roles",
                )
                if contract.get(key) not in (None, "", [], {})
            }
            return {
                "capability": capability,
                "shot_no": payload.get("shot_no"),
                "frame_kind": payload.get("frame_kind"),
                "characters": list(payload.get("characters") or []),
                "character_count": payload.get("character_count"),
                "functional_figures": payload.get(
                    "functional_figures") or [],
                "visible_figure_count": payload.get(
                    "visible_figure_count"),
                "location": contract.get("scene")
                or payload.get("location"),
                "era_context": payload.get("era_context"),
                "sanctioned_anachronisms": payload.get(
                    "sanctioned_anachronisms") or [],
                "prompt_contract": executable_contract,
                "reference_manifest": payload.get(
                    "reference_manifest") or [],
            }
        keys = (
            "title", "episode", "tagline",
            "art_name", "role", "shot_no", "scene_no", "frame_kind",
            "character_sheet", "sheet_label", "characters",
            "character_count", "functional_figures", "location", "action",
            "camera", "camera_precedence", "aspect_precedence",
            "shot_contract", "prompt_conflict_resolution",
            "start_state", "end_state",
            "readable_text",
            "composition_contract", "prompt_contract",
            "reference_manifest", "identity_references",
            "character_background", "story_world", "story_background",
            "style", "aspect",
        )
        context = {
            "capability": capability,
            **{
                key: payload.get(key) for key in keys
                if payload.get(key) not in (None, "", [], {})
            },
        }
        try:
            repair_round = int(
                payload.get("_codex_contract_repair_count") or 0)
        except (TypeError, ValueError):
            repair_round = 0
        if repair_round:
            # A repaired shot may still carry stale clauses inside a previously
            # compiled prompt (real case: the latest camera says 135mm close-up
            # while an old model-constraint line still says medium shot).
            # Make the latest structured shot state an explicit adjudication,
            # so prompt review deletes the obsolete clause instead of blocking
            # the autonomous run as an alleged same-level conflict.
            context["master_state_precedence"] = {
                "policy": (
                    "这是第N轮后的最新镜头合同。latest_camera、"
                    "latest_shot_contract、latest_action 与"
                    "prompt_conflict_resolution 共同替代并作废"
                    "AIFOS原始提示词、旧模型约束或旧审核稿中一切冲突的"
                    "景别、焦段、机位、构图、运镜、视线、裁切和道具终态；"
                    "审核必须按最新值生成优化稿，不得把已作废旧句当作"
                    "同级冲突再次阻断。人物身份、性别、人数、服装、"
                    "关键道具唯一性、场景和文字白名单仍不得改变。"),
                "revision_round": repair_round,
                "latest_camera": payload.get("camera"),
                "latest_shot_contract": payload.get("shot_contract") or {},
                "latest_action": payload.get("action"),
                "prompt_conflict_resolution": payload.get(
                    "prompt_conflict_resolution") or {},
            }
        return context

    @staticmethod
    def _scene_execution_allowed_text(payload):
        """Locked scene inventory and current props that may name exceptions."""
        values = [str(payload.get("scene_layout") or "")]
        values.extend(
            str(value or "")
            for value in payload.get("sanctioned_anachronisms") or [])
        used_ids = {
            str(item.get("prop_id") or "").strip()
            for item in payload.get("frame_props") or []
            if isinstance(item, dict) and item.get("prop_id")
        }
        values.extend(
            str(item.get("name") or "")
            for item in payload.get("frame_props") or []
            if isinstance(item, dict))
        values.extend(
            str(item.get("name") or "")
            for item in payload.get("prop_registry") or []
            if isinstance(item, dict)
            and str(item.get("prop_id") or "").strip() in used_ids)
        return " ".join(value for value in values if value.strip())

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
                "缩写、拆分或删除);删除任何一项都会导致整张图被拒绝生成。"
                "繁简字形差异不构成冲突也不算删除:提示词叙述与资产名"
                "保持简体原样,画面内文字执行 readable_text 白名单的字形"
                "(如史实繁体),同一词的两种字形视为同一事实,无需裁决"),
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
        # 排除性约束(「严禁/不得出现X」)是审词最爱删的一类:它读起来
        # 像冗余负面词,删掉却直接放走画面事实。《长夏记事》纱幕后人
        # 实案——原稿写明"严禁用面纱/帷帽/兜帽等实体遮蔽物实现面部
        # 不可见",优化稿整段丢弃,四张候选全把纱披到人物头上。
        # 只收句子级排除条款,不收零散否定词,避免把整篇写死。
        for match in re.finditer(
                r"(?:严禁|绝不|不得|禁止)[^。；\n]{6,80}", source):
            clause = match.group(0).strip()
            # Repair feedback can say “不得同时执行被作废的旧条款：删除
            # ‘景别锁定为近景’…”. This is governance metadata, not a visual
            # exclusion. Requiring it verbatim re-inserts the obsolete camera
            # phrase into the provider prompt and creates two scale contracts.
            if clause and not any(marker in clause for marker in (
                    "被作废的旧条款", "已作废的旧条款",
                    "Codex 通知 AIFOS", "Codex通知 AIFOS")):
                tokens.append(clause)
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
        # 「7人」「7名」「7位」都是明确总数;「严格共7名可见真人」实测
        # 曾因只认「名人物/名角色」后缀被误判成删除人数。
        if re.search(
                rf"(?<!\d){numerals}\s*(?:人|名|位|个)(?!\d)", optimized):
            return True
        # 分组求和:「3名登记角色…加4名巡检弓兵」= 7 同样是明确的人数
        # 字面;各组数量相加等于期望值即算保留。文本同时写总数与分组
        # (「共7名:3名…加4名」)时求和会翻倍,总数组+分组各占一半同样
        # 算保留,不得误判为改动人数。
        cn_to_int = {v: k for k, v in cls._CN_NUM.items()}
        groups = [
            int(g) if g.isdigit() else cn_to_int.get(g, 0)
            for g in re.findall(
                r"(?<!\d)(\d+|[一两三四五六七八九十])\s*[名位个](?!\d)",
                optimized)]
        total = sum(groups)
        return bool(groups) and (
            total == expected
            or (expected in groups and total == expected * 2))

    @staticmethod
    def _reviewed_prompt_execution_conflicts(prompt):
        """Return deterministic conflicts that must block the image API."""
        text = str(prompt or "")
        issues = []
        if any(marker in text for marker in (
                "被作废的旧条款", "已作废的旧条款")):
            issues.append("仍包含已作废旧合同的审计元话术")
        scales = set(re.findall(
            r"景别锁定为(大特写|中近景|特写|近景|中景|大远景|全景|远景)",
            text))
        if len(scales) > 1:
            issues.append("同时锁定多个景别:" + "、".join(sorted(scales)))
        if (any(token in text for token in ("固定机位", "机位固定"))
                and any(token in text for token in (
                    "摄影机短跟", "镜头短跟", "侧面跟拍", "摄影机跟拍"))):
            issues.append("固定机位与正向跟拍要求同时存在")
        return issues

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
        scene_allowed_text = self._scene_execution_allowed_text(payload)
        if payload.get("prompt_contract_complete"):
            source = sanitize_scene_prompt_value(
                source, payload.get("location") or "", payload,
                allowed_text=scene_allowed_text)
            # All later branches, including director-autonomy and mock-only
            # execution, must dispatch this clean projection.  Previously the
            # sanitized local variable was only used by Codex review while
            # the early-return autonomy path left the polluted payload intact.
            payload["prompt"] = source
            if "prompt_compact" in payload:
                payload["prompt_compact"] = source
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
        if payload.get("director_autonomy_mode"):
            # 用户明确授权 AI 导演使用当前冻结提示词直接生成。记录豁免
            # 事实供审计，但不调用 Codex、不改写原稿，也不伪造 approved。
            payload["prompt_review"] = {
                "schema": self.PROMPT_REVIEW_SCHEMA,
                "approved": False,
                "status": "not_applicable_director_autonomy",
                "original_prompt": source,
                "optimized_prompt": source,
                "issues_found": [],
                "changes_made": [],
                "blocking_reason": (
                    "用户授权AI导演使用冻结提示词直接生成，"
                    "不执行提示词复审"),
            }
            return None
        if payload.get("prompt_review_exempt"):
            # 资产工坊(用户自建资产):没有剧本、人数、道具等下游逐字合同
            # 要守,用户自己写的提示词就是唯一事实源。此处只登记豁免事实,
            # 不进入剧集产线的 Codex 提示词审核硬门禁,也不改写用户原稿。
            payload["prompt_review"] = {
                "schema": self.PROMPT_REVIEW_SCHEMA,
                "approved": False,
                "status": "not_applicable_user_studio",
                "original_prompt": source,
                "optimized_prompt": source,
                "issues_found": [],
                "changes_made": [],
                "blocking_reason": (
                    "用户自建资产按原提示词执行;"
                    "该产物不参与剧集提示词审核门禁"),
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
        # Current facts have already been integrated by Codex. Do not submit
        # an audit sentence that quotes a superseded visual clause: even a
        # negated “删除旧『近景』条款” exposes 近景 to the image model and QC.
        optimized = re.sub(
            r"(?:不得|禁止)?(?:同时)?执行(?:被|已)作废的旧条款"
            r"[：:]?[^。\n]*(?:。|\n|$)",
            "", optimized).strip()
        if not optimized:
            raise ProviderError("Codex提示词审核通过但优化稿为空")
        if "```" in optimized:
            raise ProviderError("Codex优化稿含Markdown代码围栏，拒绝生图")
        scene_guard_tokens = []
        if payload.get("prompt_contract_complete"):
            scene_guard_tokens = scene_prompt_contaminants(
                optimized, payload.get("location") or "", payload,
                allowed_text=scene_allowed_text)
        if scene_guard_tokens:
            # Prompt review is advisory, never an authority to invent a new
            # physical set.  Fall back to the deterministic clean contract
            # instead of blocking production or sending the invented objects.
            optimized = source
        execution_conflicts = self._reviewed_prompt_execution_conflicts(
            optimized)
        if execution_conflicts:
            raise ProviderError(
                "真实图片已被阻止：最终优化稿仍含互斥执行合同："
                + "；".join(execution_conflicts))
        missing = [
            token for token in self._prompt_review_required_tokens(
                source, payload)
            if token not in optimized
        ]
        if not self._prompt_review_count_preserved(optimized, payload):
            missing.append("人物总数")
        if missing:
            evidence = ""
            if "人物总数" in missing:
                segments = [
                    seg.strip() for seg in re.split(r"[；。\n]", optimized)
                    if any(t in seg for t in ("人", "名", "位"))][:3]
                if segments:
                    evidence = "；优化稿人数相关片段:「" + "／".join(
                        seg[:60] for seg in segments) + "」"
            rule_reason = (
                "Codex优化稿删除了不可变事实，拒绝生图："
                + "、".join(missing[:20]) + evidence)
            # 死规则只是初审:字面校验判败的,交 AI 仲裁复核一次。
            # 事实实质存在(换了写法/等价表述)即撤销原判放行并记台账;
            # 事实真被删改则维持原判,照常拒绝生图。
            appeal = self._appeal_rule_verdict(
                rule_id="prompt_review.immutable_facts",
                rule_reason=rule_reason,
                subject=optimized,
                context={
                    "missing_tokens": missing[:20],
                    "expected_character_count": payload.get(
                        "character_count"),
                    "functional_figures": payload.get("functional_figures"),
                    "original_prompt": source[:4000],
                },
                out_dir=out_dir, payload=payload, capability=capability,
                cancel=cancel)
            if not appeal.get("overturned"):
                raise ProviderError(rule_reason)
            self.log.info(
                "router",
                "字面校验判败经仲裁撤销(规则误杀),放行本稿: "
                + str(appeal.get("reason") or "")[:200])
        audit_record = {
            "schema": self.PROMPT_REVIEW_SCHEMA,
            "approved": True,
            "status": (
                "approved_scene_guard_fallback"
                if scene_guard_tokens else "approved"),
            "provider": result.provider,
            "model": result.model or "Codex 提示词审核优化",
            "input_hash": input_hash,
            "original_prompt": source,
            "optimized_prompt": optimized,
            "optimized_hash": self._stable_hash(optimized),
            "issues_found": list(data.get("issues_found") or []),
            "changes_made": list(dict.fromkeys([
                *(data.get("changes_made") or []),
                *([(
                    "审核稿越权新增未在场景母图/3D清单中的物件，"
                    "已自动回退AIFOS干净执行合同:"
                    + "、".join(scene_guard_tokens))]
                  if scene_guard_tokens else []),
                *(["移除已作废旧合同的审计元话术"]
                  if "被作废的旧条款" in str(
                      data.get("optimized_prompt") or "")
                  or "已作废的旧条款" in str(
                      data.get("optimized_prompt") or "") else []),
            ])),
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
    @staticmethod
    def _is_safety_rejection(exc):
        """产线是否因内容安全策略拒收(而非临时故障)。

        题材决定的拒绝对同一集的其它镜头一样会发生,重试与逐张试错
        都没有意义,应当整条产线绕开;临时故障则照常按张回退重试。
        """
        text = str(exc)
        return any(token in text for token in (
            "safety_violations", "rejected by the safety system",
            "safety system", "content_policy_violation",
            "content policy", "内容安全", "安全策略",
        ))

    def _appeal_rule_verdict(self, *, rule_id, rule_reason, subject,
                             context, out_dir, payload=None,
                             capability="", cancel=None):
        """规则上诉庭:内置死规则判败 → AI 仲裁复核一次。

        用户拍板的三级法院(2026-07-28):规则是死的、剧情是活的。
        规则做零成本初审,通过的直接生产;判败的才上诉,由仲裁带着
        裁决条款与上下文判断"真违规 vs 字面化误杀"。撤销必须逐字
        举证(validate_rule_appeal 强制),证据对不上一律维持原判。
        返回 {"overturned": bool, "reason": str, ...};仲裁本身
        不可用/出错时按维持原判处理(失败关闭,不放行未知风险)。
        """
        from ..rule_governance import prompt_adjudication_clause
        from ..rule_appeals import (VERDICT_OVERTURNED, VERDICT_UPHELD,
                                    record_appeal)
        result = None
        data = {}
        try:
            result = self.call("script", {
                "rule_appeal": True,
                "rule_id": rule_id,
                "rule_reason": rule_reason,
                "subject": subject,
                "context": context,
                "adjudication": prompt_adjudication_clause(),
            }, out_dir, cancel=cancel)
            data = result.data or {}
        except (ProviderError, ProviderUnavailable) as exc:
            self.log.warn(
                "router",
                f"规则仲裁不可用({str(exc)[:160]}),维持原判: {rule_id}")
        except Exception as exc:   # 仲裁绝不能反过来拖垮产线
            self.log.warn(
                "router",
                f"规则仲裁异常({str(exc)[:160]}),维持原判: {rule_id}")
        overturned = str(data.get("verdict") or "") == VERDICT_OVERTURNED
        if data:
            record_appeal(
                out_dir,
                episode_id=(payload or {}).get("episode_id"),
                item_id=str((payload or {}).get("item_id")
                            or (payload or {}).get("shot_no") or ""),
                rule_id=rule_id,
                capability=capability,
                verdict=(VERDICT_OVERTURNED if overturned
                         else VERDICT_UPHELD),
                rule_reason=rule_reason,
                arbiter_reason=data.get("reason", ""),
                evidence=data.get("evidence", ""),
                provider=(result.provider if result is not None else ""),
                model=(result.model if result is not None else ""),
                suggested_rule_fix=data.get("suggested_rule_fix", ""))
        return {
            "overturned": overturned,
            "reason": data.get("reason", ""),
            "evidence": data.get("evidence", ""),
            "suggested_rule_fix": data.get("suggested_rule_fix", ""),
        }

    def call(self, capability, payload, out_dir, cancel=None):
        # 全库唯一的能力派发漏斗:知识大脑在这里接进生产。只有写作类
        # 能力会被挂上(图片/视频模型读不懂工作流指导),详见 knowledge_apply。
        if self.knowledge is not None and knowledge_apply.attach(
                self.knowledge, capability, payload,
                warn=lambda message: self.log.warn("knowledge", message)):
            self.log.info(
                "router", f"{capability} 已挂载知识大脑指令")
        strict_provider = str(payload.get("strict_provider") or "").strip()
        required_provider = str(
            payload.get("required_provider") or "").strip()
        strict_model = str(payload.get("model_override") or "").strip()
        if strict_provider:
            self.validate_image_selection(
                strict_provider, capability, payload, strict_model)
        requires_refs = bool(payload.get("require_reference_images")
                             or payload.get("identity_required"))
        allows_text_bootstrap = bool(
            payload.get("allow_text_to_image_bootstrap"))
        supplied_refs = self._supplied_reference_uris(payload)
        if requires_refs and not supplied_refs:
            raise ProviderUnavailable(
                f"能力 {capability} 要求真实参考图，但请求未携带任何图片")
        # Route/model/reference failures are deterministic and free to detect.
        # Resolve them before spending a Codex prompt-review call so the user
        # receives the actual configuration error and no review quota is lost.
        prompt_review_result = None
        if capability in self.IMAGE_CAPABILITIES:
            prompt_review_result = self.review_image_prompt(
                capability, payload, out_dir, cancel=cancel)
        chain = self._routing_chain(capability, payload)
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
            if (name, capability) in self._safety_blocked:
                reason = "本集题材已被该产线安全策略整体拒收"
                fallbacks.append({"provider": name, "reason": reason})
                continue
            if (capability == "video"
                    and payload.get("canonical_scene_reference_required")
                    and payload.get("last")
                    and isinstance(provider, ArkVideoProvider)):
                reason = (
                    "Ark 首尾帧模式无法同时提交统一场景母图，"
                    "禁止无场景锚回退成片")
                self.log.info(
                    "router", f"{name} {reason},回退({capability})")
                fallbacks.append({"provider": name, "reason": reason})
                continue
            if (name, capability) in self._capability_blocked:
                reason = "本服务进程已确认该产线缺少此生成能力"
                fallbacks.append({"provider": name, "reason": reason})
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
                    and not supplied_refs
                    and not allows_text_bootstrap):
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
                self._validate_generated_image_shape(
                    capability, payload, result)
            except ProduceCancelled:
                raise
            except ProviderError as exc:
                if self._is_safety_rejection(exc):
                    # 题材级拒绝:《雨夜凶杀》这类有尸体/血迹的剧,
                    # OpenAI 安全系统会拒掉本集几乎每一张图(实测 73 次
                    # 全拒)。每张都先撞一次墙再回退纯属浪费,记下来
                    # 本次运行内不再向该产线投同类任务。
                    self._safety_blocked.add((name, capability))
                    self.log.warn(
                        "router",
                        f"{name} 因题材安全策略拒绝本集内容(尸体/血迹等),"
                        f"本次运行不再向它投递 {capability},直接用后续产线")
                elif self._is_deterministic_capability_failure(
                        name, capability, exc):
                    self._capability_blocked.add((name, capability))
                    self.log.warn(
                        "router",
                        f"{name} 已确认缺少 {capability} 生成能力；"
                        "本服务进程后续图片直接使用下一真实产线")
                else:
                    self.log.warn(
                        "router",
                        f"{name} 执行失败({exc}),回退({capability})")
                fallbacks.append(
                    {"provider": name, "reason": f"执行失败: {exc}"})
                continue
            except Exception as exc:
                # A malformed provider response must be a route-local failure,
                # never an AttributeError that aborts the entire production
                # stage.  Keep the exact exception in the fallback ledger and
                # let the next configured writer repair/replace the response.
                reason = f"输出结构异常: {type(exc).__name__}: {exc}"
                self.log.warn(
                    "router", f"{name} {reason},回退({capability})")
                fallbacks.append({"provider": name, "reason": reason})
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
