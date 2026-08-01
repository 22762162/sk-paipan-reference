"""创作选片模式的纯策略与候选版本令牌。

这个模块刻意不依赖数据库、文件系统或 Web 层。调用方可以先用这里的
不可变决策建立任务，再自行决定如何持久化：

* 内容视觉质检只提供非阻断观察，不能自动返工或卡住生产；
* 创作选片模式开启时，图片和视频内容视觉质检均关闭；
* 提示词、导演合同和技术完整性检查始终开启；
* 首次每镜固定产生四个同合同候选，AI 自动选优晋升；
  人工仍可覆盖选择，但不再是生产门禁；
* 内容/合同问题允许自动修复后补抽三张，但最多只补一批；
* 补抽后 AI 始终自动选最高分继续；都未过时标记风险后仍晋升最优张；
* 只有零张技术可用图时记 ``technical_incomplete``，且不阻断其他镜头；
* 网络/API 等技术失败仍可以做候选槽位级重试；
* 旧版本任务迟到时不得覆盖当前正式资产。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Union


SELECTION_POLICY_SCHEMA = "aifos.selection-policy/v1"
CANDIDATES_PER_SHOT = 4
REPAIR_CANDIDATES_PER_BATCH = 3
MAX_AUTO_REPAIR_BATCHES = 1
CANDIDATE_VERSION_SCHEMA = "aifos.candidate-set/v1"
SELECTION_SOURCES = frozenset(("manual", "ai"))


class FailureClass(str, Enum):
    """决定一次失败能否自动重试的稳定分类。"""

    NETWORK = "network"
    API = "api"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    TECHNICAL_INTEGRITY = "technical_integrity"
    CONTENT = "content"
    CONTRACT = "contract"


_RETRYABLE_FAILURES = frozenset((
    FailureClass.NETWORK,
    FailureClass.API,
    FailureClass.TIMEOUT,
    FailureClass.RATE_LIMIT,
    FailureClass.TECHNICAL_INTEGRITY,
))

_REPAIR_BATCH_FAILURES = frozenset((
    FailureClass.CONTENT,
    FailureClass.CONTRACT,
    FailureClass.TECHNICAL_INTEGRITY,
))


@dataclass(frozen=True)
class SelectionModePolicy:
    """一次生产运行采用的不可变选片策略。"""

    schema: str
    selection_mode_enabled: bool
    image_content_qc_enabled: bool
    video_content_qc_enabled: bool
    content_qc_blocking: bool
    content_qc_auto_retry: bool
    prompt_review_enabled: bool
    director_contract_review_enabled: bool
    technical_integrity_checks_enabled: bool
    initial_candidates_per_shot: int
    candidates_per_shot: int
    repair_candidates_per_batch: int
    max_auto_repair_batches: int
    candidate_ai_ranking_enabled: bool
    auto_select_best: bool
    manual_selection_override_allowed: bool
    ranking_failure_fallback: str
    ranking_failure_marks_risk: bool
    repair_auto_select_best: bool
    failed_after_repair_auto_select_best: bool
    failed_after_repair_marks_risk: bool
    zero_usable_status: str
    failure_blocks_pipeline: bool
    failure_blocks_other_shots: bool
    failure_blocks_downstream_stage: bool
    downstream_requires_selection: bool


def build_selection_policy(
        selection_mode_enabled: bool,
        *,
        image_content_qc_requested: bool = True,
        video_content_qc_requested: bool = True,
        initial_candidates_per_shot: int = CANDIDATES_PER_SHOT,
        repair_candidates_per_batch: int = REPAIR_CANDIDATES_PER_BATCH,
        max_auto_repair_batches: int = MAX_AUTO_REPAIR_BATCHES,
) -> SelectionModePolicy:
    """解析选片策略，同时保护不可关闭的前置与技术检查。

    非选片模式可以保留内容观察，但内容观察在新架构里仍然不能阻断或
    触发自动返工。开启选片模式则强制关闭两种内容视觉质检。
    """
    selection_mode_enabled = bool(selection_mode_enabled)
    initial_candidates = _positive_int(
        initial_candidates_per_shot, field="initial_candidates_per_shot")
    if initial_candidates != CANDIDATES_PER_SHOT:
        raise ValueError("首轮候选当前固定为4张")
    repair_candidates = _positive_int(
        repair_candidates_per_batch,
        field="repair_candidates_per_batch",
    )
    if repair_candidates != REPAIR_CANDIDATES_PER_BATCH:
        raise ValueError("问题镜头自动补抽当前固定为3张")
    repair_batches = _nonnegative_int(
        max_auto_repair_batches, field="max_auto_repair_batches")
    if repair_batches != MAX_AUTO_REPAIR_BATCHES:
        raise ValueError("自动修复/补抽当前固定为1批")
    return SelectionModePolicy(
        schema=SELECTION_POLICY_SCHEMA,
        selection_mode_enabled=selection_mode_enabled,
        image_content_qc_enabled=(
            bool(image_content_qc_requested) and not selection_mode_enabled),
        video_content_qc_enabled=(
            bool(video_content_qc_requested) and not selection_mode_enabled),
        content_qc_blocking=False,
        content_qc_auto_retry=False,
        prompt_review_enabled=True,
        director_contract_review_enabled=True,
        technical_integrity_checks_enabled=True,
        initial_candidates_per_shot=initial_candidates,
        # 保留旧字段，避免已有消费方误把补抽张数当成首轮张数。
        candidates_per_shot=initial_candidates,
        repair_candidates_per_batch=repair_candidates,
        max_auto_repair_batches=repair_batches,
        # 候选视觉排名与“质检是否通过”是两件事。关闭内容
        # QC 只关闭通过/返工门禁，不关闭 AI 在4张或3张里选优。
        candidate_ai_ranking_enabled=True,
        auto_select_best=True,
        manual_selection_override_allowed=True,
        ranking_failure_fallback="first_technically_usable",
        ranking_failure_marks_risk=True,
        repair_auto_select_best=True,
        failed_after_repair_auto_select_best=True,
        failed_after_repair_marks_risk=True,
        zero_usable_status="technical_incomplete",
        failure_blocks_pipeline=False,
        failure_blocks_other_shots=False,
        failure_blocks_downstream_stage=False,
        downstream_requires_selection=False,
    )


def _config_bool(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off"):
            return False
    raise ValueError(f"{field} 必须是布尔值")


def selection_policy_from_config(
        config: Optional[Mapping[str, Any]],
) -> SelectionModePolicy:
    """按设置页最终键名解析策略，并兼容旧图片质检键。

    正式键位于 ``defaults``：``selection_mode``、
    ``image_content_qc``、``video_content_qc``、
    ``shot_candidate_count``、``shot_repair_candidate_count`` 和
    ``shot_auto_repair_batches``。旧工作区没有
    ``image_content_qc`` 时才回退
    到 ``image_qc``；候选数不是 4 时拒绝启动新策略，避免静默回到不同
    镜头不同张数的旧行为。
    """
    config = config if isinstance(config, Mapping) else {}
    raw_defaults = config.get("defaults", {})
    defaults = raw_defaults if isinstance(raw_defaults, Mapping) else {}
    selection_mode = _config_bool(
        # 新产线默认就是创作选片模式；旧 workspace 没有保存该键时也
        # 必须获得升级后的非阻断行为。用户显式保存 False 才恢复旧偏好。
        defaults.get("selection_mode", True),
        field="defaults.selection_mode",
    )
    image_content_qc = _config_bool(
        defaults.get(
            "image_content_qc",
            defaults.get("image_qc", True),
        ),
        field="defaults.image_content_qc",
    )
    video_content_qc = _config_bool(
        defaults.get("video_content_qc", True),
        field="defaults.video_content_qc",
    )
    candidate_count = _positive_int(
        defaults.get("shot_candidate_count", CANDIDATES_PER_SHOT),
        field="defaults.shot_candidate_count",
    )
    if candidate_count != CANDIDATES_PER_SHOT:
        raise ValueError(
            "defaults.shot_candidate_count 当前只允许固定为4")
    repair_candidate_count = _positive_int(
        defaults.get(
            "shot_repair_candidate_count", REPAIR_CANDIDATES_PER_BATCH),
        field="defaults.shot_repair_candidate_count",
    )
    if repair_candidate_count != REPAIR_CANDIDATES_PER_BATCH:
        raise ValueError(
            "defaults.shot_repair_candidate_count 当前只允许固定为3")
    auto_repair_batches = _nonnegative_int(
        defaults.get(
            "shot_auto_repair_batches", MAX_AUTO_REPAIR_BATCHES),
        field="defaults.shot_auto_repair_batches",
    )
    if auto_repair_batches != MAX_AUTO_REPAIR_BATCHES:
        raise ValueError(
            "defaults.shot_auto_repair_batches 当前只允许固定为1")
    return build_selection_policy(
        selection_mode,
        image_content_qc_requested=image_content_qc,
        video_content_qc_requested=video_content_qc,
        initial_candidates_per_shot=candidate_count,
        repair_candidates_per_batch=repair_candidate_count,
        max_auto_repair_batches=auto_repair_batches,
    )


def should_retry_failure(
        failure: Union[FailureClass, str],
        *,
        attempts_remaining: int,
) -> bool:
    """只有尚有次数的技术失败可重试；内容意见永远不触发重试。"""
    try:
        failure_class = FailureClass(failure)
    except (TypeError, ValueError):
        return False
    return attempts_remaining > 0 and failure_class in _RETRYABLE_FAILURES


def should_start_repair_batch(
        failure: Union[FailureClass, str],
        *,
        completed_repair_batches: int,
        policy: Optional[SelectionModePolicy] = None,
) -> bool:
    """内容/合同/技术完整性问题是否应再补抽一批。

    这与 ``should_retry_failure`` 的网络槽位级重试分开：一批中的
    某个 API 请求失败可以补槽，但同一问题镜头最多只能再启动
    ``max_auto_repair_batches`` 批，防止无限抽卡。
    """
    try:
        failure_class = FailureClass(failure)
    except (TypeError, ValueError):
        return False
    try:
        completed = _nonnegative_int(
            completed_repair_batches, field="completed_repair_batches")
    except ValueError:
        return False
    effective = policy or build_selection_policy(True)
    return (
        failure_class in _REPAIR_BATCH_FAILURES
        and completed < effective.max_auto_repair_batches
    )


def _stable_digest(value: Any) -> str:
    """对 JSON 兼容结构产生与字典插入顺序无关的稳定摘要。"""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是正整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是正整数") from exc
    if number < 1 or str(value).strip() != str(number):
        raise ValueError(f"{field} 必须是正整数")
    return number


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是非负整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是非负整数") from exc
    if number < 0 or str(value).strip() != str(number):
        raise ValueError(f"{field} 必须是非负整数")
    return number


@dataclass(frozen=True)
class CandidateSetVersion:
    """镜头候选组的不可变事实快照。

    prompt/reference 只保存摘要，避免调用方之后修改原字典时令版本语义
    悄悄变化。``token`` 可直接写入异步任务和生成结果。
    """

    schema: str
    episode_id: str
    shot_no: int
    contract_revision: int
    candidate_revision: int
    prompt_digest: str
    reference_digest: str
    token: str


def build_candidate_set_version(
        *,
        episode_id: Any,
        shot_no: Any,
        contract_revision: Any,
        candidate_revision: Any,
        prompt: Any,
        reference_manifest: Any,
) -> CandidateSetVersion:
    """从镜头合同事实生成确定性的候选组版本令牌。"""
    normalized_episode_id = str(episode_id or "").strip()
    if not normalized_episode_id:
        raise ValueError("episode_id 不能为空")
    normalized_shot_no = _positive_int(shot_no, field="shot_no")
    normalized_contract_revision = _positive_int(
        contract_revision, field="contract_revision")
    normalized_candidate_revision = _positive_int(
        candidate_revision, field="candidate_revision")
    prompt_digest = _stable_digest(prompt)
    reference_digest = _stable_digest(reference_manifest)
    identity = {
        "schema": CANDIDATE_VERSION_SCHEMA,
        "episode_id": normalized_episode_id,
        "shot_no": normalized_shot_no,
        "contract_revision": normalized_contract_revision,
        "candidate_revision": normalized_candidate_revision,
        "prompt_digest": prompt_digest,
        "reference_digest": reference_digest,
    }
    token = f"cset-v1:{_stable_digest(identity)}"
    return CandidateSetVersion(
        schema=CANDIDATE_VERSION_SCHEMA,
        episode_id=normalized_episode_id,
        shot_no=normalized_shot_no,
        contract_revision=normalized_contract_revision,
        candidate_revision=normalized_candidate_revision,
        prompt_digest=prompt_digest,
        reference_digest=reference_digest,
        token=token,
    )


@dataclass(frozen=True)
class CandidateResultVersion:
    """一个并行候选任务随身携带的版本凭证。"""

    candidate_set_token: str
    candidate_index: int


def build_candidate_result_versions(
        version: CandidateSetVersion,
) -> tuple[CandidateResultVersion, ...]:
    """固定生成四个同版本任务凭证，供调度层并行扇出。"""
    return tuple(
        CandidateResultVersion(version.token, index)
        for index in range(1, CANDIDATES_PER_SHOT + 1)
    )


def is_stale_candidate_result(
        result: CandidateResultVersion,
        current_version: CandidateSetVersion,
) -> bool:
    """结果不属于当前候选组时即为陈旧结果。"""
    return result.candidate_set_token != current_version.token


@dataclass(frozen=True)
class PromotionDecision:
    """候选能否成为正式资产的纯决策。"""

    allowed: bool
    stale: bool
    reason: str


def evaluate_candidate_promotion(
        result: CandidateResultVersion,
        current_version: CandidateSetVersion,
        *,
        selection_source: str,
) -> PromotionDecision:
    """只允许人工/AI选中的当前四候选之一覆盖正式资产。"""
    source = str(selection_source or "").strip().lower()
    if source not in SELECTION_SOURCES:
        return PromotionDecision(
            allowed=False,
            stale=is_stale_candidate_result(result, current_version),
            reason="必须由人工或 AI 明确选中候选",
        )
    if result.candidate_index not in range(1, CANDIDATES_PER_SHOT + 1):
        return PromotionDecision(
            allowed=False,
            stale=is_stale_candidate_result(result, current_version),
            reason="候选序号不属于当前四图候选组",
        )
    if is_stale_candidate_result(result, current_version):
        return PromotionDecision(
            allowed=False,
            stale=True,
            reason="候选结果属于旧版本，禁止覆盖当前正式资产",
        )
    return PromotionDecision(
        allowed=True,
        stale=False,
        reason=f"当前版本候选已由{source}选中",
    )


def downstream_ready(
        selected_result: Optional[CandidateResultVersion],
        current_version: CandidateSetVersion,
        *,
        selection_source: Optional[str] = None,
) -> bool:
    """当前版本有明确人工/AI选片时，镜头下游依赖才满足。"""
    if selected_result is None:
        return False
    return evaluate_candidate_promotion(
        selected_result,
        current_version,
        selection_source=selection_source or "",
    ).allowed
