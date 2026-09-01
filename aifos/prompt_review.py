"""Authoritative per-shot prompt review payloads.

The prompt review page is deliberately compiled from the same v2.2 contract
used by production.  It is not a second, presentation-only prompt formatter:
if a contract is BLOCK here, the corresponding generation input is not ready.
"""

import copy
import json

from .prompt_contract import (
    PROMPT_CONTRACT_SCHEMA,
    compile_shot_prompt,
    render_shot_prompt,
    shot_local_scene,
    validate_shot_prompt_contract,
)
from .story_context import attach_shot_story_context
from .workflow import production_profile, reconcile_shot_semantics


PROMPT_REVIEW_SCHEMA = "aifos.prompt-review/v1"

FRAME_VARIANTS = (
    ("keyframe", "代表性关键帧", "keyframe"),
    ("first_frame", "视频首帧", "first_frame"),
    ("last_frame", "视频尾帧", "last_frame"),
    ("video", "Seedance 2 视频", "video"),
)

# 非镜头资产同样按提示词生产，提示词区不能只覆盖镜头：一集里人物、候选、
# 道具和场景母版的提示词条数通常是镜头的两三倍，它们出错一样毁整集。
ASSET_CATEGORIES = (
    ("character_candidate", "定角候选"),
    ("character_art", "人物立绘"),
    ("character_sheet", "人物设定图"),
    ("prop_candidate", "道具候选"),
    ("prop_art", "道具母资产"),
    ("scene_art", "场景母版"),
    ("cover", "封面"),
)

PRODUCED_STATUSES = frozenset({"done", "reused"})

# 「没有记录」有三种完全不同的含义，页面必须分得开，否则用户看到一片空白
# 无法判断是还没做、还是做了但没留痕、还是这一类根本不进清单。
ACTUAL_STATES = {
    "produced": "已生产并留有实际输入",
    "pending": "尚未生产",
    "missing": "已生产但缺实际输入记录",
    "planned": "未生产，显示将要提交的输入",
    "not_tracked": "该类目不进出图清单",
}

HIGHEST_RULES = (
    "提示词合同是生产最高规则；与剧本摘要、旧提示词或模型自由发挥冲突时，以合同为准。",
    "每个正式人物必须明确姓名、性别、年龄段、当前服装与头部状态；不得用未指定或以参考图为准代替事实。",
    "静态图只能有一个显式 frame_target；缺失、回退或同时描述动作过程时直接 BLOCK。",
    "视频必须具有起点、一个主动作、一个运镜和终点；不得把多个镜头事件塞进一条生成。",
    "登记人物数、功能人物数、真人身体数与构图可见数必须精确相等。",
    "同一物理 prop_id 在同一阶段只能有一个实例和一个主位置，道具出现必须服从事件时间线。",
    "人物生命、意识、存在形态与行动能力必须互相成立，死亡或昏迷状态不得出现不可能行为。",
    "服装、头饰、妆发、伤势和持物跨首尾帧连续；没有明确过程不得自行变化。",
    "每张参考图只承担一种职责，不得跨用途覆盖人物身份、服装、场景、构图或文字。",
    "画面只允许白名单文字；除此之外禁止字幕、字幕条、Logo、水印和随机乱码。",
)


