"""Fail-closed runtime capability policy for Seedance video requests.

``seedance2_5`` is a semantic capability request, not a model name.  The
runtime may use it only when the workspace supplies a real CLI model alias and
the installed CLI's own help output proves the advertised limits.  This keeps
future-facing standard/UI fields from silently spending against an older
model that happens to accept similarly named arguments.
"""
from __future__ import annotations

import math
import re
import subprocess


DEFAULT_TIER = "seedance2_0"
UPGRADE_TIER = "seedance2_5"
DEFAULT_MODEL_VERSION = "seedance2.0fast_vip"
KNOWN_SEEDANCE20_MODELS = frozenset({
    "seedance2.0",
    "seedance2.0fast",
    "seedance2.0fast_vip",
    "seedance2.0mini",
    "seedance2.0_vip",
})

TIER_LIMITS = {
    DEFAULT_TIER: {
        "min_duration_seconds": 4,
        "max_duration_seconds": 15,
        "max_material_inputs": 7,
        "max_total_references": 9,
    },
    UPGRADE_TIER: {
        "min_duration_seconds": 4,
        "max_duration_seconds": 30,
        "max_material_inputs": 40,
        "max_total_references": 50,
    },
}


class SeedancePolicyError(ValueError):
    """The requested tier cannot be proven safe on the current runtime."""


def normalize_tier(value):
    aliases = {
        "": DEFAULT_TIER,
        "default": DEFAULT_TIER,
        "seedance2": DEFAULT_TIER,
        "seedance2_0": DEFAULT_TIER,
        "seedance2.0": DEFAULT_TIER,
        "seedance2_5": UPGRADE_TIER,
        "seedance2.5": UPGRADE_TIER,
    }
    tier = aliases.get(str(value or "").strip().lower())
    if tier is None:
        raise SeedancePolicyError(
            "video_model_tier 只允许 seedance2_0 或 seedance2_5")
    return tier


def requested_tier(payload):
    payload = payload if isinstance(payload, dict) else {}
    return normalize_tier(
        payload.get("video_model_tier")
        or payload.get("model_upgrade_tier")
        or payload.get("seedance_tier"))


def configured_model_alias(conf, tier):
    conf = conf if isinstance(conf, dict) else {}
    if tier == DEFAULT_TIER:
        return str(
            conf.get("model_version") or DEFAULT_MODEL_VERSION).strip()
    aliases = conf.get("model_aliases")
    alias = aliases.get(UPGRADE_TIER) if isinstance(aliases, dict) else None
    return str(alias or conf.get("seedance2_5_model_alias") or "").strip()


def resolve_model_alias(payload, conf, tier):
    """Resolve the contract model without letting provider config drift win."""
    payload = payload if isinstance(payload, dict) else {}
    alias = configured_model_alias(conf, tier)
    if tier == DEFAULT_TIER:
        requested = str(payload.get("model_version") or "").strip()
        alias = requested or alias
        if alias not in KNOWN_SEEDANCE20_MODELS:
            raise SeedancePolicyError(
                "seedance2_0 的 model_version 不是已知 Seedance 2.0 型号："
                f"{alias or '<empty>'}")
    return alias


def _reported_limit(reported, canonical):
    aliases = {
        "max_duration_seconds": (
            "max_duration_seconds", "duration_seconds", "duration",),
        "max_material_inputs": (
            "max_material_assets", "max_material_inputs",
            "material_inputs", "materials",),
        "max_total_references": (
            "max_total_references", "total_references", "references",),
    }
    for key in aliases[canonical]:
        try:
            return int(reported.get(key))
        except (AttributeError, TypeError, ValueError):
            continue
    return 0


def validate_upgrade_declaration(policy):
    """Validate the standard/UI declaration, which is intent rather than proof."""
    policy = policy if isinstance(policy, dict) else {}
    if str(policy.get("candidate_capability_key") or "").strip() != \
            UPGRADE_TIER:
        raise SeedancePolicyError(
            "未声明 rules.production.model_upgrade_policy."
            "candidate_capability_key=seedance2_5，禁止自动升模")
    reported = policy.get("reported_limits")
    reported = reported if isinstance(reported, dict) else {}
    expected = TIER_LIMITS[UPGRADE_TIER]
    for field in (
            "max_duration_seconds", "max_material_inputs",
            "max_total_references"):
        value = _reported_limit(reported, field)
        if value < expected[field]:
            raise SeedancePolicyError(
                "model_upgrade_policy.reported_limits 未完整声明 "
                f"{field}>={expected[field]}，禁止自动升模")
    allowed_reasons = policy.get("allowed_reasons")
    allowed_reasons = {
        str(reason).strip() for reason in (allowed_reasons or [])
        if str(reason).strip()
    }
    if not allowed_reasons:
        raise SeedancePolicyError(
            "model_upgrade_policy.allowed_reasons 未声明，禁止按需升模")
    return allowed_reasons


