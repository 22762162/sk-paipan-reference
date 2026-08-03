"""AI 编剧与导演的非阻断策略合同。

本模块不调用模型、不读写数据库，也不改变生产状态。它只负责把独立剧本
评审、双稿竞稿、跨集连续性、整集导演规划和九宫格浏览组织成稳定数据：

* 评审结论一律是 ``review/advice``，不得成为生产闸门；
* 编剧生成运行不能把自己的自评分冒充独立评审；
* 九宫格是一页最多九个独立镜头的浏览模型，绝不生成单图多格参考资产。
"""

from __future__ import annotations

import copy
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence


STORY_REVIEW_SCHEMA = "aifos.story-review/v1"
DRAFT_DECISION_SCHEMA = "aifos.draft-decision/v1"
CONTINUITY_INPUT_SCHEMA = "aifos.episode-continuity-input/v1"
DIRECTOR_PLAN_SCHEMA = "aifos.episode-director-plan/v1"
NINE_GRID_SCHEMA = "aifos.nine-grid-browser/v1"
NINE_GRID_INDEX_SCHEMA = "aifos.nine-grid-browser-index/v1"
STORYBOARD_REVIEW_DOCUMENTS_SCHEMA = (
    "aifos.storyboard-review-documents/v1")

REVIEW_KIND = "review"
PRODUCTION_BLOCKING = False
MAX_GRID_CELLS = 9


def _required_text(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} 不能为空")
    return text


def _text(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是正整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是正整数") from exc
    if number < 1:
        raise ValueError(f"{field} 必须是正整数")
    return number


def _unique_texts(values: Any, *, field: str) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, Iterable) or isinstance(values, Mapping):
        raise ValueError(f"{field} 必须是文本列表")
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    if not result:
        raise ValueError(f"{field} 不能为空")
    return tuple(result)


def _document_value(value: Any) -> Any:
    """Return JSON-safe review data without adding production semantics."""
    if is_dataclass(value):
        return _document_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _document_value(item) for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_document_value(item) for item in value]
    return value


def review_document(report: Any) -> dict[str, Any]:
    """Serialize one review contract for ``ProjectCenter.save_document``.

    Only reports that explicitly identify themselves as non-blocking reviews
    are accepted.  This prevents an integration caller from accidentally
    saving a production gate under a review document kind.
    """
    document = _document_value(report)
    if not isinstance(document, dict):
        raise ValueError("report 必须是结构化评审对象")
    if document.get("kind") != REVIEW_KIND:
        raise ValueError("report.kind 必须是 review")
    if document.get("production_blocking") is not False:
        raise ValueError("故事智能报告必须明确 production_blocking=false")
    return document


class ReviewDimension(str, Enum):
    """独立剧本评审庭固定五维。"""

    CAUSAL_CHAIN = "causal_chain"
    CONFLICT_DENSITY = "conflict_density"
    CHARACTER_ARC = "character_arc"
    DIALOGUE_QUALITY = "dialogue_quality"
    HOOK_STRENGTH = "hook_strength"


DIMENSION_LABELS = {
    ReviewDimension.CAUSAL_CHAIN: "因果链",
    ReviewDimension.CONFLICT_DENSITY: "冲突密度",
    ReviewDimension.CHARACTER_ARC: "人物弧",
    ReviewDimension.DIALOGUE_QUALITY: "台词质感",
    ReviewDimension.HOOK_STRENGTH: "钩子强度",
}


@dataclass(frozen=True)
class DimensionReview:
    """一个维度的独立评分证据和定向修改。"""

    dimension: ReviewDimension
    label: str
    score: int
    evidence: tuple[str, ...]
    directed_revision: tuple[str, ...]


@dataclass(frozen=True)
class ScriptReviewCourt:
    """独立于剧本生成运行的五维审稿记录。"""

    schema: str
    kind: str
    production_blocking: bool
    script_version: str
    generator_run_id: str
    reviewer_run_id: str
    reviewer_source: str
    dimensions: tuple[DimensionReview, ...]
    advice: tuple[str, ...]


def _dimension_review(
        dimension: ReviewDimension,
        value: Any,
) -> DimensionReview:
    if not isinstance(value, Mapping):
        raise ValueError(f"{DIMENSION_LABELS[dimension]} 必须提供评审对象")
    score = value.get("score")
    if isinstance(score, bool):
        raise ValueError(f"{DIMENSION_LABELS[dimension]}评分必须为1至5")
    try:
        score = int(score)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{DIMENSION_LABELS[dimension]}评分必须为1至5") from exc
    if score < 1 or score > 5:
        raise ValueError(f"{DIMENSION_LABELS[dimension]}评分必须为1至5")
    return DimensionReview(
        dimension=dimension,
        label=DIMENSION_LABELS[dimension],
        score=score,
        evidence=_unique_texts(
            value.get("evidence"),
            field=f"{DIMENSION_LABELS[dimension]}证据",
        ),
        directed_revision=_unique_texts(
            value.get("directed_revision"),
            field=f"{DIMENSION_LABELS[dimension]}定向修改",
        ),
    )