def _read_render_plan(app, project_id, episode_number):
    path = (
        app.workspace.artifacts_dir
        / f"p{int(project_id):03d}"
        / f"e{int(episode_number):03d}"
        / "render_plan.json"
    )
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _actual_items_by_shot(render_plan):
    result = {}
    for item in (render_plan or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        try:
            shot_no = int(item.get("shot_no"))
        except (TypeError, ValueError):
            continue
        category = str(item.get("category") or "")
        if category in {"shot_image", "frames"}:
            result[(shot_no, category)] = item
    return result


def _reference_manifest(item):
    if not isinstance(item, dict):
        return []
    direct = item.get("reference_manifest")
    if isinstance(direct, list):
        return [copy.deepcopy(value) for value in direct
                if isinstance(value, dict)]
    inputs = item.get("reference_inputs") or {}
    values = inputs.get("items") if isinstance(inputs, dict) else []
    return [copy.deepcopy(value) for value in values or []
            if isinstance(value, dict)]


def _actual_prompt(item, kind):
    if not isinstance(item, dict):
        return None
    if kind in {"first_frame", "last_frame"}:
        compacts = item.get("frame_prompt_compacts") or {}
        if isinstance(compacts, dict) and compacts.get(kind):
            prompt = str(compacts[kind])
        else:
            contracts = item.get("frame_prompt_contracts") or {}
            contract = contracts.get(kind) if isinstance(contracts, dict) else None
            prompt = render_shot_prompt(contract) if isinstance(contract, dict) else ""
    else:
        generation_input = item.get("generation_input") or {}
        prompt = (
            generation_input.get("prompt_compact")
            or generation_input.get("prompt")
            or item.get("prompt_compact")
            or item.get("prompt_used")
            or item.get("prompt")
            or ""
        )
    prompt = str(prompt or "").strip()
    if not prompt:
        return None
    review = item.get("prompt_review") or {}
    return {
        "prompt": prompt,
        "status": str(item.get("status") or ""),
        "provider": str(item.get("provider") or ""),
        "reviewed": bool(
            isinstance(review, dict) and review.get("approved")),
        "source": "render_plan 实际生产输入",
        "state": "produced",
    }


def _actual_state(item, actual):
    """区分「还没做」「做了没留痕」「不进清单」——空白必须可解释。"""
    if isinstance(actual, dict) and actual.get("state"):
        return str(actual["state"])
    if not isinstance(item, dict):
        return "not_tracked"
    if str(item.get("status") or "") in PRODUCED_STATUSES:
        return "missing"
    return "pending"


def _video_actual(shot, video_qc, shot_no):
    """视频不进 render_plan：已产读视频质检快照，未产回落分镜将提交稿。"""
    for row in (video_qc or {}).get("shots") or []:
        if not isinstance(row, dict):
            continue
        try:
            if int(row.get("shot_no")) != int(shot_no):
                continue
        except (TypeError, ValueError):
            continue
        generation = row.get("generation_input") or row.get("input_snapshot")
        generation = generation if isinstance(generation, dict) else {}
        prompt = str(
            generation.get("prompt_compact")
            or generation.get("prompt")
            or row.get("prompt") or "").strip()
        if prompt:
            return {
                "prompt": prompt,
                "status": str(row.get("status") or ""),
                "provider": str(row.get("provider") or ""),
                "reviewed": False,
                "source": "视频质检记录的实际输入",
                "state": "produced",
            }
    planned = str(
        shot.get("seedance_prompt_compact")
        or shot.get("seedance_prompt") or "").strip()
    if not planned:
        return None
    return {
        "prompt": planned,
        "status": "",
        "provider": "",
        "reviewed": False,
        "source": "分镜登记的 Seedance 待提交提示词",
        "state": "planned",
    }


def _asset_prompt_rows(render_plan):
    """把非镜头资产的提示词按类目整理成可审阅的行。"""
    by_category = {}
    for item in (render_plan or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "")
        if category not in dict(ASSET_CATEGORIES):
            continue
        compiled = str(item.get("prompt") or "").strip()
        sent = str(
            item.get("prompt_used") or item.get("prompt_compact") or "").strip()
        qc = item.get("qc") if isinstance(item.get("qc"), dict) else {}
        review = item.get("prompt_review")
        review = review if isinstance(review, dict) else {}
        actual = {
            "prompt": sent,
            "status": str(item.get("status") or ""),
            "provider": str(item.get("provider") or ""),
            "reviewed": bool(review.get("approved")),
            "source": "render_plan 实际发送提示词",
            "state": "produced",
        } if sent else None
        by_category.setdefault(category, []).append({
            "id": str(item.get("id") or ""),
            "category": category,
            "name": str(item.get("name") or ""),
            "label": str(item.get("label") or item.get("name") or ""),
            "status": str(item.get("status") or ""),
            "prompt": compiled,
            "prompt_hash": str(item.get("prompt_used_hash") or ""),
            "references": _reference_manifest(item),
            "actual_generation": actual,
            "actual_state": _actual_state(item, actual),
            "prompt_review_approved": bool(review.get("approved")),
            "qc_passed": qc.get("passed"),
            "qc_issues": _dedupe(qc.get("issues") or []),
        })
    groups = []
    for category, label in ASSET_CATEGORIES:
        rows = by_category.get(category)
        if not rows:
            continue
        rows.sort(key=lambda row: (row["name"], row["id"]))
        groups.append({
            "category": category,
            "label": label,
            "total": len(rows),
            "with_prompt": sum(1 for row in rows if row["prompt"]),
            "missing_prompt": sum(1 for row in rows if not row["prompt"]),
            "items": rows,
        })
    return groups


def _dedupe(values):
    return list(dict.fromkeys(
        str(value).strip() for value in values if str(value).strip()))


def _compile_variant(shot, *, kind, label, mode, location, style,
                     references, actual):
    variant = copy.deepcopy(shot)
    variant["frame_kind"] = kind
    try:
        contract, prompt = compile_shot_prompt(
            variant,
            location=location,
            style=style,
            references=references,
            mode=mode,
        )
        validation = validate_shot_prompt_contract(contract)
    except Exception as exc:  # review must show the bad shot, not hide the page
        contract = {}
        prompt = ""
        validation = {
            "passed": False,
            "status": "BLOCK",
            "severity": "BLOCK",
            "issues": [f"提示词编译失败：{exc}"],
            "warnings": [],
            "semantic_corrections": [],
        }
    return {
        "kind": kind,
        "label": label,
        "media": "video" if mode == "video" else "image",
        "status": validation.get("status") or (
            "PASS" if validation.get("passed") else "BLOCK"),
        "passed": bool(validation.get("passed")),
        "issues": _dedupe(validation.get("issues") or []),
        "warnings": _dedupe(validation.get("warnings") or []),
        "prompt": prompt,
        "contract": contract,
        "frame_target": copy.deepcopy(contract.get("frame_target") or {}),
        "references": copy.deepcopy(contract.get("references") or []),
        "actual_generation": actual,
    }


def build_episode_prompt_review(app, episode_id):
    """Compile every shot into one authoritative, inspectable review payload."""
    episode = app.projects.get_episode(episode_id)
    if episode is None:
        return None
    project_row = app.db.query_one(
        "SELECT * FROM projects WHERE id=?", (episode["project_id"],))
    project = dict(project_row) if project_row is not None else {
        "id": episode["project_id"],
        "title": "",
        "style": "",
    }
    script, script_version = app.projects.latest_document(
        episode_id, "script")
    storyboard, storyboard_version = app.projects.latest_document(
        episode_id, "storyboard")
    storyboard, contexts = attach_shot_story_context(script, storyboard)
    render_plan = _read_render_plan(
        app, project["id"], episode["number"])
    actual_items = _actual_items_by_shot(render_plan)
    video_qc, _ = app.projects.latest_document(episode_id, "video_qc_report")
    video_references = {}
    video_reference_notes = {}
    try:
        effective = app.director.effective_video_references(episode_id)
        for key, row in ((effective or {}).get("shots") or {}).items():
            row = row if isinstance(row, dict) else {}
            video_references[str(key)] = [
                copy.deepcopy(value) for value in row.get("items") or []
                if isinstance(value, dict)]
            video_reference_notes[str(key)] = {
                "mode": str(row.get("mode") or ""),
                "spatial_reference_required": bool(
                    row.get("spatial_reference_required")),
                "spatial_reference_ready": bool(
                    row.get("spatial_reference_ready")),
                "spatial_reference_reason": str(
                    row.get("spatial_reference_reason") or ""),
            }
    except Exception:  # 参考选择器不可用时页面照常打开，只是这一栏空
        video_references = {}
        video_reference_notes = {}
    scene_locations = {
        scene.get("scene_no"): scene.get("location", "")
        for scene in (script or {}).get("scenes") or []
        if isinstance(scene, dict)
    }
    standard_snapshot, _ = app.projects.latest_document(
        episode_id, "production_standard")
    if standard_snapshot is None and app.standards is not None:
        standard_snapshot = app.standards.active()
    standard_snapshot = (
        standard_snapshot if isinstance(standard_snapshot, dict) else {})
    ctx = {
        "project": project,
        "episode": dict(episode),
        "script": script or {},
        "production_standard": standard_snapshot,
        "production_profile": production_profile(
            app.config, standard_snapshot),
    }
    ctx["rule_layers"] = app.director._load_rule_layers(
        ctx["project"], ctx["episode"], standard_snapshot)
    results = []
    for raw_shot in (storyboard or {}).get("shots") or []:
        if not isinstance(raw_shot, dict):
            continue
        shot = reconcile_shot_semantics(copy.deepcopy(raw_shot))
        shot_no = int(shot.get("shot_no") or len(results) + 1)
        location = shot_local_scene(
            shot, scene_locations.get(shot.get("scene_no"), ""))
        facts = app.director._shot_character_facts(ctx, shot)
        shot["character_facts"] = copy.deepcopy(facts)
        shot["character_background"] = copy.deepcopy(facts)
        shot["character_visuals"] = app.director._shot_character_visuals(
            ctx, shot, facts)
        shot["identity_facts_required"] = bool(
            shot.get("characters"))
        shot["appearance_state_required"] = bool(
            shot.get("characters"))

        image_item = actual_items.get((shot_no, "shot_image"))
        frames_item = actual_items.get((shot_no, "frames"))
        variants = []
        for kind, label, mode in FRAME_VARIANTS:
            source_item = (
                frames_item if kind in {"first_frame", "last_frame"}
                else image_item if kind == "keyframe"
                else None
            )
            manifest = _reference_manifest(source_item)
            if kind == "video":
                # 视频不进 render_plan，参考图要问视频参考选择器，否则这一栏
                # 结构性永远空白——用户只会以为“视频没有参考图”。
                manifest = video_references.get(str(shot_no)) or []
                actual = _video_actual(shot, video_qc, shot_no)
            else:
                actual = _actual_prompt(source_item, kind)
            variant = _compile_variant(
                shot,
                kind=kind,
                label=label,
                mode=mode,
                location=location,
                style=str(project.get("style") or ""),
                references=_reference_manifest(source_item),
                actual=actual,
            )
            variant["submitted_references"] = manifest
            variant["actual_state"] = (
                "not_tracked"
                if kind == "video" and actual is None
                else _actual_state(source_item, actual))
            if kind == "video":
                variant["reference_notes"] = video_reference_notes.get(
                    str(shot_no), {})
            variants.append(variant)
        statuses = [item["status"] for item in variants]
        status = (
            "BLOCK" if "BLOCK" in statuses
            else "WARN" if "WARN" in statuses
            else "PASS"
        )
        issues = _dedupe(
            f"{item['label']}：{issue}"
            for item in variants for issue in item["issues"])
        warnings = _dedupe(
            f"{item['label']}：{warning}"
            for item in variants for warning in item["warnings"])
        story_context = contexts.get(shot_no, {}) if isinstance(
            contexts, dict) else {}
        try:
            effective_rule_stack = app.director._resolve_effective_rules(
                ctx, shot=shot, stage="prompt_review",
                modality="image").as_dict()
        except Exception as exc:
            effective_rule_stack = {
                "schema": "aifos.rule-scope/error",
                "final_rules": {}, "sources": {}, "overridden": [],
                "fingerprint": "", "error": str(exc),
            }
        results.append({
            "shot_no": shot_no,
            "unit_id": shot.get("unit_id"),
            "scene_no": shot.get("scene_no"),
            "duration": shot.get("duration"),
            "location": location,
            "characters": list(shot.get("characters") or []),
            "status": status,
            "issues": issues,
            "warnings": warnings,
            "script_reference": (
                shot.get("script_reference")
                or story_context.get("script_reference")
                or ""
            ),
            "description": str(
                shot.get("description") or shot.get("action") or ""),
            "story_phase": str(shot.get("story_phase") or "present"),
            "active_realm_id": str(shot.get("active_realm_id") or ""),
            "era_context": str(shot.get("era_context") or shot.get("era")
                               or ""),
            "effective_rule_stack": effective_rule_stack,
            "raw_storyboard_prompt": str(shot.get("prompt") or ""),
            "character_facts": copy.deepcopy(facts),
            "variants": variants,
        })

    counts = {
        status: sum(1 for item in results if item["status"] == status)
        for status in ("PASS", "WARN", "BLOCK")
    }
    variant_counts = {
        status: sum(
            1 for shot in results for item in shot["variants"]
            if item["status"] == status)
        for status in ("PASS", "WARN", "BLOCK")
    }
    overall = (
        "BLOCK" if counts["BLOCK"]
        else "WARN" if counts["WARN"]
        else "PASS" if results
        else "BLOCK"
    )
    asset_groups = _asset_prompt_rows(render_plan)
    asset_total = sum(group["total"] for group in asset_groups)
    asset_missing = sum(group["missing_prompt"] for group in asset_groups)
    return {
        "schema": PROMPT_REVIEW_SCHEMA,
        "contract_schema": PROMPT_CONTRACT_SCHEMA,
        "rule_priority": "highest",
        "rule_title": "提示词制作规则＝AIFOS 生产最高规则",
        "rules": list(HIGHEST_RULES),
        "episode": {
            "id": int(episode["id"]),
            "number": int(episode["number"]),
            "title": episode["title"] or "",
            "status": episode["status"] or "",
        },
        "project": {
            "id": int(project["id"]),
            "title": project.get("title") or "",
            "style": project.get("style") or "",
            "aspect": project.get("aspect") or "",
        },
        "document_versions": {
            "script": script_version,
            "storyboard": storyboard_version,
        },
        "actual_states": dict(ACTUAL_STATES),
        "summary": {
            "status": overall,
            "ready": overall != "BLOCK" and bool(results),
            "shots_total": len(results),
            "shots": counts,
            "prompts_total": len(results) * len(FRAME_VARIANTS),
            "prompts": variant_counts,
            "assets_total": asset_total,
            "assets_missing_prompt": asset_missing,
        },
        "shots": results,
        "assets": asset_groups,
    }