def requested_upgrade_reason(payload):
    payload = payload if isinstance(payload, dict) else {}
    return str(
        payload.get("video_model_reason")
        or payload.get("model_upgrade_reason")
        or "").strip()


_LIMIT_PATTERNS = {
    "max_duration_seconds": (
        r"duration.{0,80}?(?:supported[-_ ]*)?range\s*[:=]?\s*"
        r"\d+\s*[-–~]\s*(?P<n>\d+)",
        r"(?:max(?:imum)?[-_ ]*)?duration(?:[-_ =:]{1,12}|.{0,20}?"
        r"(?:up to|limit|最多|上限))(?P<n>\d+)",
        r"(?:时长|秒数).{0,30}?\d+\s*[-–~至]\s*(?P<n>\d+)",
        r"(?:时长|秒数).{0,20}?(?:最多|上限|可达|支持)?\s*(?P<n>\d+)",
    ),
    "max_material_inputs": (
        r"(?:materials?|assets?)[-_ ]*(?:inputs?)?\s*<=?\s*(?P<n>\d+)",
        r"(?:max(?:imum)?[-_ ]*)?(?:material|asset)[-_ ]*(?:inputs?|"
        r"images?)?(?:[-_ =:]{1,12}|.{0,20}?(?:up to|limit))(?P<n>\d+)",
        r"素材(?:输入|数量)?\s*(?:<=|≤|最多|上限)\s*(?P<n>\d+)",
        r"素材(?:输入|图片|数量)?.{0,20}?(?:最多|上限|可达|支持)?\s*"
        r"(?P<n>\d+)",
    ),
    "max_total_references": (
        r"total[-_ ]*(?:references?|inputs?)\s*<=?\s*(?P<n>\d+)",
        r"(?:max(?:imum)?[-_ ]*)?total[-_ ]*(?:references?|inputs?|images?)"
        r"(?:[-_ =:]{1,12}|.{0,20}?(?:up to|limit))(?P<n>\d+)",
        r"(?:总参考|参考总数|总输入)\s*(?:<=|≤|最多|上限)\s*(?P<n>\d+)",
        r"(?:总参考|参考总数|总输入|图片总数).{0,20}?"
        r"(?:最多|上限|可达|支持)?\s*(?P<n>\d+)",
    ),
}


def _parse_limit(help_text, field):
    values = []
    for pattern in _LIMIT_PATTERNS[field]:
        for match in re.finditer(pattern, help_text, re.IGNORECASE):
            try:
                values.append(int(match.group("n")))
            except (TypeError, ValueError):
                pass
    return max(values) if values else 0


def parse_cli_help(help_text, model_alias):
    text = str(help_text or "")
    alias = str(model_alias or "").strip()
    alias_pattern = (
        rf"(?<![A-Za-z0-9_.-]){re.escape(alias)}(?![A-Za-z0-9_.-])"
        if alias else "")
    return {
        "model_alias": alias,
        "alias_supported": bool(
            alias_pattern and re.search(alias_pattern, text, re.IGNORECASE)),
        "max_duration_seconds": _parse_limit(
            text, "max_duration_seconds"),
        "max_material_inputs": _parse_limit(text, "max_material_inputs"),
        "max_total_references": _parse_limit(
            text, "max_total_references"),
    }


def probe_cli_help(command, model_alias, *, timeout=10, runner=None):
    """Ask the installed CLI itself; config claims alone never enable 2.5."""
    command = [str(part) for part in (command or ["dreamina"])]
    runner = runner or subprocess.run
    help_command = [*command, "multimodal2video", "--help"]
    try:
        completed = runner(
            help_command, capture_output=True, text=True, timeout=timeout,
            check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "supported": False,
            "reason": f"无法执行 dreamina multimodal2video --help: {exc}",
            "help_command": help_command,
        }
    output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    parsed = parse_cli_help(output, model_alias)
    parsed["help_command"] = help_command
    parsed["returncode"] = int(completed.returncode)
    expected = TIER_LIMITS[UPGRADE_TIER]
    missing = []
    if completed.returncode != 0:
        missing.append(f"help退出码={completed.returncode}")
    if not parsed["alias_supported"]:
        missing.append(f"未列出模型别名 {model_alias}")
    for field in (
            "max_duration_seconds", "max_material_inputs",
            "max_total_references"):
        if parsed[field] < expected[field]:
            missing.append(f"{field}<{expected[field]}")
    parsed["supported"] = not missing
    parsed["reason"] = (
        "" if not missing else
        "当前 dreamina CLI 的 help 未证明 Seedance 2.5 能力："
        + "、".join(missing))
    return parsed


