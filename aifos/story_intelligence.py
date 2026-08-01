"""AI 编剧与导演的非阻断策略合同。

本模块不调用模型、不读写数据库，也不改变生产状态。它只负责把独立剧本
评审、双稿竞稿、跨集连续性、整集导演规划和九宫格浏览组织成稳定数据：

* 评审结论一律是 ``review/advice``，不得成为生产闸门；
* 编剧生成运行不能把自己的自评分冒充独立评审；
* 九宫格是一页最多九个独立镜头的浏览模型，绝不生成单图多格参考资产。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence


STORY_REVIEW_SCHEMA = "aifos.story-review/v1"
DRAFT_DECISION_SCHEMA = "aifos.draft-decision/v1"
CONTINUITY_INPUT_SCHEMA = "aifos.episode-continuity-input/v1"
DIRECTOR_PLAN_SCHEMA = "aifos.episode-director-plan/v1"
NINE_GRID_SCHEMA = "aifos.nine-grid-browser/v1"

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
    hooks = tuple(
        str(item).strip() for item in (unresolved_hooks or ())
        if str(item or "").strip()
    )
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
    advice: tuple[str, ...]


@dataclass(frozen=True)
class EpisodeDirectorPlan:
    """整集镜头规划摘要，只提出导演建议。"""

    schema: str
    kind: str
    production_blocking: bool
    episode_id: str
    shot_count: int
    shot_scale_distribution: tuple[tuple[str, int], ...]
    camera_movement_distribution: tuple[tuple[str, int], ...]
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
    repetitions: list[AdjacentShotRepetition] = []
    scene_shots: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    dialogue_missing: list[int] = []
    beat_missing: list[int] = []
    lighting_missing: list[int] = []
    previous: Optional[tuple[int, str, str]] = None

    for index, shot in enumerate(normalized, 1):
        shot_no = _shot_no(shot, index)
        scene_no = _scene_no(shot, 1)
        scale = _text(
            shot.get("shot_scale") or shot.get("shot_size") or shot.get("景别"),
            "未标注",
        )
        movement = _text(
            shot.get("camera_movement") or shot.get("camera_move")
            or shot.get("运镜"),
            "固定",
        )
        scale_counter[scale] += 1
        movement_counter[movement] += 1
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
        if not _present(shot, ("dialogue", "lines", "台词")):
            dialogue_missing.append(shot_no)
        if not _present(shot, ("beat", "beats", "timing", "节拍")):
            beat_missing.append(shot_no)
        if not _present(shot, ("lighting", "light", "光影")):
            lighting_missing.append(shot_no)

    scenes: list[SceneDramaticPlan] = []
    for scene_no, items in sorted(scene_shots.items()):
        dramatic_function = next((
            _text(item.get("dramatic_function") or item.get("story_function"))
            for item in items
            if _text(item.get("dramatic_function") or item.get("story_function"))
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
        shot_scale_distribution=_distribution(scale_counter),
        camera_movement_distribution=_distribution(movement_counter),
        adjacent_repetitions=tuple(repetitions),
        scenes=tuple(scenes),
        input_completeness=InputCompleteness(
            dialogue_missing=tuple(dialogue_missing),
            beat_missing=tuple(beat_missing),
            lighting_missing=tuple(lighting_missing),
            advice=tuple(completeness_advice),
        ),
        advice=tuple(overall_advice),
    )


@dataclass(frozen=True)
class NineGridCell:
    """九宫格中的一格只对应一个镜头。"""

    shot_no: int
    scene_no: int
    keyframe_uri: str
    shot_scale: str
    camera_movement: str
    dialogue: str


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
    advice: tuple[str, ...]


def _dialogue_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return _text(value.get("dialogue") or value.get("text"))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return " ".join(_dialogue_text(item) for item in value).strip()
    return _text(value)


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
            keyframe_uri=_text(
                shot.get("keyframe_uri") or shot.get("image_uri")),
            shot_scale=_text(
                shot.get("shot_scale") or shot.get("shot_size"), "未标注"),
            camera_movement=_text(
                shot.get("camera_movement") or shot.get("camera_move"), "固定"),
            dialogue=_dialogue_text(
                shot.get("dialogue") or shot.get("lines")),
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
                advice=("每格只看一镜。", "只审节奏，不作生成参考图。"),
            ))
    return tuple(pages)