def build_script_review_court(
        *,
        script_version: Any,
        generator_run_id: Any,
        reviewer_run_id: Any,
        reviewer_source: Any,
        dimension_reviews: Mapping[Any, Any],
) -> ScriptReviewCourt:
    """建立独立五维评审；生成器同轮自评会被拒绝。

    这里只校验评审合同真实性，不因低分阻断生产。低分会进入 ``advice``
    供编剧定向修改，调用方仍可继续其它镜头和生产环节。
    """
    script_version = _required_text(script_version, field="script_version")
    generator_run_id = _required_text(
        generator_run_id, field="generator_run_id")
    reviewer_run_id = _required_text(reviewer_run_id, field="reviewer_run_id")
    reviewer_source = _required_text(reviewer_source, field="reviewer_source")
    if reviewer_run_id == generator_run_id:
        raise ValueError("剧本生成运行不能自报评分充当独立评审")
    if reviewer_source.lower() in {
            "generator", "self", "self_report", "生成器", "自评"}:
        raise ValueError("reviewer_source 必须是独立评审来源")
    if not isinstance(dimension_reviews, Mapping):
        raise ValueError("dimension_reviews 必须是五维评审对象")

    normalized: dict[ReviewDimension, Any] = {}
    for raw_key, value in dimension_reviews.items():
        try:
            key = raw_key if isinstance(
                raw_key, ReviewDimension) else ReviewDimension(str(raw_key))
        except ValueError as exc:
            raise ValueError(f"未知评审维度: {raw_key}") from exc
        normalized[key] = value
    missing = [item for item in ReviewDimension if item not in normalized]
    extra_count = len(normalized) - len(ReviewDimension)
    if missing or extra_count:
        labels = "、".join(DIMENSION_LABELS[item] for item in missing)
        raise ValueError(f"必须完整提供五维评审；缺少: {labels or '无'}")

    reviews = tuple(
        _dimension_review(dimension, normalized[dimension])
        for dimension in ReviewDimension
    )
    advice = tuple(
        f"{review.label}：{revision}"
        for review in reviews
        for revision in review.directed_revision
        if review.score < 5
    )
    return ScriptReviewCourt(
        schema=STORY_REVIEW_SCHEMA,
        kind=REVIEW_KIND,
        production_blocking=PRODUCTION_BLOCKING,
        script_version=script_version,
        generator_run_id=generator_run_id,
        reviewer_run_id=reviewer_run_id,
        reviewer_source=reviewer_source,
        dimensions=reviews,
        advice=advice,
    )


@dataclass(frozen=True)
class DraftSource:
    """竞稿候选的来源事实，不保存或篡改正文。"""

    source_id: str
    engine: str
    document_ref: str
    generator_run_id: str


@dataclass(frozen=True)
class SourceContribution:
    """融合稿从某个来源保留的具体长处。"""

    source_id: str
    aspect: str
    retained_value: str
    reason: str


@dataclass(frozen=True)
class DraftFusionDecision:
    """双稿竞稿/融合的数据合同，不执行模型调用。"""

    schema: str
    kind: str
    production_blocking: bool
    decision_id: str
    sources: tuple[DraftSource, DraftSource]
    preferred_source_id: str
    output_document_ref: str
    contributions: tuple[SourceContribution, ...]
    fusion_reasons: tuple[str, ...]
    advice: tuple[str, ...]


def _draft_source(value: Any) -> DraftSource:
    if isinstance(value, DraftSource):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("竞稿来源必须是对象")
    return DraftSource(
        source_id=_required_text(value.get("source_id"), field="source_id"),
        engine=_required_text(value.get("engine"), field="engine"),
        document_ref=_required_text(
            value.get("document_ref"), field="document_ref"),
        generator_run_id=_required_text(
            value.get("generator_run_id"), field="generator_run_id"),
    )


def _contribution(value: Any) -> SourceContribution:
    if isinstance(value, SourceContribution):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("融合贡献必须是对象")
    return SourceContribution(
        source_id=_required_text(value.get("source_id"), field="source_id"),
        aspect=_required_text(value.get("aspect"), field="aspect"),
        retained_value=_required_text(
            value.get("retained_value"), field="retained_value"),
        reason=_required_text(value.get("reason"), field="reason"),
    )