def _rounded_duration(value):
    try:
        requested = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SeedancePolicyError("Seedance duration 必须是数字") from exc
    if not math.isfinite(requested):
        raise SeedancePolicyError("Seedance duration 必须是有限数字")
    # CLI accepts whole seconds.  Validate the requested value before rounding
    # so 30.6 cannot be silently clipped to 30.
    return requested, int(requested + 0.5)


def validate_runtime_request(payload, conf, *, command=None, help_probe=None):
    """Resolve a request to an auditable model/limit contract."""
    payload = payload if isinstance(payload, dict) else {}
    conf = conf if isinstance(conf, dict) else {}
    tier = requested_tier(payload)
    limits = dict(TIER_LIMITS[tier])
    alias = resolve_model_alias(payload, conf, tier)
    references = payload.get("reference_images")
    references = references if isinstance(references, list) else []
    unique_references = list(dict.fromkeys(str(uri) for uri in references))
    material_count = len(unique_references)
    boundary_count = int(bool(payload.get("first"))) + int(
        bool(payload.get("last")))
    total_count = material_count + boundary_count
    requested_duration, submitted_duration = _rounded_duration(
        payload.get("duration", conf.get("duration", 8)))

    # CLI accepts whole seconds, so 3.5 is a truthful 4-second submission;
    # 3.0 still cannot be sent without silently lengthening the shot.
    if submitted_duration < limits["min_duration_seconds"]:
        raise SeedancePolicyError(
            f"{tier} 最短 {limits['min_duration_seconds']} 秒，本镜请求 "
            f"{requested_duration:g} 秒（提交为 {submitted_duration} 秒）；"
            "请回分镜层修改，不静默拉长")
    if requested_duration > limits["max_duration_seconds"]:
        raise SeedancePolicyError(
            f"{tier} 最长 {limits['max_duration_seconds']} 秒，本镜请求 "
            f"{requested_duration:g} 秒；禁止静默截短")
    if material_count > limits["max_material_inputs"]:
        raise SeedancePolicyError(
            f"{tier} 最多 {limits['max_material_inputs']} 份素材输入，"
            f"当前 {material_count} 份；禁止静默丢弃参考")
    if total_count > limits["max_total_references"]:
        raise SeedancePolicyError(
            f"{tier} 最多 {limits['max_total_references']} 个总参考输入，"
            f"当前 {total_count} 个；禁止静默丢弃参考")

    probe = None
    upgrade_reason = ""
    if tier == UPGRADE_TIER:
        allowed_reasons = validate_upgrade_declaration(
            payload.get("model_upgrade_policy"))
        upgrade_reason = requested_upgrade_reason(payload)
        if not upgrade_reason:
            raise SeedancePolicyError(
                "已请求 seedance2_5，但未填写逐镜 video_model_reason；"
                "Seedance 2.5 只允许按需升级")
        if upgrade_reason not in allowed_reasons:
            raise SeedancePolicyError(
                "seedance2_5 升级原因不在 model_upgrade_policy."
                f"allowed_reasons 内：{upgrade_reason}")
        if not alias:
            raise SeedancePolicyError(
                "已请求 seedance2_5，但 providers.jimeng.model_aliases."
                "seedance2_5 未配置真实 CLI model alias")
        probe = help_probe() if callable(help_probe) else help_probe
        probe = probe or probe_cli_help(command, alias)
        if not isinstance(probe, dict) or not probe.get("supported"):
            reason = (probe or {}).get("reason") if isinstance(
                probe, dict) else "CLI help 探测没有返回能力证明"
            raise SeedancePolicyError(
                f"已请求 seedance2_5，但当前 CLI 不支持，已在提交前阻断："
                f"{reason}")

    return {
        "tier": tier,
        "upgrade_reason": upgrade_reason,
        "model_alias": alias,
        "requested_duration": requested_duration,
        "submitted_duration": submitted_duration,
        "material_reference_count": material_count,
        "total_reference_count": total_count,
        "limits": limits,
        "cli_probe": probe,
    }