def build_draft_fusion_decision(
        *,
        decision_id: Any,
        sources: Sequence[Any],
        preferred_source_id: Any,
        output_document_ref: Any,
        contributions: Sequence[Any],
        fusion_reasons: Any,
) -> DraftFusionDecision:
    """记录双稿选优与融合理由；来源必须完整且至少各贡献一项。"""
    if isinstance(sources, (str, bytes)) or len(sources) != 2:
        raise ValueError("双稿竞稿必须且只能提供两个来源")
    normalized_sources = tuple(_draft_source(item) for item in sources)
    source_ids = tuple(item.source_id for item in normalized_sources)
    if len(set(source_ids)) != 2:
        raise ValueError("双稿来源 source_id 必须不同")
    preferred_source_id = _required_text(
        preferred_source_id, field="preferred_source_id")
    if preferred_source_id not in source_ids:
        raise ValueError("preferred_source_id 必须来自竞稿来源")
    normalized_contributions = tuple(
        _contribution(item) for item in contributions)
    contribution_sources = {item.source_id for item in normalized_contributions}
    unknown = contribution_sources.difference(source_ids)
    if unknown:
        raise ValueError("融合贡献引用了未知来源")
    if set(source_ids).difference(contribution_sources):
        raise ValueError("融合决定必须保留两个来源各自的具体长处")
    reasons = _unique_texts(fusion_reasons, field="fusion_reasons")
    return DraftFusionDecision(
        schema=DRAFT_DECISION_SCHEMA,
        kind=REVIEW_KIND,
        production_blocking=PRODUCTION_BLOCKING,
        decision_id=_required_text(decision_id, field="decision_id"),
        sources=normalized_sources,  # type: ignore[arg-type]
        preferred_source_id=preferred_source_id,
        output_document_ref=_required_text(
            output_document_ref, field="output_document_ref"),
        contributions=normalized_contributions,
        fusion_reasons=reasons,
        advice=("保留来源标记。", "按融合理由复核成稿。"),
    )


class ContinuityDomain(str, Enum):
    CHARACTER = "character"
    PROP = "prop"
    WARDROBE = "wardrobe"
    SCENE = "scene"


@dataclass(frozen=True)
class ContinuityState:
    domain: ContinuityDomain
    entity_id: str
    state: str
    evidence: str


@dataclass(frozen=True)
class EpisodeContinuityInput:
    """下一集编剧必须收到的前集出口事实。"""

    schema: str
    kind: str
    production_blocking: bool
    previous_episode_id: str
    previous_exit_state: str
    unresolved_hooks: tuple[str, ...]
    states: tuple[ContinuityState, ...]
    instructions: tuple[str, ...]


def _continuity_state(
        domain: ContinuityDomain,
        entity_id: Any,
        raw: Any,
) -> ContinuityState:
    if isinstance(raw, Mapping):
        state = raw.get("state")
        evidence = raw.get("evidence")
    else:
        state = raw
        evidence = "前集出口状态"
    return ContinuityState(
        domain=domain,
        entity_id=_required_text(entity_id, field=f"{domain.value}.entity_id"),
        state=_required_text(state, field=f"{domain.value}.state"),
        evidence=_required_text(evidence, field=f"{domain.value}.evidence"),
    )


def build_episode_continuity_input(
        *,
        previous_episode_id: Any,
        previous_exit_state: Any,
        unresolved_hooks: Optional[Sequence[Any]] = None,
        character_states: Optional[Mapping[Any, Any]] = None,
        prop_states: Optional[Mapping[Any, Any]] = None,
        wardrobe_states: Optional[Mapping[Any, Any]] = None,
        scene_states: Optional[Mapping[Any, Any]] = None,
) -> EpisodeContinuityInput:
    """把跨集事实压成短指令，空状态也显式保留而不阻断。"""
    raw_domains = (
        (ContinuityDomain.CHARACTER, character_states or {}),
        (ContinuityDomain.PROP, prop_states or {}),
        (ContinuityDomain.WARDROBE, wardrobe_states or {}),
        (ContinuityDomain.SCENE, scene_states or {}),
    )
    states = tuple(
        _continuity_state(domain, entity_id, raw)
        for domain, values in raw_domains
        for entity_id, raw in values.items()
    )
    hooks = tuple(dict.fromkeys(
        str(item).strip() for item in (unresolved_hooks or ())
        if str(item or "").strip()
    ))
    return EpisodeContinuityInput(
        schema=CONTINUITY_INPUT_SCHEMA,
        kind=REVIEW_KIND,
        production_blocking=PRODUCTION_BLOCKING,
        previous_episode_id=_required_text(
            previous_episode_id, field="previous_episode_id"),
        previous_exit_state=_required_text(
            previous_exit_state, field="previous_exit_state"),
        unresolved_hooks=hooks,
        states=states,
        instructions=(
            "承接前集出口，不擅自重置。",
            "未回收钩子按剧情推进。",
            "人物、道具、服装、场景状态须连续。",
        ),
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _compact_state(value: Any) -> str:
    if isinstance(value, Mapping):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return "；".join(
            text for text in (_compact_state(item) for item in value)
            if text)
    return _text(value)


def _text_items(value: Any) -> tuple[str, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = value
    elif value in (None, "", {}, []):
        values = ()
    else:
        values = (value,)
    return tuple(dict.fromkeys(
        _text(item) for item in values if _text(item)))


def _last_mapping(values: Any) -> Mapping[str, Any]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return {}
    return next((
        item for item in reversed(values) if isinstance(item, Mapping)
    ), {})


def derive_episode_continuity_input(
        *,
        previous_episode_id: Any,
        previous_script: Optional[Mapping[str, Any]] = None,
        previous_storyboard: Optional[Mapping[str, Any]] = None,
        previous_continuity: Optional[Mapping[str, Any]] = None,
) -> EpisodeContinuityInput:
    """Derive the next writer's input from actual saved episode documents.

    Extraction is deliberately conservative: only the last structured shot,
    its matching scene and explicitly named cross-episode hooks are used.  A
    missing fact becomes review text instead of a production failure or an
    invented state.
    """
    script = _mapping(previous_script)
    storyboard = _mapping(previous_storyboard)
    continuity = _mapping(previous_continuity)
    last_shot = _last_mapping(storyboard.get("shots"))
    shot_no = _shot_no(last_shot, 1) if last_shot else 0
    scene_no = _scene_no(last_shot, 1) if last_shot else 0
    evidence = (
        f"前集分镜第{shot_no}镜 end_state" if shot_no
        else "前集保存文档")

    raw_end_states = _mapping(last_shot.get("end_state"))
    character_states: dict[str, Any] = {}
    wardrobe_states: dict[str, Any] = {}
    for name, raw_state in raw_end_states.items():
        entity = _text(name)
        state = _mapping(raw_state)
        if not entity:
            continue
        character_states[entity] = {
            "state": _compact_state(raw_state),
            "evidence": evidence,
        }
        appearance = {
            key: copy.deepcopy(state.get(key)) for key in (
                "wardrobe", "headwear", "hair_visibility", "hair_makeup")
            if state.get(key) not in (None, "", {}, [])
        }
        if appearance:
            wardrobe_states[entity] = {
                "state": _compact_state(appearance),
                "evidence": evidence,
            }

    prop_states: dict[str, Any] = {}
    raw_props = (
        last_shot.get("frame_props") or last_shot.get("prop_states") or ())
    if isinstance(raw_props, Sequence) and not isinstance(
            raw_props, (str, bytes)):
        for raw_prop in raw_props:
            if not isinstance(raw_prop, Mapping):
                continue
            phase = _text(raw_prop.get("phase")).lower()
            if phase and phase not in {"end", "freeze"}:
                continue
            entity = _text(
                raw_prop.get("name") or raw_prop.get("prop_name")
                or raw_prop.get("prop_id"))
            if not entity:
                continue
            state = {
                key: copy.deepcopy(value)
                for key, value in raw_prop.items()
                if key not in {"name", "prop_name", "prop_id", "phase"}
                and value not in (None, "", {}, [])
            }
            prop_states[entity] = {
                "state": _compact_state(state or raw_prop),
                "evidence": (
                    f"前集分镜第{shot_no}镜 {phase or 'end'} 道具状态"),
            }

    scenes = [
        scene for scene in (script.get("scenes") or ())
        if isinstance(scene, Mapping)
    ]
    last_scene = next((
        scene for scene in reversed(scenes)
        if not scene_no or str(scene.get("scene_no")) == str(scene_no)
    ), _last_mapping(scenes))
    scene_states: dict[str, Any] = {}
    if last_scene:
        scene_name = _text(
            last_scene.get("location"),
            f"scene-{scene_no or len(scenes)}")
        director_logic = _mapping(last_scene.get("director_logic"))
        scene_exit = _text(
            director_logic.get("exit_state")
            or last_scene.get("exit_state")
            or last_scene.get("action"))
        if scene_exit:
            scene_states[scene_name] = {
                "state": scene_exit,
                "evidence": f"前集第{scene_no or len(scenes)}场出口",
            }

    if raw_end_states:
        previous_exit_state = (
            f"前集分镜第{shot_no}镜结尾：" + "；".join(
                f"{name}={_compact_state(state)}"
                for name, state in raw_end_states.items()))
    elif scene_states:
        previous_exit_state = next(iter(scene_states.values()))["state"]
    else:
        previous_exit_state = (
            "前集已结束；未抽取到结构化出口状态，请编剧人工核对前集末场。")

    story_background = _mapping(script.get("story_background"))
    background_narrative = _mapping(story_background.get("narrative"))
    production_analysis = _mapping(script.get("production_analysis"))
    analysis_narrative = _mapping(production_analysis.get("narrative"))
    hook_values = (
        script.get("unresolved_hooks"),
        script.get("continuity_hooks"),
        story_background.get("continuity_hooks"),
        background_narrative.get("continuity_hooks"),
        analysis_narrative.get("continuity_hooks"),
        continuity.get("unresolved_hooks"),
    )
    hooks = tuple(dict.fromkeys(
        hook for value in hook_values for hook in _text_items(value)))
    return build_episode_continuity_input(
        previous_episode_id=previous_episode_id,
        previous_exit_state=previous_exit_state,
        unresolved_hooks=hooks,
        character_states=character_states,
        prop_states=prop_states,
        wardrobe_states=wardrobe_states,
        scene_states=scene_states,
    )


@dataclass(frozen=True)
class AdjacentShotRepetition:
    previous_shot_no: int
    current_shot_no: int
    shot_scale: str
    camera_movement: str
    advice: str


@dataclass(frozen=True)
class SceneDramaticPlan:
    scene_no: int
    dramatic_function: str
    shot_count: int
    advice: str


@dataclass(frozen=True)
class InputCompleteness:
    dialogue_missing: tuple[int, ...]
    beat_missing: tuple[int, ...]
    lighting_missing: tuple[int, ...]
    script_reference_missing: tuple[int, ...]
    five_dimensions_missing: tuple[int, ...]
    start_end_state_missing: tuple[int, ...]
    advice: tuple[str, ...]


@dataclass(frozen=True)
class EpisodeDirectorPlan:
    """整集镜头规划摘要，只提出导演建议。"""

    schema: str
    kind: str
    production_blocking: bool
    episode_id: str
    shot_count: int
    total_duration_seconds: float
    shot_scale_distribution: tuple[tuple[str, int], ...]
    camera_movement_distribution: tuple[tuple[str, int], ...]
    shot_function_distribution: tuple[tuple[str, int], ...]
    adjacent_repetitions: tuple[AdjacentShotRepetition, ...]
    scenes: tuple[SceneDramaticPlan, ...]
    input_completeness: InputCompleteness
    advice: tuple[str, ...]


def _shot_no(shot: Mapping[str, Any], fallback: int) -> int:
    try:
        return _positive_int(shot.get("shot_no", fallback), field="shot_no")
    except ValueError:
        return fallback


def _scene_no(shot: Mapping[str, Any], fallback: int) -> int:
    try:
        return _positive_int(shot.get("scene_no", fallback), field="scene_no")
    except ValueError:
        return fallback


def _present(shot: Mapping[str, Any], keys: Sequence[str]) -> bool:
    return any(
        key in shot and shot.get(key) not in (None, "", [], {})
        for key in keys
    )


def _distribution(counter: Counter[str]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def _nested_shot_text(
        shot: Mapping[str, Any],
        *paths: Sequence[str],
) -> str:
    for path in paths:
        value: Any = shot
        for key in path:
            value = value.get(key) if isinstance(value, Mapping) else None
        text = _text(value)
        if text:
            return text
    return ""


def summarize_episode_directing(
        *,
        episode_id: Any,
        shots: Sequence[Mapping[str, Any]],
) -> EpisodeDirectorPlan:
    """汇总整集导演视野，不因缺字段或重复镜头阻断生产。"""
    if isinstance(shots, (str, bytes)):
        raise ValueError("shots 必须是镜头列表")
    normalized = [shot for shot in shots if isinstance(shot, Mapping)]
    scale_counter: Counter[str] = Counter()
    movement_counter: Counter[str] = Counter()
    function_counter: Counter[str] = Counter()
    repetitions: list[AdjacentShotRepetition] = []
    scene_shots: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    dialogue_missing: list[int] = []
    beat_missing: list[int] = []
    lighting_missing: list[int] = []
    script_reference_missing: list[int] = []
    five_dimensions_missing: list[int] = []
    start_end_state_missing: list[int] = []
    total_duration = 0.0
    production_contract_present = any(
        "unit_id" in shot or "pipeline_version" in shot
        for shot in normalized)
    previous: Optional[tuple[int, str, str]] = None

    for index, shot in enumerate(normalized, 1):
        shot_no = _shot_no(shot, index)
        scene_no = _scene_no(shot, 1)
        scale = _text(
            shot.get("shot_scale") or shot.get("shot_size")
            or shot.get("景别") or _nested_shot_text(
                shot,
                ("shot_contract", "景别"),
                ("five_dimensions", "camera_design", "shot_scale")),
            "未标注")
        movement = _text(
            shot.get("camera_movement") or shot.get("camera_move")
            or shot.get("运镜") or _nested_shot_text(
                shot,
                ("shot_contract", "运镜"),
                ("five_dimensions", "camera_design", "movement")),
            "固定")
        shot_function = _text(
            shot.get("shot_function") or shot.get("dramatic_function")
            or shot.get("story_function"), "未标注")
        scale_counter[scale] += 1
        movement_counter[movement] += 1
        function_counter[shot_function] += 1
        try:
            total_duration += max(0.0, float(shot.get("duration") or 0))
        except (TypeError, ValueError, OverflowError):
            pass
        scene_shots[scene_no].append(shot)
        if previous and previous[1:] == (scale, movement):
            repetitions.append(AdjacentShotRepetition(
                previous_shot_no=previous[0],
                current_shot_no=shot_no,
                shot_scale=scale,
                camera_movement=movement,
                advice="确认重复服务情绪，否则变化景别或运镜。",
            ))
        previous = (shot_no, scale, movement)
        dialogue_declared = any(
            key in shot for key in ("dialogue", "lines", "台词"))
        dialogue_expected = (
            dialogue_declared
            or _text(shot.get("kind")).lower() in {"dialogue", "voiceover"})
        if dialogue_expected and not _present(
                shot, ("dialogue", "lines", "台词")):
            dialogue_missing.append(shot_no)
        if not (
                _present(shot, ("beat", "beats", "timing", "timecode", "节拍"))
                or _nested_shot_text(shot, ("performance", "beat"))):
            beat_missing.append(shot_no)
        if not (
                _present(shot, ("lighting", "light", "光影"))
                or _nested_shot_text(
                    shot,
                    ("five_dimensions", "environment_light"),
                    ("five_dimensions", "aesthetics", "lighting"))):
            lighting_missing.append(shot_no)
        if production_contract_present:
            if not _present(
                    shot, ("script_reference", "script_excerpt")):
                script_reference_missing.append(shot_no)
            if not isinstance(shot.get("five_dimensions"), Mapping):
                five_dimensions_missing.append(shot_no)
            if not (
                    isinstance(shot.get("start_state"), Mapping)
                    and isinstance(shot.get("end_state"), Mapping)):
                start_end_state_missing.append(shot_no)

    scenes: list[SceneDramaticPlan] = []
    for scene_no, items in sorted(scene_shots.items()):
        dramatic_function = next((
            _text(item.get("dramatic_function") or item.get("story_function")
                  or item.get("shot_function"))
            for item in items
            if _text(item.get("dramatic_function") or item.get("story_function")
                     or item.get("shot_function"))
        ), "待补场次戏剧功能")
        scenes.append(SceneDramaticPlan(
            scene_no=scene_no,
            dramatic_function=dramatic_function,
            shot_count=len(items),
            advice="核对本场起承转合与下场钩连。",
        ))

    completeness_advice: list[str] = []
    if dialogue_missing:
        completeness_advice.append("补齐镜头台词输入。")
    if beat_missing:
        completeness_advice.append("补齐动作节拍输入。")
    if lighting_missing:
        completeness_advice.append("补齐光影输入。")
    if script_reference_missing:
        completeness_advice.append("补齐镜头对应的剧本事件。")
    if five_dimensions_missing:
        completeness_advice.append("补齐五维分镜输入。")
    if start_end_state_missing:
        completeness_advice.append("补齐镜头起止状态。")
    overall_advice: list[str] = []
    if repetitions:
        overall_advice.append("复核相邻镜头重复。")
    if completeness_advice:
        overall_advice.append("补齐导演输入后再精修建议。")
    if not overall_advice:
        overall_advice.append("整集导演输入已具备，可继续人工审片。")
    return EpisodeDirectorPlan(
        schema=DIRECTOR_PLAN_SCHEMA,
        kind=REVIEW_KIND,
        production_blocking=PRODUCTION_BLOCKING,
        episode_id=_required_text(episode_id, field="episode_id"),
        shot_count=len(normalized),
        total_duration_seconds=round(total_duration, 3),
        shot_scale_distribution=_distribution(scale_counter),
        camera_movement_distribution=_distribution(movement_counter),
        shot_function_distribution=_distribution(function_counter),
        adjacent_repetitions=tuple(repetitions),
        scenes=tuple(scenes),
        input_completeness=InputCompleteness(
            dialogue_missing=tuple(dialogue_missing),
            beat_missing=tuple(beat_missing),
            lighting_missing=tuple(lighting_missing),
            script_reference_missing=tuple(script_reference_missing),
            five_dimensions_missing=tuple(five_dimensions_missing),
            start_end_state_missing=tuple(start_end_state_missing),
            advice=tuple(completeness_advice),
        ),
        advice=tuple(overall_advice),
    )


@dataclass(frozen=True)
class NineGridCell:
    """九宫格中的一格只对应一个镜头。"""

    shot_no: int
    scene_no: int
    unit_id: str
    keyframe_uri: str
    keyframe_status: str
    duration_seconds: float
    shot_scale: str
    camera_movement: str
    shot_function: str
    script_reference: str
    visual_hook: str
    dialogue: str
    high_value_event_id: str
    event_role: str
    event_beat_ids: tuple[str, ...]
    must_visualize: bool


@dataclass(frozen=True)
class NineGridPage:
    """同一场的一页浏览板；不是生成参考资产。"""

    schema: str
    kind: str
    production_blocking: bool
    scene_no: int
    page_no: int
    cells: tuple[NineGridCell, ...]
    max_cells: int
    render_mode: str
    generates_reference_asset: bool
    single_image_multi_panel: bool
    reference_chain_eligible: bool
    view_only: bool
    advice: tuple[str, ...]


@dataclass(frozen=True)
class NineGridBrowser:
    """Serializable page index for human browsing, never an asset manifest."""

    schema: str
    kind: str
    production_blocking: bool
    source_storyboard_version: str
    shot_count: int
    page_count: int
    render_mode: str
    generates_reference_asset: bool
    single_image_multi_panel: bool
    reference_chain_eligible: bool
    view_only: bool
    pages: tuple[NineGridPage, ...]
    advice: tuple[str, ...]


def _dialogue_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return _text(value.get("dialogue") or value.get("text"))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return " ".join(_dialogue_text(item) for item in value).strip()
    return _text(value)


def _duration(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return round(max(0.0, number), 3)


def build_nine_grid_pages(
        shots: Sequence[Mapping[str, Any]],
) -> tuple[NineGridPage, ...]:
    """按场分组，每页最多九个独立镜头浏览格。"""
    if isinstance(shots, (str, bytes)):
        raise ValueError("shots 必须是镜头列表")
    grouped: dict[int, list[NineGridCell]] = defaultdict(list)
    for index, shot in enumerate(shots, 1):
        if not isinstance(shot, Mapping):
            continue
        shot_no = _shot_no(shot, index)
        scene_no = _scene_no(shot, 1)
        grouped[scene_no].append(NineGridCell(
            shot_no=shot_no,
            scene_no=scene_no,
            unit_id=_text(shot.get("unit_id"), f"U{shot_no:02d}"),
            keyframe_uri=_text(
                shot.get("keyframe_uri") or shot.get("image_uri")),
            keyframe_status=(
                "ready" if _text(
                    shot.get("keyframe_uri") or shot.get("image_uri"))
                else "pending"),
            duration_seconds=_duration(shot.get("duration")),
            shot_scale=_text(
                shot.get("shot_scale") or shot.get("shot_size")
                or _nested_shot_text(
                    shot,
                    ("shot_contract", "景别"),
                    ("five_dimensions", "camera_design", "shot_scale")),
                "未标注"),
            camera_movement=_text(
                shot.get("camera_movement") or shot.get("camera_move")
                or _nested_shot_text(
                    shot,
                    ("shot_contract", "运镜"),
                    ("five_dimensions", "camera_design", "movement")),
                "固定"),
            shot_function=_text(
                shot.get("shot_function") or shot.get("dramatic_function"),
                "未标注"),
            script_reference=_text(
                shot.get("script_reference") or shot.get("script_excerpt")),
            visual_hook=_text(shot.get("visual_hook")),
            dialogue=_dialogue_text(
                shot.get("dialogue") or shot.get("lines")),
            high_value_event_id=_text(
                shot.get("high_value_event_id")
                or shot.get("dramatic_sequence_id")),
            event_role=_text(shot.get("event_role"), "routine"),
            event_beat_ids=tuple(dict.fromkeys(
                str(value).strip()
                for value in (
                    shot.get("event_beat_ids")
                    if isinstance(shot.get("event_beat_ids"), list)
                    else [shot.get("event_beat_id")]
                )
                if str(value or "").strip())),
            must_visualize=bool(shot.get("must_visualize")),
        ))

    pages: list[NineGridPage] = []
    for scene_no, cells in sorted(grouped.items()):
        for page_index, start in enumerate(
                range(0, len(cells), MAX_GRID_CELLS), 1):
            page_cells = tuple(cells[start:start + MAX_GRID_CELLS])
            pages.append(NineGridPage(
                schema=NINE_GRID_SCHEMA,
                kind=REVIEW_KIND,
                production_blocking=PRODUCTION_BLOCKING,
                scene_no=scene_no,
                page_no=page_index,
                cells=page_cells,
                max_cells=MAX_GRID_CELLS,
                render_mode="independent_shot_cells",
                generates_reference_asset=False,
                single_image_multi_panel=False,
                reference_chain_eligible=False,
                view_only=True,
                advice=("每格只看一镜。", "只审节奏，不作生成参考图。"),
            ))
    return tuple(pages)


def _keyframe_uri_map(keyframes: Any) -> dict[int, str]:
    result: dict[int, str] = {}
    if isinstance(keyframes, Mapping):
        values = keyframes.items()
        for raw_no, value in values:
            try:
                shot_no = _positive_int(raw_no, field="shot_no")
            except ValueError:
                continue
            uri = _text(
                value.get("uri") or value.get("keyframe_uri")
                or value.get("image_uri")) if isinstance(
                    value, Mapping) else _text(value)
            if uri:
                result[shot_no] = uri
        return result
    if isinstance(keyframes, Sequence) and not isinstance(
            keyframes, (str, bytes)):
        for index, value in enumerate(keyframes, 1):
            if not isinstance(value, Mapping):
                continue
            shot_no = _shot_no(value, index)
            uri = _text(
                value.get("uri") or value.get("keyframe_uri")
                or value.get("image_uri"))
            if uri:
                result[shot_no] = uri
    return result


def build_nine_grid_browser(
        storyboard: Mapping[str, Any],
        *,
        keyframes: Any = (),
        storyboard_version: Any = "",
) -> NineGridBrowser:
    """Build browser-only pages from a saved storyboard and keyframe rows."""
    if not isinstance(storyboard, Mapping):
        raise ValueError("storyboard 必须是对象")
    uri_by_shot = _keyframe_uri_map(keyframes)
    shots: list[dict[str, Any]] = []
    for index, raw in enumerate(storyboard.get("shots") or (), 1):
        if not isinstance(raw, Mapping):
            continue
        shot = copy.deepcopy(dict(raw))
        shot_no = _shot_no(shot, index)
        if uri_by_shot.get(shot_no):
            shot["keyframe_uri"] = uri_by_shot[shot_no]
        shots.append(shot)
    pages = build_nine_grid_pages(shots)
    return NineGridBrowser(
        schema=NINE_GRID_INDEX_SCHEMA,
        kind=REVIEW_KIND,
        production_blocking=PRODUCTION_BLOCKING,
        source_storyboard_version=_text(
            storyboard_version
            or storyboard.get("storyboard_version")
            or storyboard.get("version"), "unknown"),
        shot_count=len(shots),
        page_count=len(pages),
        render_mode="independent_shot_cells",
        generates_reference_asset=False,
        single_image_multi_panel=False,
        reference_chain_eligible=False,
        view_only=True,
        pages=pages,
        advice=(
            "九宫格只用于逐镜浏览与节奏审片。",
            "禁止把页面导出成单图拼贴或加入人物、场景、分镜参考链。",
        ),
    )


def build_storyboard_review_documents(
        *,
        episode_id: Any,
        storyboard: Mapping[str, Any],
        keyframes: Any = (),
        storyboard_version: Any = "",
) -> dict[str, Any]:
    """Create the two documents persisted after storyboard/keyframe updates."""
    if not isinstance(storyboard, Mapping):
        raise ValueError("storyboard 必须是对象")
    shots = storyboard.get("shots") or ()
    if isinstance(shots, (str, bytes)) or not isinstance(shots, Sequence):
        raise ValueError("storyboard.shots 必须是镜头列表")
    director_review = summarize_episode_directing(
        episode_id=episode_id, shots=shots)
    browser = build_nine_grid_browser(
        storyboard,
        keyframes=keyframes,
        storyboard_version=storyboard_version)
    return {
        "schema": STORYBOARD_REVIEW_DOCUMENTS_SCHEMA,
        "kind": REVIEW_KIND,
        "production_blocking": PRODUCTION_BLOCKING,
        "director_review": review_document(director_review),
        "nine_grid_browser": review_document(browser),
    }
