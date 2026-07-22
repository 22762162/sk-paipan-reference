"""AI 导演中心:总控。拆解任务、调度 Provider、控制流程与成本。

生产流程(SK 漫剧工业流):
  需求 → 剧本 → 连续性圣经 → 五维分镜 → 空间调度图 → 关键帧/文字锁定 → 首尾帧
       → 生产门禁 → Seedance2 视频(随视频声音/口型) → 剪辑
       → 抽帧检查板 + 内容复核 + 交付脚本 → 包装 → 数据沉淀
"""

import hashlib
import json
import time
from pathlib import Path

from .db import now
from .errors import (AifosError, BudgetExceeded, ProduceCancelled,
                     ProviderError, ProviderUnavailable)
from .image_acceleration import ImageAccelerationStore
from .quality_policy import (
    default_quality_policy,
    formal_reference_allowed,
    image_task_class_for,
    normalize_quality,
    normalize_quality_policy,
    recommend_asset_quality,
    recommend_shot_image_quality,
    resolve_image_quality,
    resolve_video_quality,
    set_policy_choices,
)
from .spatial_blocking import (build_spatial_plan, shot_blocking,
                               write_spatial_svgs)
from .workflow import (
    PIPELINE_VERSION,
    build_content_review,
    build_continuity_bible,
    build_preflight,
    enrich_storyboard,
    lock_text_assets,
    production_profile,
    write_delivery_verifier,
    write_review_board,
)

# 画幅 → 像素尺寸(视频/图片);封面用竖版比例
ASPECT_DIMS = {
    "9:16": {"width": 1080, "height": 1920},
    "16:9": {"width": 1920, "height": 1080},
}

MODERN_OTOME_STYLE = (
    "现代都市乙女游戏CG，精致3D半写实角色渲染，亚洲当代青年，"
    "现代发型与时尚通勤服装，清透自然皮肤，细腻五官，柔和电影灯光，"
    "高级时尚杂志质感；禁止古装、汉服、发簪、长袍、水墨、国风、"
    "2D平涂、动漫线稿和历史建筑"
)
DEFAULT_VISUAL_STYLE = (
    "现代都市精品漫剧，电影级半写实人物与场景，现代服装和现代建筑，"
    "自然皮肤质感，细腻灯光；禁止古装、汉服和历史场景"
)


def infer_visual_style(premise="", project_title=""):
    """把明确时代/媒介要求提升为项目级视觉风格。

    剧情前提优先于标题；标题只在前提没有给出视觉方向时兜底，避免
    《万妖图录》这类明确仙侠标题被现代默认值改成都市题材。
    """
    text = (premise or "").lower()
    if any(word in text for word in ("乙女", "3d", "现代", "时尚", "都市")):
        return MODERN_OTOME_STYLE
    if any(word in text for word in ("古装", "仙侠", "武侠", "国风")):
        return ("国风漫剧，精致2D动画质感，服装与建筑严格符合剧情时代，"
                "高细节，统一人物造型")
    title = (project_title or "").lower()
    if any(word in title for word in ("仙侠", "修仙", "妖", "灵", "剑", "宗门")):
        return ("国风漫剧，精致2D动画质感，服装与建筑严格符合剧情时代，"
                "高细节，统一人物造型")
    return DEFAULT_VISUAL_STYLE

STAGES = [
    ("script", "剧本"),
    ("continuity", "连续性圣经"),
    ("cast", "人物/场景图"),
    ("storyboard", "五维分镜"),
    ("blocking", "空间调度图"),
    ("images", "关键帧"),
    ("text_assets", "文字资产锁定"),
    ("frames", "首尾帧"),
    ("preflight", "生产门禁"),
    ("videos", "Seedance视频"),
    ("voices", "Seedance2随视频声音/口型"),
    ("edit", "剪映剪辑"),
    ("qc", "三层质检"),
    ("package", "封面/标题/拆条"),
    ("archive", "数据沉淀"),
]

# 预生产检查点:此阶段完成后可暂停等待用户确认,
# 确认后才进入视频生产(真实产线从这里开始消耗即梦额度)
CONFIRM_AFTER = "preflight"
CHARACTER_CANDIDATES = 5

# 人物完整资产套件:立绘之外每个角色补齐的生产级设定资产
# (项目级,跨集复用;全部以立绘和用户参考图为基准保证同一形象)
CHARACTER_SHEETS = [
    ("turnaround", "四视图",
     "标准四视图设定:正面/侧面/背面/四分之三视角并排一张图,"
     "全身等比例,发型服装配色完全一致"),
    ("closeup", "面部特写",
     "面部大特写:五官、发际线、瞳色细节清晰,中性表情"),
    ("features", "特征设定",
     "辨识特征拆解:发型、瞳色、体态、标志性配饰逐项放大标注"),
    ("makeup", "妆容设定",
     "妆容细节:底妆、眉眼妆、唇色、特殊纹样,正面半身"),
    ("costume", "服装设定",
     "全身服装设定:正面站姿,服装配色、材质与层次清晰完整"),
    ("costume_detail", "服装细节",
     "服装细节拆解:纹样、扣饰、腰带、鞋履、佩饰逐项放大展示"),
]

IMAGE_ASSET_KINDS = {
    "character_art", "character_sheet", "scene_art", "character_candidate",
    "image", "first_frame", "last_frame", "cover", "reference",
}


class Director:
    def __init__(self, db, config, logger, projects, assets, router, qc, ops,
                 data_center, artifacts_root, standards=None):
        self.db = db
        self.config = config
        self.log = logger
        self.projects = projects
        self.assets = assets
        self.router = router
        self.qc = qc
        self.ops = ops
        self.data = data_center
        self.artifacts_root = Path(artifacts_root)
        self.standards = standards
        self.image_acceleration = ImageAccelerationStore(db)

    def _resolve_standard_snapshot(self, episode_id, force=False):
        """为一集绑定不可漂移的制作标准。

        新集与强制重做读取当前生效标准；确认续产和普通断点续产始终恢复
        本集首次绑定的快照，避免出现旧分镜搭配新声画参数的混合产线。
        """
        if not force:
            existing, _ = self.projects.latest_document(
                episode_id, "production_standard")
            if existing is not None:
                return existing
        if self.standards is None:
            snapshot = {
                "profile_key": "sk-manju-v5",
                "version": 1,
                "version_id": 0,
                "name": "SK 五维漫剧标准",
                "fingerprint": "legacy-config",
                "content": {},
            }
        else:
            snapshot = self.standards.active()
        self.projects.save_document(
            episode_id, "production_standard", snapshot)
        return snapshot

    def _episode_quality_policy(self, episode_id, *, persist=False):
        policy, _ = self.projects.latest_document(episode_id, "quality_policy")
        normalized = normalize_quality_policy(policy)
        if persist and policy is None:
            self.projects.save_document(
                episode_id, "quality_policy", normalized)
        return normalized

    def update_quality_policy(self, episode_id, *, image_default=None,
                              video_default=None, image_overrides=None,
                              video_overrides=None):
        """保存本集自动/手动质量选择，供续产和逐镜审计复用。"""
        episode = self.projects.get_episode(int(episode_id))
        if episode is None:
            raise AifosError("剧集不存在")
        current = self._episode_quality_policy(episode["id"])
        policy = set_policy_choices(
            current, image_default=image_default,
            video_default=video_default,
            image_overrides=image_overrides,
            video_overrides=video_overrides)
        self.projects.save_document(episode["id"], "quality_policy", policy)
        return policy

    # ---- 入口:一句话开工 ----
    def produce(self, project_title, episode_number, premise="", style="",
                force=False, script=None, pause_for_confirm=False,
                kind=None, feedback="", run_id=None):
        """force=False 时增量生产:已有且落盘完好的产物直接复用,
        只补齐缺失部分——真实产线(即梦按镜头计费)断点续产的关键。
        script:用户自带剧本(标准 JSON);提供时跳过 AI 编剧,
        人物/场次/分镜等全部从该剧本自动推导。
        pause_for_confirm=True:剧本确认后先生成每人5张定妆候选并暂停，
        所有人物人工锁定后才继续五维分镜、关键帧、首尾帧和门禁；确认后再次调用
        produce(不带该参数)即从断点继续自动完成 Seedance 声画、无字幕剪辑
        与三层质检。"""
        if script is not None:
            force = True  # 剧本变了,旧镜头/配音不可复用
        existing_project = self.projects.get_project(project_title)
        requested_style = (style or "").strip()
        visual_style = (requested_style
                        or (existing_project["style"]
                            if existing_project and existing_project["style"]
                            else infer_visual_style(premise, project_title)))
        project, created = self.projects.get_or_create_project(
            project_title, style=visual_style,
            kind=kind if kind in ("drama", "idol") else "drama")
        updates = {}
        if (not created and kind in ("drama", "idol")
                and project["kind"] != kind):
            updates["kind"] = kind
        if (not created and visual_style
                and (requested_style or not project["style"])
                and project["style"] != visual_style):
            updates["style"] = visual_style
        if updates:
            # 用户明确改了内容类型/画风，或旧项目尚未锁定画风。
            project = self.projects.update_project(project_title, **updates)
        episode, _ = self.projects.get_or_create_episode(
            project["id"], episode_number, premise=premise)
        self.log.info(
            "director",
            f"开始制作《{project_title}》第{episode_number}集 "
            f"(episode_id={episode['id']},force={force})")

        standard_snapshot = self._resolve_standard_snapshot(
            episode["id"], force=force)
        profile = production_profile(self.config, standard_snapshot)
        aspect = (project["aspect"]
                  or self.config.get("defaults", "aspect", default="9:16"))
        ctx = {
            "project": dict(project),
            "episode": dict(episode),
            "out_root": self._episode_dir(project, episode),
            "force": force,
            "aspect": aspect,
            "dims": ASPECT_DIMS.get(aspect, ASPECT_DIMS["9:16"]),
            "provided_script": script,
            "feedback": feedback,
            "production_standard": standard_snapshot,
            "production_profile": profile,
            "quality_policy": self._episode_quality_policy(
                episode["id"], persist=True),
            "run_id": run_id,
        }
        stage_reports = []
        failed = False
        paused = ""
        for stage, stage_cn in STAGES:
            if self._cancel_requested(ctx):
                paused = "cancelled"
                break
            try:
                report = self._run_stage(stage, stage_cn, ctx)
            except ProduceCancelled:
                paused = "cancelled"
                break
            stage_reports.append(report)
            if report["status"] == "failed":
                failed = True
                break
            # 第一道确认:新生成的剧本先给用户过目,确认后才开始画图
            if (pause_for_confirm and stage == "script"
                    and ctx.get("script_is_new")):
                paused = "script"
                break
            # 第二道确认:每名角色5张候选必须人工选定1张。没有最终立绘时，
            # 禁止继续生成资产套件、分镜画面和首尾帧。
            if (stage == "cast" and ctx.get("cast_selection_required")):
                paused = "cast"
                break
            if pause_for_confirm and stage == CONFIRM_AFTER:
                paused = "preflight"
                break

        episode = self.projects.get_episode(episode["id"])
        if failed:
            self.projects.set_episode_status(episode["id"], "failed")
        elif paused == "cancelled":
            # 手动停止:安全落回最近的可调整检查点
            gate_done = self.db.query_one(
                "SELECT COUNT(*) AS n FROM tasks WHERE episode_id=? "
                "AND stage=? AND status='done'",
                (episode["id"], CONFIRM_AFTER))
            script_doc, _ = self.projects.latest_document(
                episode["id"], "script")
            selection = self.character_selection_status(
                ctx["project"]["id"],
                (script_doc or {}).get("characters", []))
            candidates_started = any(
                item.get("candidate_count", 0)
                for item in selection.get("characters", []))
            landing = ("awaiting_confirm" if gate_done and gate_done["n"]
                       else "awaiting_cast" if (
                           selection.get("required") and candidates_started)
                       else "awaiting_script" if script_doc else "created")
            self.projects.set_episode_status(episode["id"], landing)
            self.log.info(
                "director",
                f"已手动停止生成,回到「{landing}」;调整后确认即可继续")
        elif paused == "script":
            self.projects.set_episode_status(
                episode["id"], "awaiting_script")
            self.log.info(
                "director",
                f"剧本已生成,等待确认后再画人物/场景/分镜"
                f"(episode_id={episode['id']})")
        elif paused == "cast":
            self.projects.set_episode_status(
                episode["id"], "awaiting_cast")
            self.log.info(
                "director",
                "人物候选已生成，等待逐个选定最终立绘；"
                "全部锁定前不生成后续图片"
                f"(episode_id={episode['id']})")
        elif paused:
            self.projects.set_episode_status(
                episode["id"], "awaiting_confirm")
            self.log.info(
                "director",
                f"预生产完成,等待确认后进入视频生产"
                f"(episode_id={episode['id']})")
        elif not ctx.get("qc_report", {}).get("passed", True):
            self.projects.set_episode_status(episode["id"], "qc_failed")
        else:
            self.projects.set_episode_status(episode["id"], "done")
        episode = self.projects.get_episode(episode["id"])

        summary = {
            "project": project_title,
            "episode": episode_number,
            "status": episode["status"],
            "qc_score": episode["qc_score"],
            "cost": round(episode["cost"], 2),
            "budget": self.config.get("budget", "per_episode", default=0),
            "artifacts_dir": str(ctx["out_root"]),
            "stages": stage_reports,
            "outputs": {
                "final": ctx.get("final_uri", ""),
                "cover": ctx.get("cover_uri", ""),
                "titles": ctx.get("titles", []),
                "clips": [c["uri"] for c in ctx.get("clips", [])],
                "publish": ctx.get("publish_kit", {}).get("uri", ""),
            },
            "aspect": ctx["aspect"],
            "production_standard": {
                "profile_key": standard_snapshot.get("profile_key", ""),
                "version": standard_snapshot.get("version"),
                "version_id": standard_snapshot.get("version_id"),
                "name": standard_snapshot.get("name", ""),
                "fingerprint": standard_snapshot.get("fingerprint", ""),
            },
        }
        (ctx["out_root"] / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8")
        self.log.info(
            "director",
            f"《{project_title}》第{episode_number}集 制作结束:"
            f"{episode['status']},质检 {episode['qc_score']},"
            f"成本 {episode['cost']:.2f}")
        return summary

    def _episode_dir(self, project, episode):
        path = (self.artifacts_root / f"p{project['id']:03d}"
                / f"e{episode['number']:03d}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---- 阶段调度:每个阶段落任务表,统一异常与成本记账 ----
    def _run_stage(self, stage, stage_cn, ctx):
        episode_id = ctx["episode"]["id"]
        ts = now()
        cur = self.db.execute(
            "INSERT INTO tasks(episode_id, run_id, stage, name, status, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (episode_id, ctx.get("run_id"), stage, stage_cn, "running", ts,
             ts))
        task_id = cur.lastrowid
        # 用户点了停止(状态=cancelling)时不要覆盖停止信号
        current = self.projects.get_episode(episode_id)
        if current is None or current["status"] != "cancelling":
            self.projects.set_episode_status(episode_id, stage)
        self._task_cost = 0.0
        self._task_providers = set()
        try:
            result = getattr(self, f"_stage_{stage}")(ctx)
            self.db.execute(
                "UPDATE tasks SET status='done', provider=?, cost=?, "
                "result=?, updated_at=? WHERE id=?",
                (",".join(sorted(self._task_providers)), self._task_cost,
                 json.dumps(result or {}, ensure_ascii=False)[:4000],
                 now(), task_id))
            return {"stage": stage, "name": stage_cn, "status": "done",
                    "cost": round(self._task_cost, 2),
                    "providers": sorted(self._task_providers),
                    "detail": result or {}}
        except ProduceCancelled:
            self.db.execute(
                "UPDATE tasks SET status='stopped', provider=?, cost=?, "
                "error=?, updated_at=? WHERE id=?",
                (",".join(sorted(self._task_providers)), self._task_cost,
                 "已手动停止", now(), task_id))
            raise
        except Exception as exc:
            self.db.execute(
                "UPDATE tasks SET status='failed', provider=?, cost=?, "
                "error=?, updated_at=? WHERE id=?",
                (",".join(sorted(self._task_providers)), self._task_cost,
                 str(exc)[:1000], now(), task_id))
            import traceback
            trace = traceback.format_exc(limit=3).strip()
            self.log.error(
                "director",
                f"阶段 {stage} 失败: {exc}\n{trace[-600:]}")
            return {"stage": stage, "name": stage_cn, "status": "failed",
                    "cost": round(self._task_cost, 2), "error": str(exc)}

    def _cancel_requested(self, ctx):
        """用户是否在 Web/CLI 点了「停止生成」(状态置为 cancelling)。"""
        row = self.projects.get_episode(ctx["episode"]["id"])
        return row is not None and row["status"] == "cancelling"

    def _call(self, ctx, capability, payload, sub_dir):
        """经由路由器调用 Provider,并做预算与成本记账。"""
        if self._cancel_requested(ctx):
            raise ProduceCancelled("已手动停止生成")
        episode = self.projects.get_episode(ctx["episode"]["id"])
        budget = self.config.get("budget", "per_episode", default=0)
        if budget and episode["cost"] >= budget:
            raise BudgetExceeded(
                f"单集成本 {episode['cost']:.2f} 已达预算 {budget},停止调度")
        result = self.router.call(
            capability, payload, ctx["out_root"] / sub_dir,
            cancel=lambda: self._cancel_requested(ctx))
        self._task_cost += result.cost
        self._task_providers.add(result.provider)
        self.projects.add_episode_cost(ctx["episode"]["id"], result.cost)
        return result

    # ---- 增量复用:已有资产落盘完好则直接使用 ----
    def _existing_asset_uri(self, ctx, kind, name):
        if ctx.get("force"):
            return None
        row = self.assets.latest(ctx["project"]["id"], kind, name)
        if row is None or not row["uri"]:
            return None
        uri = row["uri"]
        if uri.startswith("http://") or uri.startswith("https://"):
            return uri
        return uri if Path(uri).exists() else None

    def _shot_name(self, ctx, shot_no):
        return f"e{ctx['episode']['number']:03d}_shot{shot_no:03d}"

    def _line_name(self, ctx, line_no):
        return f"e{ctx['episode']['number']:03d}_line{line_no:03d}"

    # ---- 图片生产清单:每张图的分类/提示词/实时状态(Web 实时可见) ----
    # 每类资产套件重点采用的人物设定字段(全用会超长,按图取材)
    SHEET_DESIGN_KEYS = {
        "turnaround": ("species", "appearance", "hair", "costume",
                       "palette"),
        "closeup": ("species", "appearance", "hair", "eyes",
                    "temperament"),
        "features": ("signature", "appearance", "hair", "palette"),
        "makeup": ("makeup", "eyes", "palette"),
        "costume": ("costume", "palette", "accessories"),
        "costume_detail": ("costume_detail", "accessories"),
    }
    DESIGN_LABELS = (
        ("species", "形态"),
        ("appearance", "外貌"), ("hair", "发型"), ("eyes", "眼睛"),
        ("temperament", "气质"), ("personality", "性格"),
        ("makeup", "妆容"), ("costume", "服装"),
        ("costume_detail", "服装细节"), ("accessories", "配饰"),
        ("palette", "配色"), ("signature", "标志特征"))

    def _design_line(self, design, keys=None):
        """人物设定 → 提示词片段;keys 限定只取某几个字段。"""
        if not design:
            return ""
        parts = []
        for key, label in self.DESIGN_LABELS:
            if keys is not None and key not in keys:
                continue
            value = str(design.get(key) or "").strip()
            if value:
                parts.append(f"{label}:{value}")
        return ",".join(parts)

    def _anchor_character(self, project_id, characters=None):
        """风格锚角色:主角优先,否则名单第一位;记入 style_anchor 资产。"""
        row = self.assets.latest(project_id, "style_anchor", "default")
        if row is not None:
            meta = row["meta"]
            if isinstance(meta, str):
                meta = json.loads(meta or "{}")
            if meta.get("character"):
                return meta["character"]
        if not characters:
            return None
        anchor = next((c["name"] for c in characters
                       if "主" in (c.get("role") or "")),
                      characters[0]["name"])
        self.assets.register(project_id, "style_anchor", "default",
                             meta={"character": anchor})
        return anchor

    def _style_anchor_uri(self, project_id, exclude_name=None):
        """风格基准图:锚角色的最新立绘;全项目所有形象向它对齐。
        exclude_name=锚角色自己画立绘时不引用自己。"""
        anchor = self._anchor_character(project_id)
        if not anchor or anchor == exclude_name:
            return None
        row = self._locked_identity(project_id, anchor)
        if row and row["uri"] and Path(row["uri"]).exists():
            return row["uri"]
        return None

    def _character_design(self, project_id, name):
        row = self.assets.latest(project_id, "character", name)
        if row is None:
            return None
        meta = row["meta"]
        if isinstance(meta, str):
            meta = json.loads(meta or "{}")
        return (meta or {}).get("design")

    def _portrait_prompt(self, name, role, style, design=None):
        detail = self._design_line(
            design, keys=("species", "appearance", "hair", "eyes",
                          "makeup", "accessories", "signature",
                          "temperament", "personality", "costume",
                          "palette"))
        return (f"角色立绘:{name}({role}),{style}"
                + (f",{detail},表情站姿体现其性格" if detail else "")
                + ",全身,正面;如有角色参考图,优先锁定该图的人脸骨相、五官比例、"
                "眼鼻嘴、肤色与年龄感、发际线、发型轮廓、发色、眉眼妆、眼线、"
                "睫毛、唇妆和身份配饰,必须是同一个人;服装、服装颜色/材质、动作、"
                "场景和光影按本剧本及当集造型生成,允许与参考图服装不同,除非明确"
                "要求保留参考图服装")

    def _scene_prompt(self, location, style):
        return f"场景概念图:{location},{style},空镜,氛围感"

    def _sheet_prompt(self, name, role, style, label, desc, key=None,
                      design=None):
        detail = self._design_line(
            design, keys=self.SHEET_DESIGN_KEYS.get(key))
        return (f"角色{label}:{name}({role}),{style},{desc}"
                + (f";人物设定:{detail}" if detail else "")
                + ";与立绘同一人物、同一发型服装配色,严格保持形象一致")

    def _plan_path(self, ctx):
        return ctx["out_root"] / "render_plan.json"

    def _plan_read(self, ctx):
        path = self._plan_path(ctx)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        return {"items": []}

    def _plan_write(self, ctx, plan):
        plan["updated_at"] = now()
        path = self._plan_path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(path)

    def _plan_seed(self, ctx, category, items):
        """登记(或刷新)某分类要生成的全部图片;同 id 条目保留状态。"""
        plan = self._plan_read(ctx)
        old = {i["id"]: i for i in plan["items"]
               if i.get("category") == category}
        rest = [i for i in plan["items"] if i.get("category") != category]
        for item in items:
            prev = old.get(item["id"])
            item.setdefault("status", "pending")
            item.setdefault("error", "")
            if prev is not None:
                item["status"] = prev.get("status", "pending")
                item["error"] = prev.get("error", "")
                for key in ("provider", "model", "real", "fallbacks",
                            "image_task_class", "image_quality", "unit_cost",
                            "qc", "started_at", "finished_at", "duration",
                            "reference_inputs", "revision"):
                    if key in prev:
                        item[key] = prev[key]
                if prev.get("custom_prompt"):
                    item["prompt"] = prev.get("prompt", item["prompt"])
                    item["custom_prompt"] = True
        plan["items"] = rest + items
        self._plan_write(ctx, plan)

    def _plan_mark(self, ctx, item_id, status, error="", prompt=None,
                   only_pending=False, extra=None):
        plan = self._plan_read(ctx)
        for item in plan["items"]:
            if item["id"] != item_id:
                continue
            if only_pending and item.get("status") not in ("pending",
                                                           "failed"):
                return
            item["status"] = status
            item["error"] = error
            # 计时:生成中记起点,完成/失败记单张耗时(供前端估算剩余时间)
            if status == "generating":
                item["started_at"] = round(time.time(), 1)
                item.pop("finished_at", None)
            elif status in ("done", "failed") and item.get("started_at"):
                item["finished_at"] = round(time.time(), 1)
                item["duration"] = round(
                    item["finished_at"] - item["started_at"], 1)
            if prompt is not None and prompt != item.get("prompt"):
                item["prompt"] = prompt
                item["custom_prompt"] = True
            if extra:
                item.update(extra)
            self._plan_write(ctx, plan)
            return

    @staticmethod
    def _prompt_with_feedback(prompt, feedback):
        prompt = (prompt or "").strip()
        feedback = (feedback or "").strip()
        return (f"{prompt}。修改意见(必须落实):{feedback}"
                if feedback else prompt)

    @staticmethod
    def _reference_inputs(payload):
        """把本次真实传入产线的参考图做成人可读清单，供手机端核验。"""
        payload = payload or {}
        rows = []

        asset_by_uri = {
            str(item.get("uri")): item
            for item in (payload.get("asset_matches") or [])
            if item.get("uri")
        }

        def add(kind, uri, label=""):
            if not uri:
                return
            value = str(uri)
            if any(row["uri"] == value for row in rows):
                return
            match = asset_by_uri.get(value) or {}
            rows.append({
                "kind": kind,
                "label": match.get("label") or label or kind,
                "name": match.get("name") or Path(value).name or value,
                "uri": value,
                "asset_id": match.get("asset_id"),
                "source": "asset_center" if match else "upload",
            })

        for ref in payload.get("identity_references") or []:
            if isinstance(ref, dict):
                add("identity", ref.get("uri"),
                    f"{ref.get('character', '角色')}最终立绘")
        for uri in payload.get("character_refs") or []:
            add("character", uri, "人物设定/资产图")
        add("keyframe", payload.get("image_uri"), "本镜关键图")
        add("continuity", payload.get("chain_first_uri"), "上一镜尾帧")
        add("scene", payload.get("scene_ref"), "场景概念图")
        add("style", payload.get("style_ref"), "全项目风格基准图")
        for uri in payload.get("reference_images") or []:
            add("asset" if str(uri) in asset_by_uri else "user", uri,
                "资产中心匹配图" if str(uri) in asset_by_uri else "用户参考图")
        return {"attached": bool(rows), "count": len(rows),
                "required": bool(payload.get("require_reference_images")),
                "items": rows}

    ACCELERATABLE_IMAGE_CATEGORIES = frozenset({
        "character_candidate", "character_sheet", "scene_art",
        "shot_image", "frames",
    })
    ACCELERATION_IDENTITY_CATEGORIES = frozenset({
        "character_sheet", "shot_image", "frames",
    })

    @staticmethod
    def _stable_hash(value):
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _build_dispatch_contract(self, task, item):
        """从即将交给 worker 的真实 payload 构造不可变预检契约。"""
        payload = json.loads(json.dumps(
            task.get("payload") or {}, ensure_ascii=False, default=str))
        prompt = self._prompt_with_feedback(
            payload.get("prompt", ""), payload.get("feedback", ""))
        refs = self._reference_inputs(payload)
        category = item.get("category", "")
        characters = [str(value) for value in payload.get("characters") or []]
        identity_rows = [
            ref for ref in payload.get("identity_references") or []
            if isinstance(ref, dict)
        ]
        identity_map = {
            str(ref.get("character")): str(ref.get("uri"))
            for ref in identity_rows
            if ref.get("character") and ref.get("uri")
        }
        issues = []
        if not prompt.strip():
            issues.append("最终提示词为空")
        subject = str(payload.get("art_name") or payload.get("location") or "")
        expected_names = characters or ([subject] if subject else [])
        for name in expected_names:
            if name and name not in prompt:
                issues.append(f"提示词没有明确写出对象「{name}」")
        # 用户已明确要求：调用其他 API 时不能只交文字，所有加速项至少一图。
        if not refs["items"]:
            issues.append("API 加速必须携带真实参考图，不能只使用文字描述")
        for ref in refs["items"]:
            uri = str(ref.get("uri") or "")
            if not uri:
                issues.append(f"参考图「{ref.get('label', '未命名')}」没有文件地址")
            elif not uri.startswith(("http://", "https://", "data:image/")) \
                    and not Path(uri).is_file():
                issues.append(f"参考图文件不存在: {uri}")
        if category in self.ACCELERATION_IDENTITY_CATEGORIES:
            missing = [name for name in characters if name not in identity_map]
            extra = [name for name in identity_map if name not in characters]
            if missing:
                issues.append("缺少人物最终立绘映射: " + "、".join(missing))
            if extra:
                issues.append("参考图人物与提示词名单不一致: " + "、".join(extra))
        if category == "frames" and not (
                payload.get("image_uri") or payload.get("chain_first_uri")):
            issues.append("首尾帧缺少本镜关键帧/上一镜尾帧连续性参考")
        reference_facts = [{
            "kind": ref.get("kind", ""),
            "label": ref.get("label", ""),
            "name": ref.get("name", ""),
            "uri": ref.get("uri", ""),
            "asset_id": ref.get("asset_id"),
        } for ref in refs["items"]]
        contract = {
            "schema": "aifos.image-dispatch/v1",
            "item_id": task["item_id"],
            "label": item.get("label", task["item_id"]),
            "category": category,
            "capability": task["capability"],
            "prompt": prompt,
            "prompt_hash": self._stable_hash(prompt),
            "references": {
                "required": True,
                "count": len(reference_facts),
                "items": reference_facts,
            },
            "reference_hash": self._stable_hash(reference_facts),
            "characters": characters,
            "identity_map": identity_map,
            "base_quality": payload.get("image_quality", "medium"),
            "payload": payload,
            "issues": list(dict.fromkeys(issues)),
        }
        contract["passed"] = not contract["issues"]
        token_basis = {key: contract[key] for key in (
            "item_id", "category", "capability", "prompt_hash",
            "reference_hash", "characters", "identity_map")}
        contract["token"] = self._stable_hash(token_basis)
        return contract

    def _prepare_dispatch_contracts(self, ctx, tasks):
        plan = self._plan_read(ctx)
        by_id = {item.get("id"): item for item in plan.get("items", [])}
        records = []
        for task in tasks:
            item = by_id.get(task.get("item_id"))
            if item is None or item.get("category") \
                    not in self.ACCELERATABLE_IMAGE_CATEGORIES:
                continue
            contract = self._build_dispatch_contract(task, item)
            task["_dispatch_contract"] = contract
            task["_dispatch_contract_token"] = contract["token"]
            records.append({
                "item_id": task["item_id"],
                "category": item["category"],
                "capability": task["capability"],
                "contract_token": contract["token"],
                "contract": contract,
                "never_started": (
                    item.get("status", "pending") == "pending"
                    and not item.get("started_at")
                    and not item.get("finished_at")
                    and not item.get("provider")),
            })
        if records:
            self.image_acceleration.register(ctx["episode"]["id"], records)

    def _claim_dispatch_task(self, ctx, task):
        token = task.get("_dispatch_contract_token")
        if not token:
            return task
        request = self.image_acceleration.claim(
            ctx["episode"]["id"], task["item_id"], token)
        if request is None:
            return task
        quality = normalize_quality(
            request.get("quality") or "medium", field="image_quality")
        payload = dict(task.get("payload") or {})
        decision = dict(payload.get("quality_decision") or {})
        decision.update({
            "level": quality,
            "source": "api_acceleration",
            "reasons": ["用户将尚未开工图片批量分流到指定 API"],
        })
        payload.update({
            "image_quality": quality,
            "quality_decision": decision,
            "image_task_class": image_task_class_for(quality),
            "strict_provider": request["provider"],
            "model_override": request["model"],
            "require_reference_images": True,
        })
        # claim 后、进入 worker 前再用本任务持有的 Router 做一次硬校验；
        # 服务运行期间配置变更时宁可失败，也不允许模型/参考图静默漂移。
        self.router.validate_image_selection(
            request["provider"], task["capability"], payload,
            request["model"])
        accelerated = dict(task)
        accelerated["payload"] = payload
        accelerated["_acceleration"] = {
            "status": "running", "gate": "passed",
            "provider": request["provider"], "model": request["model"],
            "quality": quality, "contract_token": token,
        }
        return accelerated

    def _finish_dispatch_task(self, ctx, task, result=None, error=""):
        if not task.get("_dispatch_contract_token"):
            return
        self.image_acceleration.finish(
            ctx["episode"]["id"], task["item_id"],
            result={
                "provider": getattr(result, "provider", "") if result else "",
                "model": getattr(result, "model", "") if result else "",
            }, error=error)

    def _plan_run(self, ctx, item_id, fn, prompt=None, payload=None,
                  revision_source="manual"):
        """包住一次出图调用:生成中 → 完成/失败;手动停止落回排队。
        完成时记录实际使用的产线(真实/占位)与回退原因,界面透明可见。"""
        feedback = (payload or {}).get("feedback", "")
        self._plan_mark(ctx, item_id, "generating", prompt=prompt,
                        extra={
                            "qc": None,
                            "reference_inputs": self._reference_inputs(
                                payload),
                            "revision": {
                                "source": revision_source,
                                "prompt_modified": bool(feedback),
                                "feedback": feedback,
                            },
                        })
        try:
            result = fn()
        except ProduceCancelled:
            self._plan_mark(ctx, item_id, "pending")
            raise
        except Exception as exc:
            self._plan_mark(ctx, item_id, "failed", error=str(exc)[:300])
            raise
        extra = (self._plan_done_extra(result)
                 if getattr(result, "provider", None) else None)
        self._plan_mark(ctx, item_id, "done", extra=extra)
        return result

    # ---- 图片视觉质检:生成后核对剧本要求,不合格自动带意见重画 ----
    def _image_qc_enabled(self):
        return bool(self.config.get("defaults", "image_qc", default=True))

    def _qc_retries(self):
        try:
            return max(0, min(int(self.config.get(
                "defaults", "image_qc_retries", default=1)), 3))
        except (TypeError, ValueError):
            return 1

    def _identity_references(self, project_id, characters, required=True):
        refs, missing = [], []
        for name in characters or []:
            row = self._locked_identity(project_id, name)
            if row is None:
                missing.append(name)
                continue
            refs.append({
                "character": name,
                "asset_id": row["id"],
                "uri": row["uri"],
                "version": row["version"],
            })
        if required and missing:
            raise AifosError(
                "以下角色尚未锁定最终立绘，禁止出图/质检: " + "、".join(missing))
        return refs

    def _qc_spec(self, project_id, characters, location="", action="",
                 forbid=None, require_identity=True):
        """视觉质检规格：待检图必须与人工锁定的最终立绘逐人比对。"""
        designs = []
        for name in characters:
            design = self._character_design(project_id, name)
            line = self._design_line(design, keys=(
                "species", "hair", "costume",
                "signature")) if design else ""
            designs.append(f"{name}({line})" if line else name)
        identity_refs = self._identity_references(
            project_id, characters,
            required=bool(characters and require_identity))
        return {
            "characters": list(characters),
            "count": len(characters),
            "designs": ";".join(designs),
            "location": location or "",
            "action": action or "",
            "forbid": list(forbid or []),
            "identity_references": identity_refs,
            "identity_required": bool(characters and require_identity),
        }

    def _generate_image_with_qc(self, capability, payload, out_dir,
                                cancel, qc_spec):
        """出图 + 视觉质检 + 不合格自动重画(worker 线程安全:只调产线)。
        质检产线不可用/出错时放行不阻塞;结果附在 result.qc。"""
        attempts = 0
        spent = 0.0
        while True:
            result = self.router.call(capability, payload, out_dir,
                                      cancel=cancel)
            result.cost += spent
            if not qc_spec or not self._image_qc_enabled():
                return result
            uri = result.uri
            if not uri or not Path(uri).exists():
                return result
            try:
                qc_result = self.router.call(
                    "image_qc", {**qc_spec, "image_uri": uri}, out_dir,
                    cancel=cancel)
            except (ProviderUnavailable, ProviderError):
                return result      # 质检产线故障不阻塞生产
            result.cost += qc_result.cost
            verdict = qc_result.data or {}
            identity_checked = (not qc_spec.get("identity_required")
                                or bool(verdict.get("identity_checked")))
            issues = list(verdict.get("issues") or [])
            if not identity_checked:
                issues.append("质检未确认已逐人比对最终立绘")
            report = {"passed": bool(verdict.get("pass")) and identity_checked,
                      "issues": issues,
                      "attempts": attempts + 1,
                      "identity_checked": identity_checked,
                      "identity_references": len(
                          qc_spec.get("identity_references") or [])}
            result.qc = report
            if report["passed"] or attempts >= self._qc_retries():
                return result
            spent = result.cost
            attempts += 1
            payload = dict(payload)
            payload["feedback"] = ((payload.get("feedback") or "")
                                   + ";图片质检不通过,必须修正:"
                                   + "；".join(report["issues"]))[:800]
            payload["qc_attempt"] = attempts

    def _plan_done_extra(self, result):
        extra = {"provider": result.provider,
                 "real": result.provider != "mock",
                 "fallbacks": getattr(result, "fallbacks", [])}
        data = getattr(result, "data", {}) or {}
        for key in ("first_source", "generation_calls", "model",
                    "image_task_class", "image_quality", "unit_cost"):
            if key in data:
                extra[key] = data[key]
        model = getattr(result, "model", "")
        if model and "model" not in extra:
            extra["model"] = model
        qc = getattr(result, "qc", None)
        if qc is not None:
            extra["qc"] = qc
        return extra

    def _run_one_task(self, ctx, task):
        """串行执行单个出图任务(含质检),记账并更新清单。"""
        if self._cancel_requested(ctx):
            raise ProduceCancelled("已手动停止生成")
        try:
            task = self._claim_dispatch_task(ctx, task)
        except Exception as exc:
            self._finish_dispatch_task(ctx, task, error=str(exc))
            self._plan_mark(ctx, task["item_id"], "failed",
                            error=str(exc)[:300])
            raise
        payload = task.get("payload") or {}
        generating_extra = {
            "image_task_class": payload.get("image_task_class"),
            "image_quality": payload.get("image_quality"),
            "reference_inputs": self._reference_inputs(payload),
        }
        if task.get("_acceleration"):
            generating_extra["acceleration"] = task["_acceleration"]
        self._plan_mark(ctx, task["item_id"], "generating",
                        extra=generating_extra)
        try:
            result = self._generate_image_with_qc(
                task["capability"], task["payload"],
                ctx["out_root"] / task["sub_dir"],
                lambda: self._cancel_requested(ctx), task.get("qc_spec"))
        except ProduceCancelled:
            self._finish_dispatch_task(ctx, task, error="已手动停止生成")
            self._plan_mark(ctx, task["item_id"], "pending")
            raise
        except Exception as exc:
            self._finish_dispatch_task(ctx, task, error=str(exc))
            self._plan_mark(ctx, task["item_id"], "failed",
                            error=str(exc)[:300])
            raise
        self._task_cost += result.cost
        self._task_providers.add(result.provider)
        self.projects.add_episode_cost(ctx["episode"]["id"], result.cost)
        self._finish_dispatch_task(ctx, task, result=result)
        self._plan_mark(ctx, task["item_id"], "done",
                        extra=self._plan_done_extra(result))
        return result

    def _parallel_workers(self):
        try:
            workers = int(self.config.get(
                "defaults", "parallel_images", default=3))
        except (TypeError, ValueError):
            workers = 3
        return max(1, min(workers, 8))

    def _run_parallel(self, ctx, tasks, line="出图产线"):
        """有界并行出图:只把 worker 数量的任务标为生成中。

        多人/文字/场首等高风险镜头可通过 priority 提前；尚未真正开工的
        条目保持 pending，因此计时与暂停后的恢复都反映真实状态。
        worker 线程只做产线调用;记账/资产登记/清单状态全在主线程。
        tasks: [{"item_id","capability","payload","sub_dir","tag","priority"}]
        返回 {tag: ProviderResult};暂停时未完成条目回到排队并保留已完成。"""
        if not tasks:
            return {}
        self._prepare_dispatch_contracts(ctx, tasks)
        tasks = sorted(tasks, key=lambda task: (
            -int(task.get("priority", 0)), str(task.get("item_id", ""))))
        workers = self._parallel_workers()
        if workers == 1 or len(tasks) == 1:
            out = {}
            for task in tasks:
                out[task["tag"]] = self._run_one_task(ctx, task)
            return out
        if self._cancel_requested(ctx):
            raise ProduceCancelled("已手动停止生成")
        episode = self.projects.get_episode(ctx["episode"]["id"])
        budget = self.config.get("budget", "per_episode", default=0)
        if budget and episode["cost"] >= budget:
            raise BudgetExceeded(
                f"单集成本 {episode['cost']:.2f} 已达预算 {budget},停止调度")
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
        self.log.info(
            "director",
            f"{line}并行开工:共 {len(tasks)} 张,{workers} 路同时生成")
        cancel = lambda: self._cancel_requested(ctx)   # noqa: E731
        results, failures = {}, []
        cancelled = False
        started_at = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            iterator = iter(tasks)
            futures = {}

            def submit_next():
                try:
                    task = next(iterator)
                except StopIteration:
                    return False
                try:
                    task = self._claim_dispatch_task(ctx, task)
                except Exception as exc:
                    failures.append((task, exc))
                    self._finish_dispatch_task(ctx, task, error=str(exc))
                    self._plan_mark(ctx, task["item_id"], "failed",
                                    error=str(exc)[:300])
                    return False
                payload = task.get("payload") or {}
                generating_extra = {
                    "image_task_class": payload.get("image_task_class"),
                    "image_quality": payload.get("image_quality"),
                    "reference_inputs": self._reference_inputs(payload),
                }
                if task.get("_acceleration"):
                    generating_extra["acceleration"] = task["_acceleration"]
                self._plan_mark(ctx, task["item_id"], "generating",
                                extra=generating_extra)
                future = pool.submit(
                    self._generate_image_with_qc, task["capability"],
                    task["payload"], ctx["out_root"] / task["sub_dir"],
                    cancel, task.get("qc_spec"))
                futures[future] = task
                return True

            for _ in range(min(workers, len(tasks))):
                submit_next()
            while futures:
                done_now, _ = wait(set(futures), timeout=2,
                                   return_when=FIRST_COMPLETED)
                for future in done_now:
                    task = futures.pop(future)
                    try:
                        result = future.result()
                    except ProduceCancelled:
                        cancelled = True
                        self._finish_dispatch_task(
                            ctx, task, error="已手动停止生成")
                        self._plan_mark(ctx, task["item_id"], "pending")
                        continue
                    except Exception as exc:
                        failures.append((task, exc))
                        self._finish_dispatch_task(ctx, task, error=str(exc))
                        self._plan_mark(ctx, task["item_id"], "failed",
                                        error=str(exc)[:300])
                        continue
                    self._task_cost += result.cost
                    self._task_providers.add(result.provider)
                    self.projects.add_episode_cost(
                        ctx["episode"]["id"], result.cost)
                    self._finish_dispatch_task(ctx, task, result=result)
                    self._plan_mark(ctx, task["item_id"], "done",
                                    extra=self._plan_done_extra(result))
                    results[task["tag"]] = result
                if not cancelled and not failures \
                        and not self._cancel_requested(ctx):
                    while len(futures) < workers and submit_next():
                        pass
        elapsed = max(.001, time.monotonic() - started_at)
        self.log.info(
            "director", f"{line}本批完成 {len(results)}/{len(tasks)}，"
            f"墙钟 {elapsed:.1f}s，吞吐 {len(results) * 60 / elapsed:.2f} 张/分钟")
        if cancelled or self._cancel_requested(ctx):
            raise ProduceCancelled(
                "已手动暂停(本批已完成的图片全部保留)")
        if failures:
            raise failures[0][1]
        return results

    def image_acceleration_options(self, project_title, episode_number):
        """当前 stage 尚未进入 worker 的图片与可选 API/模型。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        plan = self._plan_read(ctx)
        plan_by_id = {item.get("id"): item for item in plan.get("items", [])}
        rows = self.image_acceleration.list(episode["id"])
        items = []
        for row in rows:
            contract = row.get("contract") or {}
            item = plan_by_id.get(row["item_id"]) or {}
            issues = list(contract.get("issues") or [])
            plan_pending = item.get("status") == "pending"
            if row["acceleration_status"] in ("queued", "running"):
                status = row["acceleration_status"]
            elif row["acceleration_status"] in ("done", "failed"):
                status = row["acceleration_status"]
            elif row["production_state"] != "pending" or not plan_pending:
                status = ("completed" if row["production_state"] == "generated"
                          else "in_production")
                issues = []
            elif not row["never_started"]:
                status = "retry_only"
                issues.append("图片曾经进入过生产线，只能按重试流程处理")
            else:
                status = "blocked" if issues else "ready"
            items.append({
                "item_id": row["item_id"],
                "label": contract.get("label", item.get("label", row["item_id"])),
                "category": row["category"],
                "capability": row["capability"],
                "status": status,
                "production_state": row["production_state"],
                "contract_token": row["contract_token"],
                "prompt": contract.get("prompt", ""),
                "prompt_hash": contract.get("prompt_hash", ""),
                "references": contract.get("references") or {
                    "required": True, "count": 0, "items": []},
                "characters": contract.get("characters") or [],
                "identity_map": contract.get("identity_map") or {},
                "base_quality": contract.get("base_quality", "medium"),
                "issues": list(dict.fromkeys(issues)),
                "requested_provider": row["requested_provider"],
                "requested_model": row["requested_model"],
                "requested_quality": row["requested_quality"],
                "actual_provider": row["actual_provider"],
                "actual_model": row["actual_model"],
                "error": row["error"],
            })
        providers = self.router.image_api_options()
        default = next((option for option in providers if option["ready"]), None)
        ready = [item for item in items if item["status"] == "ready"]
        return {
            "project": project_title,
            "episode": episode_number,
            "providers": providers,
            "default_provider": default["provider"] if default else "",
            "default_model": default["default_model"] if default else "",
            "default_quality": "medium",
            "items": items,
            "summary": {
                "total": len(items), "ready": len(ready),
                "queued": sum(item["status"] == "queued" for item in items),
                "running": sum(item["status"] == "running" for item in items),
                "blocked": sum(item["status"] == "blocked" for item in items),
                "completed": sum(item["status"] == "completed" for item in items),
            },
        }

    def preflight_image_acceleration(
            self, project_title, episode_number, item_ids, provider, model,
            quality="medium", contract_tokens=None):
        """无副作用逐张核对最终提示词、参考图、API、模型和质量。"""
        quality = normalize_quality(
            quality or "medium", field="image_quality")
        unique = list(dict.fromkeys(str(value) for value in (item_ids or [])
                                    if str(value).strip()))
        if not unique:
            raise AifosError("至少选择一张尚未开工的图片")
        if len(unique) > 200:
            raise AifosError("单次最多加速 200 张图片")
        _project, episode = self._episode_ctx(project_title, episode_number)
        rows = {row["item_id"]: row
                for row in self.image_acceleration.list(episode["id"])}
        expected = contract_tokens or {}
        checked = []
        for item_id in unique:
            row = rows.get(item_id)
            issues = []
            if row is None:
                checked.append({"item_id": item_id, "label": item_id,
                                "status": "blocked",
                                "issues": ["图片尚未形成可派发契约"]})
                continue
            contract = row.get("contract") or {}
            issues.extend(contract.get("issues") or [])
            if expected.get(item_id) \
                    and expected[item_id] != row["contract_token"]:
                issues.append("页面中的提示词/参考图预览已经过期")
            if row["production_state"] != "pending" \
                    or not row["never_started"]:
                issues.append("图片已进入生产线，不能再切换 API")
            if row["acceleration_status"] in ("queued", "running", "done"):
                issues.append("图片已经提交过 API 加速")
            payload = dict(contract.get("payload") or {})
            decision = dict(payload.get("quality_decision") or {})
            decision.update({"level": quality,
                             "source": "api_acceleration"})
            payload.update({
                "image_quality": quality,
                "quality_decision": decision,
                "image_task_class": image_task_class_for(quality),
                "require_reference_images": True,
                "strict_provider": provider,
                "model_override": model,
            })
            if not issues:
                try:
                    self.router.validate_image_selection(
                        provider, row["capability"], payload, model)
                except (ProviderUnavailable, ProviderError) as exc:
                    issues.append(str(exc))
            checked.append({
                "item_id": item_id,
                "label": contract.get("label", item_id),
                "category": row["category"],
                "capability": row["capability"],
                "status": "blocked" if issues else "ready",
                "issues": list(dict.fromkeys(issues)),
                "contract_token": row["contract_token"],
                "prompt": contract.get("prompt", ""),
                "prompt_hash": contract.get("prompt_hash", ""),
                "references": contract.get("references") or {},
                "characters": contract.get("characters") or [],
                "provider": provider, "model": model,
                "quality": quality,
            })
        passed = bool(checked) and all(
            item["status"] == "ready" for item in checked)
        fingerprint_basis = {
            "episode_id": episode["id"], "provider": provider,
            "model": model, "quality": quality,
            "items": [{"item_id": item["item_id"],
                       "contract_token": item.get("contract_token", "")}
                      for item in checked],
        }
        return {
            "passed": passed,
            "fingerprint": self._stable_hash(fingerprint_basis),
            "provider": provider, "model": model, "quality": quality,
            "items": checked,
            "summary": {
                "total": len(checked),
                "ready": sum(item["status"] == "ready" for item in checked),
                "blocked": sum(item["status"] != "ready" for item in checked),
            },
        }

    def queue_image_acceleration(
            self, project_title, episode_number, item_ids, provider, model,
            quality="medium", fingerprint="", contract_tokens=None):
        report = self.preflight_image_acceleration(
            project_title, episode_number, item_ids, provider, model,
            quality=quality, contract_tokens=contract_tokens)
        if fingerprint and fingerprint != report["fingerprint"]:
            raise AifosError("预检结果已过期，请重新核对提示词和参考图")
        if not report["passed"]:
            first = next(item for item in report["items"]
                         if item["status"] != "ready")
            raise AifosError(
                f"{first['label']} 未通过放行: "
                + "；".join(first.get("issues") or ["未知原因"]))
        _project, episode = self._episode_ctx(project_title, episode_number)
        requests = [{
            "item_id": item["item_id"],
            "contract_token": item["contract_token"],
            "provider": provider, "model": model, "quality": report["quality"],
        } for item in report["items"]]
        self.image_acceleration.queue(episode["id"], requests)
        return {
            "queued": len(requests), "provider": provider,
            "model": model, "quality": report["quality"],
            "item_ids": [request["item_id"] for request in requests],
            "fingerprint": report["fingerprint"],
        }

    @staticmethod
    def _shot_priority(shot, scene_first=False):
        """失败代价最高的镜头先出，尽早暴露多人/文字/运动问题。"""
        people = int(shot.get("character_count", len(
            shot.get("characters", []))))
        text = shot.get("readable_text") or {}
        camera = str(shot.get("camera") or "")
        action = str(shot.get("description") or shot.get("prompt") or "")
        return (people * 30
                + (45 if text.get("required") else 0)
                + (25 if scene_first else 0)
                + (15 if any(word in camera for word in
                             ("跟", "移", "摇", "环绕")) else 0)
                + (10 if any(word in action for word in
                             ("走", "跑", "进入", "离开", "追")) else 0))

    def _plan_seed_shots(self, ctx):
        """分镜确定后,把每个镜头的关键帧与首尾帧登记进清单。
        清单里展示的是详细提示词(含人物设定与故事情境),所见即所得。"""
        shots = (ctx.get("storyboard") or {}).get("shots") or []
        locations = self._scene_locations(ctx) if ctx.get("script") else {}
        scene_counts = {}
        for shot in shots:
            scene_counts[shot.get("scene_no")] = (
                scene_counts.get(shot.get("scene_no"), 0) + 1)
        image_items, frame_items = [], []
        for shot in shots:
            shot_no = shot["shot_no"]
            image_quality = resolve_image_quality(
                recommend_shot_image_quality(shot),
                ctx.get("quality_policy") or default_quality_policy(),
                f"shot:{shot_no}")
            frame_quality = resolve_image_quality(
                recommend_shot_image_quality(
                    shot, continuity_anchor=(
                        scene_counts.get(shot.get("scene_no"), 0) > 1)),
                ctx.get("quality_policy") or default_quality_policy(),
                f"frames:{shot_no}")
            image_items.append({
                "id": f"shot:{shot_no}", "category": "shot_image",
                "label": f"镜头 {shot_no:02d}"
                         + (f" · 第{shot['scene_no']}场"
                            if shot.get("scene_no") else ""),
                "shot_no": shot_no,
                "prompt": self._rich_shot_prompt(
                    ctx, shot, locations.get(shot.get("scene_no"), "")),
                **self._quality_meta(image_quality),
            })
            frame_items.append({
                "id": f"frames:{shot_no}", "category": "frames",
                "label": f"镜头 {shot_no:02d} 首尾帧",
                "shot_no": shot_no,
                "prompt": shot.get("seedance_prompt", shot["prompt"]),
                **self._quality_meta(frame_quality),
            })
        self._plan_seed(ctx, "shot_image", image_items)
        self._plan_seed(ctx, "frames", frame_items)

    # ---- 各阶段实现 ----
    def _stage_script(self, ctx):
        episode = ctx["episode"]
        provided = ctx.get("provided_script")
        if provided is not None:
            provided.setdefault("project_title", ctx["project"]["title"])
            provided.setdefault("episode_number", episode["number"])
            version = self.projects.save_document(
                episode["id"], "script", provided)
            ctx["script"] = provided
            ctx["script_version"] = version
            # 用户自己写的剧本不需要再过目 → 不触发剧本确认暂停
            self.log.info("director", f"使用用户自带剧本(v{version}),"
                          "人物/分镜将自动推导")
            return {"version": version, "provided": True,
                    "scenes": len(provided["scenes"])}
        if not ctx.get("force"):
            existing, version = self.projects.latest_document(
                episode["id"], "script")
            if existing is not None:
                ctx["script"] = existing
                ctx["script_version"] = version
                self.log.info("director", f"复用已有剧本 v{version}")
                return {"version": version, "reused": True,
                        "scenes": len(existing["scenes"])}
        payload = {
            "project_title": ctx["project"]["title"],
            "episode_number": episode["number"],
            "premise": episode["premise"],
            "style": ctx["project"]["style"],
            "template": ctx["project"]["kind"],       # drama / idol
            "persona": ctx["project"]["title"],       # 偶像人设名=项目名
        }
        if ctx.get("feedback"):
            # 修改意见:连同上一版剧本一起交给编剧重写
            payload["feedback"] = ctx["feedback"]
            previous, _ = self.projects.latest_document(
                episode["id"], "script")
            if previous is not None:
                payload["previous_script"] = previous
        result = self._call(ctx, "script", payload, "script")
        script = result.data
        version = self.projects.save_document(episode["id"], "script", script)
        ctx["script"] = script
        ctx["script_version"] = version
        ctx["script_is_new"] = True     # 新写的剧本 → 触发剧本确认暂停
        self.data.record(
            "prompt", "success", prompt=f"script:{ctx['project']['title']}"
            f":e{episode['number']}", uri=result.uri,
            meta={"version": version}, episode_id=episode["id"])
        return {"version": version, "scenes": len(script["scenes"])}

    def _stage_continuity(self, ctx):
        """项目角色/场景/文字规则与生产配置的单集快照。"""
        if not ctx.get("force"):
            existing, version = self.projects.latest_document(
                ctx["episode"]["id"], "continuity")
            if (existing is not None
                    and existing.get("pipeline_version") == PIPELINE_VERSION
                    and existing.get("script_version") == ctx.get(
                        "script_version")
                    and (existing.get("production_profile")
                         if isinstance(existing.get("production_profile"),
                                       dict) else {}).get(
                        "standard_fingerprint") == ctx[
                            "production_profile"].get(
                                "standard_fingerprint")):
                ctx["continuity"] = existing
                return {"version": version, "reused": True,
                        "characters": len(existing.get("characters", [])),
                        "scenes": len(existing.get("scenes", []))}
        continuity = build_continuity_bible(
            ctx["project"], ctx["script"], ctx["production_profile"])
        continuity["script_version"] = ctx.get("script_version")
        version = self.projects.save_document(
            ctx["episode"]["id"], "continuity", continuity)
        ctx["continuity"] = continuity
        return {"version": version,
                "characters": len(continuity["characters"]),
                "scenes": len(continuity["scenes"])}

    def _stage_storyboard(self, ctx):
        if not ctx.get("force"):
            existing, version = self.projects.latest_document(
                ctx["episode"]["id"], "storyboard")
            if (existing is not None
                    and existing.get("pipeline_version") == PIPELINE_VERSION
                    and existing.get("script_version") == ctx.get(
                        "script_version")
                    and (existing.get("profile")
                         if isinstance(existing.get("profile"), dict)
                         else {}).get(
                        "standard_fingerprint") == ctx[
                            "production_profile"].get(
                                "standard_fingerprint")):
                ctx["storyboard"] = existing
                self._plan_seed_shots(ctx)
                self.log.info("director", f"复用已有五维分镜 v{version}")
                return {"version": version, "reused": True,
                        "shots": len(existing["shots"])}
            if existing is not None:
                self.log.info(
                    "director", "剧本/标准已更新,重出分镜并重制后续画面")
        result = self._call(
            ctx, "storyboard", {
                "script": ctx["script"],
                "continuity": ctx["continuity"],
                "production_profile": ctx["production_profile"],
            }, "storyboard")
        # 原始分镜落盘:加工若出错,凭这份文件即可复现定位
        raw_path = ctx["out_root"] / "storyboard" / "raw_provider.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(
            json.dumps(result.data, ensure_ascii=False, indent=1,
                       default=str),
            encoding="utf-8")
        try:
            storyboard = enrich_storyboard(
                ctx["script"], result.data, ctx["continuity"],
                ctx["production_profile"],
                style=ctx["project"].get("style", ""))
        except (AttributeError, TypeError, KeyError, ValueError) as exc:
            # 最后兜底:未知畸形结构给出可行动的错误与原始文件位置
            raise AifosError(
                f"分镜产出结构异常({exc});原始分镜已保存在 "
                f"{raw_path},把该文件发给开发助手即可定位") from exc
        storyboard["script_version"] = ctx.get("script_version")
        version = self.projects.save_document(
            ctx["episode"]["id"], "storyboard", storyboard)
        ctx["storyboard"] = storyboard
        # 分镜变了 → 旧的关键帧/首尾帧/视频全部作废重做
        ctx["force"] = True
        for shot in storyboard["shots"]:
            self.assets.register(
                ctx["project"]["id"], "prompt",
                f"e{ctx['episode']['number']:03d}_shot{shot['shot_no']:03d}",
                meta={"prompt": shot["prompt"],
                      "seedance_prompt": shot["seedance_prompt"],
                      "unit_id": shot["unit_id"]})
        self._plan_seed_shots(ctx)
        return {"version": version, "shots": len(storyboard["shots"]),
                "pipeline_version": storyboard["pipeline_version"]}

    def _stage_blocking(self, ctx):
        """五维分镜 → 确定性俯视空间图，不消耗任何出图额度。"""
        rules = ctx["production_profile"].get("rules", {}).get(
            "storyboard", {})
        threshold = int(rules.get(
            "spatial_blocking_required_for_group", 3))
        candidate = build_spatial_plan(
            ctx["script"], ctx["storyboard"], ctx["continuity"],
            group_threshold=threshold)
        existing, version = self.projects.latest_document(
            ctx["episode"]["id"], "blocking")
        reused = (not ctx.get("force") and existing is not None
                  and existing.get("source_fingerprint")
                  == candidate["source_fingerprint"]
                  and (existing.get("validation") or {}).get("passed"))
        blocking = existing if reused else candidate
        paths = write_spatial_svgs(
            blocking, ctx["out_root"] / "blocking")
        if not reused:
            version = self.projects.save_document(
                ctx["episode"]["id"], "blocking", blocking)
        ctx["blocking"] = blocking
        return {
            "version": version,
            "reused": reused,
            "scenes": len(blocking.get("scenes", [])),
            "required_scenes": blocking.get("summary", {}).get(
                "required_scenes", 0),
            "shots": blocking.get("summary", {}).get("shots", 0),
            "svgs": len(paths),
            "passed": blocking.get("validation", {}).get("passed", False),
        }

    def _ensure_character_designs(self, ctx, characters):
        """人物设定:编剧 AI 为每个角色写性格/外貌/妆容/服装细节。
        项目级一次生成(存 character 资产 meta),跨集复用保证形象一致;
        缺谁补谁,占位产线也会给出具体可画的设定。"""
        project_id = ctx["project"]["id"]
        designs, missing = {}, []
        for character in characters:
            name = character["name"]
            design = self._character_design(project_id, name)
            if design:
                designs[name] = design
            else:
                missing.append(character)
        if not missing:
            return designs
        result = self._call(ctx, "script", {
            "character_design": True,
            "project_title": ctx["project"]["title"],
            "style": ctx["project"]["style"] or "",
            "logline": (ctx.get("script") or {}).get("logline", ""),
            "characters": [{"name": c["name"],
                            "role": c.get("role", "")}
                           for c in missing],
        }, "script")
        by_name = {d.get("name"): d
                   for d in result.data.get("designs", [])}
        for character in missing:
            name = character["name"]
            design = by_name.get(name)
            if not design:
                continue
            self.assets.register(
                project_id, "character", name,
                meta={"role": character.get("role", ""),
                      "design": design}, new_version=True)
            designs[name] = design
        if designs:
            self.log.info(
                "director",
                "人物设定已就绪(性格/外貌/妆容/服装细节),"
                f"覆盖角色: {'、'.join(designs)}")
        return designs

    @staticmethod
    def _asset_meta(row):
        if row is None:
            return {}
        meta = row["meta"]
        if isinstance(meta, str):
            try:
                return json.loads(meta or "{}")
            except ValueError:
                return {}
        return meta or {}

    def _asset_quality(self, row, default="medium"):
        """旧资产没有质量元数据时按中档兼容；新资产必须显式记录。"""
        value = self._asset_meta(row).get("image_quality", default)
        try:
            return normalize_quality(value, field="asset.image_quality")
        except AifosError:
            return default

    def _quality_meta(self, decision):
        return {
            "image_quality": decision["level"],
            "recommended_quality": decision.get("recommended",
                                                decision["level"]),
            "quality_source": decision.get("source", "auto"),
            "quality_rule": decision.get("rule", ""),
            "quality_reasons": list(decision.get("reasons") or []),
        }

    def _shot_image_meta(self, ctx, shot, decision, extra=None):
        """镜头图写入可检索上下文，供跨集资产匹配和复用。"""
        location = self._scene_locations(ctx).get(shot.get("scene_no"), "")
        value = {
            **self._quality_meta(decision),
            "episode_number": ctx["episode"]["number"],
            "shot_no": shot.get("shot_no"),
            "scene_no": shot.get("scene_no"),
            "characters": list(shot.get("characters") or []),
            "location": location,
        }
        if extra:
            value.update(extra)
        return value

    @staticmethod
    def _quality_meets(actual, required):
        order = {"low": 0, "medium": 1, "high": 2}
        return order.get(actual, 1) >= order.get(required, 1)

    def _locked_identity(self, project_id, name):
        """返回人工选定的最终立绘；普通 character_art 不视为已定版。"""
        row = self.assets.latest(project_id, "character_identity", name)
        if row is None or not self._asset_meta(row).get("locked"):
            return None
        if not formal_reference_allowed(self._asset_quality(row)):
            return None
        uri = row["uri"]
        if not uri:
            return None
        if not uri.startswith(("http://", "https://")) and not Path(uri).exists():
            return None
        return row

    def character_selection_status(self, project_id, characters):
        """项目级人物定版状态：每人5候选、最终只锁1张，跨集复用。"""
        result = []
        candidate_rows = {}
        for row in self.assets.list(project_id, "character_candidate"):
            candidate_rows[row["name"]] = row
        for character in characters or []:
            name = character["name"] if isinstance(character, dict) else str(character)
            locked = self._locked_identity(project_id, name)
            selected_meta = self._asset_meta(locked)
            candidates = []
            for row in candidate_rows.values():
                meta = self._asset_meta(row)
                if meta.get("character") != name:
                    continue
                uri = row["uri"]
                if not uri or (not uri.startswith(("http://", "https://"))
                               and not Path(uri).exists()):
                    continue
                index = int(meta.get("candidate_index") or 0)
                candidates.append({
                    "id": f"candidate:{name}:{index}",
                    "index": index,
                    "uri": uri,
                    "version": row["version"],
                    "selected": bool(
                        locked and selected_meta.get("candidate_asset_id") == row["id"]),
                })
            candidates.sort(key=lambda item: item["index"])
            result.append({
                "character": name,
                "role": character.get("role", "") if isinstance(character, dict) else "",
                "locked": locked is not None,
                "identity_uri": locked["uri"] if locked else "",
                "identity_version": locked["version"] if locked else None,
                "candidates": candidates,
                "candidate_count": len(candidates),
            })
        locked_count = sum(1 for item in result if item["locked"])
        return {
            "schema": "aifos.character-selection/v1",
            "candidate_target": CHARACTER_CANDIDATES,
            "characters": result,
            "locked": locked_count,
            "total": len(result),
            "passed": bool(result) and locked_count == len(result),
            "required": any(not item["locked"] for item in result),
        }

    def _ensure_character_candidates(self, ctx, characters, designs, style):
        """为尚未定版的角色补足5张候选；候选之间并行，后续等待人工选择。"""
        project_id = ctx["project"]["id"]
        seed = []
        tasks = []
        quality_by_candidate = {}
        for character in characters:
            name = character["name"]
            role = character.get("role", "")
            locked = self._locked_identity(project_id, name)
            existing = {}
            for index in range(1, CHARACTER_CANDIDATES + 1):
                row = self.assets.latest(
                    project_id, "character_candidate", f"{name}:{index:02d}")
                if row is None:
                    continue
                meta = self._asset_meta(row)
                idx = int(meta.get("candidate_index") or 0)
                uri = row["uri"]
                if idx and uri and (uri.startswith(("http://", "https://"))
                                    or Path(uri).exists()):
                    existing[idx] = row
            if locked and not existing:
                # 人工上传的最终立绘没有候选集，仍视为明确人工定版。
                continue
            refs = self._reference_uris(project_id, [name])
            base_prompt = self._portrait_prompt(
                name, role, style, design=designs.get(name))
            quality = resolve_image_quality(
                recommend_asset_quality("character_candidate"),
                ctx.get("quality_policy") or default_quality_policy(),
                f"candidate:{name}")
            for index in range(1, CHARACTER_CANDIDATES + 1):
                item_id = f"candidate:{name}:{index}"
                quality_by_candidate[(name, index)] = quality
                prompt = (
                    f"{base_prompt};人物候选{index}/{CHARACTER_CANDIDATES};"
                    "身份核心设定不变，只允许在脸部骨相细节、神态感染力和"
                    "自然真实感上做克制差异；干净均匀肤质，禁止塑料脸、"
                    "脏污毛孔；参考图锁定脸型、五官、发型、发色、妆容和年龄感；"
                    "服装与配色按当前剧本和本集造型，可与参考图不同；禁止新增人物")
                seed.append({
                    "id": item_id, "category": "character_candidate",
                    "label": f"{name} · 候选 {index}", "name": name,
                    "candidate_index": index, "prompt": prompt,
                    **self._quality_meta(quality),
                })
                if index in existing or locked:
                    continue
                tasks.append({
                    "item_id": item_id,
                    "capability": "image",
                    "payload": {
                        "portrait": True,
                        "portrait_candidate": True,
                        "image_task_class": image_task_class_for(
                            quality["level"]),
                        "image_quality": quality["level"],
                        "quality_decision": quality,
                        "art_name": f"{name}_candidate_{index:02d}",
                        "role": role, "shot_no": 0,
                        "characters": [name], "location": "",
                        "prompt": prompt, "style": style,
                        "reference_images": refs,
                        # 初次定妆尚不存在最终立绘；若用户上传过身份参考，
                        # API 也必须真实使用这些图，不能只读文字。
                        "require_reference_images": bool(refs),
                        "aspect": ctx["aspect"], **ctx["dims"],
                    },
                    "sub_dir": "cast/candidates",
                    "tag": (name, index, role),
                    # 初始人物母资产只按剧本性格、角色参考图和风格生成，
                    # 不在候选阶段做视觉 QC；人工选定后供后续镜头质检引用。
                })
        self._plan_seed(ctx, "character_candidate", seed)
        # 已存在的候选明确标成复用，避免重新排队。
        for item in seed:
            name, index = item["name"], item["candidate_index"]
            status = self.character_selection_status(project_id, [name])
            if any(c["index"] == index
                   for c in status["characters"][0]["candidates"]):
                self._plan_mark(ctx, item["id"], "reused", only_pending=True)
        for (name, index, role), result in self._run_parallel(
                ctx, tasks, line="人物定妆候选(每人5张)").items():
            quality = quality_by_candidate[(name, index)]
            self.assets.register(
                project_id, "character_candidate", f"{name}:{index:02d}",
                uri=result.uri,
                meta={"character": name, "role": role,
                      "candidate_index": index,
                      "provider": result.provider,
                      "model": getattr(result, "model", ""),
                      **self._quality_meta(quality)})
        return self.character_selection_status(project_id, characters)

    def select_character_candidate(self, project_title, episode_number,
                                   character_name, candidate_index):
        """人工选择并锁定最终立绘；下游只能引用该不可变身份锚点。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        script, _ = self.projects.latest_document(episode["id"], "script")
        characters = (script or {}).get("characters", [])
        character = next((c for c in characters
                          if c.get("name") == character_name), None)
        if character is None:
            raise AifosError(f"剧本中没有角色: {character_name}")
        if episode["status"] != "awaiting_cast":
            raise AifosError("只能在人物定版阶段选择候选；后续已生产时请先重开定版")
        candidate = self.assets.latest(
            project["id"], "character_candidate",
            f"{character_name}:{int(candidate_index):02d}")
        if candidate is None or not candidate["uri"]:
            raise AifosError(f"人物候选不存在: {character_name}/{candidate_index}")
        if (not candidate["uri"].startswith(("http://", "https://"))
                and not Path(candidate["uri"]).exists()):
            raise AifosError("候选图片文件已丢失，请重新生成候选")
        candidate_quality = self._asset_quality(candidate, default="high")
        if not formal_reference_allowed(candidate_quality):
            raise AifosError(
                "低质量试错图不能锁为正式人物参考，请把选中形象以高质量重生后再定版")
        meta = {
            "character": character_name,
            "role": character.get("role", ""),
            "locked": True,
            "candidate_index": int(candidate_index),
            "candidate_asset_id": candidate["id"],
            "candidate_version": candidate["version"],
            "locked_at": now(),
            "image_quality": candidate_quality,
            "recommended_quality": "high",
            "quality_source": "selected_mother_asset",
        }
        identity = self.assets.register(
            project["id"], "character_identity", character_name,
            uri=candidate["uri"], meta=meta, new_version=True)
        # character_art 是旧代码/资产中心的兼容别名，但其来源明确指向
        # 人工锁定的 identity，不能再由文字直接生成。
        self.assets.register(
            project["id"], "character_art", character_name,
            uri=candidate["uri"], meta=meta, new_version=True)
        ctx = {"episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        plan = self._plan_read(ctx)
        for item in plan.get("items", []):
            if item.get("category") == "character_candidate" \
                    and item.get("name") == character_name:
                self._plan_mark(
                    ctx, item["id"], item.get("status", "done"),
                    extra={"selected": int(item.get("candidate_index", 0))
                           == int(candidate_index)})
        self._plan_mark(
            ctx, f"char:{character_name}", "reused",
            extra={"selected": True, "identity_version": identity["version"]})
        status = self.character_selection_status(project["id"], characters)
        self.projects.save_document(episode["id"], "cast_selection", status)
        self.log.info(
            "director",
            f"人物定版: {character_name} 选中候选{int(candidate_index)}，"
            f"进度 {status['locked']}/{status['total']}")
        return status

    def _stage_cast(self, ctx):
        """人物立绘与场景概念图:项目级资产,跨集复用保证形象一致。"""
        project_id = ctx["project"]["id"]
        style = ctx["project"]["style"] or DEFAULT_VISUAL_STYLE
        characters = ctx["script"].get("characters", [])
        locations = []
        for scene in ctx["script"]["scenes"]:
            if scene["location"] not in locations:
                locations.append(scene["location"])
        location_reuse = {
            location: sum(1 for scene in ctx["script"]["scenes"]
                          if scene.get("location") == location)
            for location in locations
        }
        scene_quality = {
            location: resolve_image_quality(
                recommend_asset_quality(
                    "scene_art", reuse_count=location_reuse[location]),
                ctx.get("quality_policy") or default_quality_policy(),
                f"scene:{location}")
            for location in locations
        }
        # 先由编剧 AI 写人物设定(性格/外貌/妆容/服装细节),
        # 立绘与全部资产套件的提示词据此丰富;项目级一次,跨集复用
        designs = self._ensure_character_designs(ctx, characters)
        # 风格锚:主角立绘最先画,成为全项目形象的风格基准图
        anchor_name = self._anchor_character(project_id, characters)
        characters = sorted(
            characters, key=lambda c: c["name"] != anchor_name)
        selection = self._ensure_character_candidates(
            ctx, characters, designs, style)
        self.projects.save_document(
            ctx["episode"]["id"], "cast_selection", selection)
        if selection["required"]:
            ctx["cast"] = [c["name"] for c in characters]
            ctx["cast_selection"] = selection
            ctx["cast_selection_required"] = True
            return {
                "characters": len(characters),
                "candidates": sum(item["candidate_count"]
                                  for item in selection["characters"]),
                "candidate_target": CHARACTER_CANDIDATES * len(characters),
                "locked": selection["locked"],
                "awaiting_selection": True,
                "created": 0, "reused": 0, "scenes": 0,
            }
        ctx["cast_selection"] = selection
        self._plan_seed(ctx, "character_art", [
            {"id": f"char:{c['name']}", "category": "character_art",
             "label": f"{c['name']}({c.get('role') or '角色'})",
             "name": c["name"],
             "image_quality": "high", "recommended_quality": "high",
             "quality_source": "auto", "quality_rule": "mother_asset",
             "quality_reasons": ["人物母资产会被后续全部镜头引用"],
             "prompt": self._portrait_prompt(
                 c["name"], c.get("role", ""), style,
                 design=designs.get(c["name"]))}
            for c in characters])
        self._plan_seed(ctx, "character_sheet", [
            {"id": f"sheet:{c['name']}:{key}",
             "category": "character_sheet",
             "label": f"{c['name']} · {label}",
             "name": c["name"], "sheet": key,
             "image_quality": "high", "recommended_quality": "high",
             "quality_source": "auto", "quality_rule": "mother_asset",
             "quality_reasons": ["人物母资产会被后续全部镜头引用"],
             "prompt": self._sheet_prompt(
                 c["name"], c.get("role", ""), style, label, desc,
                 key=key, design=designs.get(c["name"]))}
            for c in characters
            for key, label, desc in CHARACTER_SHEETS])
        self._plan_seed(ctx, "scene_art", [
            {"id": f"scene:{loc}", "category": "scene_art",
             "label": loc, "name": loc,
             "prompt": self._scene_prompt(loc, style),
             **self._quality_meta(scene_quality[loc])}
            for loc in locations])
        reused, created = 0, 0
        cast = []

        # 最终立绘只能来自人工锁定的候选；此处绝不再从文字直接生成。
        for character in characters:
            name = character["name"]
            self.assets.acquire(
                project_id, "character", name,
                meta={"role": character.get("role", "")})
            cast.append(name)
            locked = self._locked_identity(project_id, name)
            if locked:
                reused += 1
                self._plan_mark(ctx, f"char:{name}", "reused",
                                only_pending=True,
                                extra={"selected": True,
                                       "identity_version": locked["version"]})
                continue
            raise AifosError(f"角色{name}尚未锁定最终立绘")
        # 场景可与人物资产套件继续并行；人物出图全部引用最终身份锚点。
        tasks = []
        for scene in ctx["script"]["scenes"]:
            location = scene["location"]
            self.assets.acquire(project_id, "scene", location)
            existing_scene = self._existing_asset_uri(
                ctx, "scene_art", location)
            if existing_scene:
                row = self.assets.latest(project_id, "scene_art", location)
                if self._quality_meets(
                        self._asset_quality(row),
                        scene_quality[location]["level"]):
                    reused += 1
                    self._plan_mark(ctx, f"scene:{location}", "reused",
                                    only_pending=True)
                    continue
            if any(t["tag"] == ("scene", location, "") for t in tasks):
                continue
            tasks.append({
                "item_id": f"scene:{location}", "capability": "image",
                "payload": {
                    "scene_art": True, "art_name": location,
                    "image_task_class": image_task_class_for(
                        scene_quality[location]["level"]),
                    "image_quality": scene_quality[location]["level"],
                    "quality_decision": scene_quality[location],
                    "shot_no": 0, "characters": [], "location": location,
                    "action": scene.get("action", ""),
                    "prompt": self._scene_prompt(location, style),
                    "style": style,
                    "reference_images": self._reference_uris(
                        project_id, [location]),
                    "style_ref": self._style_anchor_uri(project_id),
                    "aspect": ctx["aspect"], **ctx["dims"],
                }, "sub_dir": "cast", "tag": ("scene", location, "")})
        for tag, result in self._run_parallel(
                ctx, tasks, line="场景概念图").items():
            kind, name, role = tag
            self.assets.register(
                project_id, "scene_art", name, uri=result.uri,
                meta=self._quality_meta(scene_quality[name]))
            created += 1
        # 阶段3:人物资产套件产线 并行批量(引用各自立绘+风格基准图)
        tasks = []
        for character in characters:
            name = character["name"]
            role = character.get("role", "")
            portrait = self.assets.latest(project_id, "character_art", name)
            portrait_uri = (portrait["uri"]
                            if portrait and portrait["uri"]
                            and Path(portrait["uri"]).exists() else None)
            reference = self._reference_uris(project_id, [name])
            for key, label, desc in CHARACTER_SHEETS:
                asset_name = f"{name}:{key}"
                existing_sheet = self._existing_asset_uri(
                    ctx, "character_sheet", asset_name)
                if existing_sheet:
                    row = self.assets.latest(
                        project_id, "character_sheet", asset_name)
                    if self._quality_meets(
                            self._asset_quality(row), "high"):
                        reused += 1
                        self._plan_mark(
                            ctx, f"sheet:{name}:{key}", "reused",
                            only_pending=True)
                        continue
                tasks.append({
                    "item_id": f"sheet:{name}:{key}",
                    "capability": "image",
                    "payload": {
                        "character_sheet": key, "sheet_label": label,
                        "image_task_class": "important",
                        "image_quality": "high",
                        "art_name": name, "role": role,
                        "shot_no": 0, "characters": [name], "location": "",
                        "prompt": self._sheet_prompt(
                            name, role, style, label, desc,
                            key=key, design=designs.get(name)),
                        "style": style,
                        "character_refs": (
                            [portrait_uri] if portrait_uri else []),
                        "identity_references": self._identity_references(
                            project_id, [name]),
                        "require_reference_images": True,
                        "reference_images": reference,
                        "style_ref": self._style_anchor_uri(project_id),
                        "aspect": ctx["aspect"], **ctx["dims"],
                    }, "sub_dir": "cast",
                    "tag": (name, key, label),
                    # 人物资产套件仍属于初始母资产阶段，不做视觉 QC；
                    # 分镜/首尾帧等后续镜头才使用已锁定人物立绘质检。
                })
        for (name, key, label), result in self._run_parallel(
                ctx, tasks, line="人物资产套件").items():
            self.assets.register(
                project_id, "character_sheet", f"{name}:{key}",
                uri=result.uri,
                meta={"character": name, "sheet": key, "label": label,
                      "image_quality": "high",
                      "recommended_quality": "high",
                      "quality_source": "auto",
                      "quality_rule": "mother_asset"})
            created += 1
        ctx["cast"] = cast
        return {"reused": reused, "created": created,
                "characters": len(cast),
                "scenes": len(ctx["script"]["scenes"])}

    def _scene_locations(self, ctx):
        return {s["scene_no"]: s["location"]
                for s in ctx["script"]["scenes"]}

    def _reference_uris(self, project_id, attach_names):
        """用户上传的参考图:关联到指定角色/场景的 + 全局的。"""
        uris = []
        for row in self.assets.active_list(project_id, kind="reference"):
            meta = json.loads(row["meta"] or "{}") if isinstance(
                row["meta"], str) else (row["meta"] or {})
            attach = meta.get("attach_to", "")
            if attach and attach not in (attach_names or []):
                continue
            if row["uri"] and Path(row["uri"]).exists():
                uris.append(row["uri"])
        return uris

    def _matching_produced_image_rows(self, project_id, characters,
                                      location, shot_no=None, limit=3):
        """从资产中心找同人物/同场景的正式成图，优先作为连续性参考。"""
        wanted = set(characters or [])
        ranked = []
        for row in self.assets.active_list(project_id):
            if row["kind"] not in ("image", "first_frame", "last_frame"):
                continue
            if not formal_reference_allowed(self._asset_quality(row)):
                continue
            meta = self._asset_meta(row)
            if shot_no is not None and meta.get("shot_no") == shot_no:
                continue
            uri = row["uri"]
            if (not uri.startswith(("http://", "https://"))
                    and not Path(uri).exists()):
                continue
            row_chars = set(meta.get("characters") or [])
            same_location = bool(location and meta.get("location") == location)
            overlap = len(wanted & row_chars)
            if not same_location and not overlap:
                continue
            score = (6 if same_location else 0) + overlap * 4
            if wanted and row_chars == wanted:
                score += 3
            ranked.append((score, row["id"], row))
        ranked.sort(key=lambda item: (-item[0], -item[1]))
        return [item[2] for item in ranked[:limit]]

    def _art_refs(self, ctx, characters, location, shot_no=None):
        """最终立绘/四视图/场景图/用户参考 → 真实多图参考输入。

        含人物画面缺任何一个最终立绘都直接阻断；禁止静默退化为文字生图。
        """
        project_id = ctx["project"]["id"]
        refs = {"character_refs": [], "identity_references": [],
                "asset_matches": []}
        identities = self._identity_references(
            project_id, characters, required=bool(characters))
        for identity in identities:
            refs["character_refs"].append(identity["uri"])
            refs["identity_references"].append(identity)
            refs["asset_matches"].append({
                "asset_id": identity.get("asset_id"),
                "kind": "character_identity",
                "name": identity.get("character", ""),
                "label": f"{identity.get('character', '角色')}最终立绘",
                "uri": identity["uri"],
            })
        for name in characters or []:
            row = self.assets.latest(
                project_id, "character_sheet", f"{name}:turnaround")
            if (row and formal_reference_allowed(self._asset_quality(row))
                    and row["uri"] and Path(row["uri"]).exists()):
                refs["character_refs"].append(row["uri"])
                refs["asset_matches"].append({
                    "asset_id": row["id"], "kind": row["kind"],
                    "name": row["name"], "label": f"{name}四视图",
                    "uri": row["uri"],
                })
        if location:
            row = self.assets.latest(project_id, "scene_art", location)
            if (row and formal_reference_allowed(self._asset_quality(row))
                    and row["uri"] and Path(row["uri"]).exists()):
                refs["scene_ref"] = row["uri"]
                refs["asset_matches"].append({
                    "asset_id": row["id"], "kind": row["kind"],
                    "name": row["name"], "label": f"场景:{location}",
                    "uri": row["uri"],
                })
        matched_rows = (self._matching_produced_image_rows(
            project_id, characters, location, shot_no=shot_no)
            if shot_no is not None else [])
        matched = []
        for row in matched_rows:
            matched.append(row["uri"])
            refs["asset_matches"].append({
                "asset_id": row["id"], "kind": row["kind"],
                "name": row["name"], "label": "同人物/同场景已生产图",
                "uri": row["uri"],
            })
        reference = matched + self._reference_uris(
            project_id, list(characters or []) + ([location] if location
                                                  else []))
        if reference:
            refs["reference_images"] = reference
        anchor = self._style_anchor_uri(project_id)
        if anchor:
            refs["style_ref"] = anchor
        # 只要本次已经有任何锚点，就必须路由到能真实接收图片的产线；
        # 空镜同样不能把场景/风格/用户参考静默丢掉。
        refs["require_reference_images"] = bool(
            characters or refs.get("scene_ref")
            or refs.get("reference_images") or refs.get("style_ref"))
        return refs

    def _rich_shot_prompt(self, ctx, shot, location):
        """详细出图提示词:场景 + 出场人物完整设定 + 动作 + 台词情绪 + 镜头,
        让每张分镜画面都说清楚人物是谁、在做什么、什么故事情境。"""
        project_id = ctx["project"]["id"]
        title = ctx["project"].get("title", "")
        parts = [f"漫剧《{title}》分镜画面"]
        if location:
            parts.append(f"场景:{location}")
        who = []
        for name in shot.get("characters", []):
            design = self._character_design(project_id, name)
            line = self._design_line(design, keys=(
                "species", "appearance", "hair", "eyes", "costume",
                "signature", "temperament")) if design else ""
            who.append(f"{name}({line})" if line else name)
        if who:
            parts.append(
                f"出场人物(严格共{len(who)}人,形态与设定一致):"
                + ";".join(who))
        else:
            parts.append("环境空镜,画面中无人物")
        action = shot.get("description") or shot.get("prompt", "")
        if action:
            parts.append(f"本镜动作/画面:{action}")
        dialogue = shot.get("dialogue")
        if isinstance(dialogue, dict) and dialogue.get("dialogue"):
            speaker = dialogue.get("character", "")
            emo = (shot.get("speech_timing") or {}).get("emotion", "")
            parts.append(
                f"此刻{speaker}正说「{dialogue['dialogue']}」"
                + (f",情绪{emo},神态需体现" if emo else ",神态需体现"))
        elif shot.get("kind") == "reaction":
            parts.append("表现听者听到上一句台词后的即时反应与微表情")
        camera = shot.get("camera", "")
        if camera:
            parts.append(f"镜头语言:{camera}")
        ref = (shot.get("script_reference") or "").strip()
        if ref and ref not in action:
            parts.append(f"剧情依据:{ref}")
        return "。".join(p for p in parts if p)

    def _shot_payload(self, ctx, shot, *, continuity_anchor=False,
                      quality_override=None, item_id=None):
        locations = self._scene_locations(ctx)
        location = locations.get(shot["scene_no"], "")
        profile = (ctx.get("production_profile")
                   or (ctx.get("storyboard") or {}).get("profile")
                   or production_profile(
                       self.config, ctx.get("production_standard")))
        spatial = shot_blocking(ctx.get("blocking"), shot["shot_no"])
        readable_text = shot.get("readable_text", {}) or {}
        text_required = bool(readable_text.get("required"))
        quality = resolve_image_quality(
            recommend_shot_image_quality(
                shot, continuity_anchor=continuity_anchor),
            ctx.get("quality_policy") or default_quality_policy(),
            item_id or f"shot:{shot['shot_no']}",
            explicit_override=quality_override)
        payload = {
            "shot_no": shot["shot_no"],
            "unit_id": shot.get("unit_id"),
            "prompt": self._rich_shot_prompt(ctx, shot, location),
            "seedance_prompt": shot.get("seedance_prompt", shot["prompt"]),
            "characters": shot["characters"],
            "character_count": shot.get(
                "character_count", len(shot["characters"])),
            "location": location,
            "dialogue": shot.get("dialogue"),
            "camera": shot.get("camera", ""),
            "action": shot.get("description", ""),
            "start_state": shot.get("start_state", {}),
            "end_state": shot.get("end_state", {}),
            "five_dimensions": shot.get("five_dimensions", {}),
            "readable_text": readable_text,
            # 正式关键帧默认中档；文字/群像/人脸情绪/连续性自动升高。
            "image_task_class": image_task_class_for(
                quality["level"], readable_text=text_required),
            "image_quality": quality["level"],
            "quality_decision": quality,
            "performance": shot.get("performance", {}),
            "shot_contract": shot.get("shot_contract", {}),
            "sound_design": shot.get("sound_design", {}),
            "spatial_blocking": spatial or {},
            "spatial_constraint": (spatial or {}).get("constraint", ""),
            "standard_fingerprint": profile.get("standard_fingerprint", ""),
            "forbid_subtitles": not profile["burn_subtitles"],
            "style": ctx["project"]["style"] or "",
            "aspect": ctx["aspect"], **ctx["dims"],
            **self._art_refs(ctx, shot["characters"], location,
                             shot_no=shot["shot_no"]),
        }
        actor_ids = {
            actor.get("name"): actor.get("actor_id")
            for actor in (spatial or {}).get("actors", [])
            if actor.get("name") and actor.get("actor_id")
        }
        mapped_refs = []
        for reference in payload.get("identity_references", []):
            mapped = dict(reference)
            if actor_ids.get(mapped.get("character")):
                mapped["actor_id"] = actor_ids[mapped["character"]]
            mapped_refs.append(mapped)
        payload["identity_references"] = mapped_refs
        payload["character_reference_map"] = [{
            "actor_id": actor_ids.get(name, ""),
            "character": name,
            "uri": next((ref.get("uri") for ref in mapped_refs
                         if ref.get("character") == name), ""),
        } for name in shot.get("characters", [])]
        return payload

    def _stage_images(self, ctx):
        self._plan_seed_shots(ctx)
        ctx["images"] = []
        reused = 0
        tasks = []
        quality_by_shot = {}
        seen_scenes = set()
        for shot in ctx["storyboard"]["shots"]:
            scene_first = shot.get("scene_no") not in seen_scenes
            seen_scenes.add(shot.get("scene_no"))
            payload = self._shot_payload(ctx, shot)
            required_quality = payload["quality_decision"]["level"]
            existing = self._existing_asset_uri(
                ctx, "image", self._shot_name(ctx, shot["shot_no"]))
            if existing:
                row = self.assets.latest(
                    ctx["project"]["id"], "image",
                    self._shot_name(ctx, shot["shot_no"]))
                actual_quality = self._asset_quality(row)
                if self._quality_meets(actual_quality, required_quality):
                    ctx["images"].append(
                        {"shot_no": shot["shot_no"], "uri": existing,
                         "image_quality": actual_quality})
                    reused += 1
                    self._plan_mark(
                        ctx, f"shot:{shot['shot_no']}", "reused",
                        only_pending=True)
                    continue
            quality_by_shot[shot["shot_no"]] = payload["quality_decision"]
            tasks.append({
                "item_id": f"shot:{shot['shot_no']}",
                "capability": "image",
                "payload": payload,
                "sub_dir": "images", "tag": shot["shot_no"],
                "priority": self._shot_priority(shot, scene_first),
                "qc_spec": {**self._qc_spec(
                    ctx["project"]["id"], payload.get("characters", []),
                    location=payload.get("location", ""),
                    action=payload.get("action", ""),
                    forbid=["与设定形态不符的角色", "悬挂的衣物或衣架", "与设定不符的人"] + ["字幕条"]),
                    "camera": payload.get("camera", "")}})
        results = self._run_parallel(ctx, tasks, line="分镜画面")
        for shot_no in sorted(results):
            result = results[shot_no]
            quality = quality_by_shot[shot_no]
            self._register_shot_asset(
                ctx, "image", shot_no, result.uri,
                meta=self._shot_image_meta(
                    ctx, next(s for s in ctx["storyboard"]["shots"]
                              if s["shot_no"] == shot_no), quality))
            ctx["images"].append({
                "shot_no": shot_no, "uri": result.uri,
                "image_quality": quality["level"]})
        ctx["images"].sort(key=lambda i: i["shot_no"])
        return {"count": len(ctx["images"]), "reused": reused}

    def _stage_text_assets(self, ctx):
        """所有可读文字先由关键帧锁定；无文字单元自动通过。"""
        existing, version = self.projects.latest_document(
            ctx["episode"]["id"], "text_assets")
        if (not ctx.get("force") and existing is not None
                and existing.get("passed")):
            ctx["text_assets"] = existing
            return {"version": version, "reused": True,
                    "assets": len(existing.get("assets", [])),
                    "passed": True}
        images = {i["shot_no"]: i["uri"] for i in ctx["images"]}
        storyboard, manifest = lock_text_assets(
            ctx["storyboard"], images,
            ctx["production_profile"]["text_lock_provider"])
        if storyboard != ctx["storyboard"]:
            sb_version = self.projects.save_document(
                ctx["episode"]["id"], "storyboard", storyboard)
        else:
            _, sb_version = self.projects.latest_document(
                ctx["episode"]["id"], "storyboard")
        version = self.projects.save_document(
            ctx["episode"]["id"], "text_assets", manifest)
        ctx["storyboard"] = storyboard
        ctx["text_assets"] = manifest
        return {"version": version, "storyboard_version": sb_version,
                "assets": len(manifest["assets"]),
                "passed": manifest["passed"]}

    def _stage_frames(self, ctx):
        """首尾帧·帧链模式:同一场内「上一镜尾帧 = 下一镜首帧」,
        两段视频拼接处画面连贯;不同场之间是剪辑硬切,各自独立,
        因此按轮推进——每轮并行处理各场的第 N 镜,场内保持串行。"""
        self._plan_seed_shots(ctx)
        images = {i["shot_no"]: i for i in ctx["images"]}
        ctx["frames"] = []
        reused = 0
        chains = {}
        for shot in ctx["storyboard"]["shots"]:
            chains.setdefault(shot.get("scene_no"), []).append(shot)
        chain_list = list(chains.values())
        last_by_scene = {}
        max_len = max((len(c) for c in chain_list), default=0)
        for round_no in range(max_len):
            round_tasks = []
            for chain in chain_list:
                if round_no >= len(chain):
                    continue
                shot = chain[round_no]
                scene_no = shot.get("scene_no")
                name = self._shot_name(ctx, shot["shot_no"])
                payload = self._shot_payload(
                    ctx, shot, continuity_anchor=len(chain) > 1,
                    item_id=f"frames:{shot['shot_no']}")
                required_quality = payload["quality_decision"]["level"]
                first = self._existing_asset_uri(ctx, "first_frame", name)
                last = self._existing_asset_uri(ctx, "last_frame", name)
                if first and last:
                    first_row = self.assets.latest(
                        ctx["project"]["id"], "first_frame", name)
                    last_row = self.assets.latest(
                        ctx["project"]["id"], "last_frame", name)
                    levels = (self._asset_quality(first_row),
                              self._asset_quality(last_row))
                    frame_quality = min(
                        levels, key=("low", "medium", "high").index)
                    if self._quality_meets(
                            frame_quality, required_quality):
                        ctx["frames"].append({
                            "shot_no": shot["shot_no"], "first": first,
                            "last": last, "image_quality": frame_quality})
                        reused += 1
                        self._plan_mark(ctx, f"frames:{shot['shot_no']}",
                                        "reused", only_pending=True)
                        last_by_scene[scene_no] = {
                            "uri": last, "image_quality": frame_quality}
                        continue
                image = images[shot["shot_no"]]
                if formal_reference_allowed(
                        image.get("image_quality", "medium")):
                    payload["image_uri"] = image["uri"]
                else:
                    payload["draft_image_rejected"] = image["uri"]
                chain_first = last_by_scene.get(scene_no)
                if (round_no > 0 and chain_first
                        and formal_reference_allowed(
                            chain_first.get("image_quality", "medium"))
                        and Path(chain_first["uri"]).exists()):
                    payload["chain_first_uri"] = chain_first["uri"]
                round_tasks.append({
                    "item_id": f"frames:{shot['shot_no']}",
                    "capability": "frames",
                    "payload": payload,
                    "sub_dir": "frames", "tag": shot["shot_no"],
                    "priority": self._shot_priority(
                        shot, scene_first=round_no == 0),
                    "scene": scene_no})
            if not round_tasks:
                continue
            results = self._run_parallel(
                ctx, round_tasks,
                line=f"首尾帧帧链(第{round_no + 1}轮·各场并行)")
            for task in round_tasks:
                result = results.get(task["tag"])
                if result is None:
                    continue
                shot_no = task["tag"]
                decision = task["payload"]["quality_decision"]
                meta = self._quality_meta(decision)
                self._register_shot_asset(
                    ctx, "first_frame", shot_no, result.data["first"],
                    meta=meta)
                self._register_shot_asset(
                    ctx, "last_frame", shot_no, result.data["last"],
                    meta=meta)
                ctx["frames"].append({
                    "shot_no": shot_no,
                    "first": result.data["first"],
                    "last": result.data["last"],
                    "image_quality": decision["level"],
                })
                last_by_scene[task["scene"]] = {
                    "uri": result.data["last"],
                    "image_quality": decision["level"],
                }
        ctx["frames"].sort(key=lambda f: f["shot_no"])
        return {"count": len(ctx["frames"]), "reused": reused}

    def _stage_preflight(self, ctx):
        """确认前硬门禁：任一项未过都不能消耗 Seedance 额度。"""
        report = build_preflight(
            ctx["script"], ctx["storyboard"], ctx["continuity"],
            ctx["text_assets"], ctx["frames"], ctx["production_profile"],
            ctx.get("blocking"), ctx.get("quality_policy"))
        version = self.projects.save_document(
            ctx["episode"]["id"], "preflight", report)
        (ctx["out_root"] / "preflight_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8")
        ctx["preflight"] = report
        if not report["passed"]:
            failed = [g["label"] for g in report["gates"]
                      if not g["passed"] and g.get("severity") != "warning"]
            raise AifosError("生产门禁未通过: " + "、".join(failed))
        return {"version": version, "passed": True,
                "gates": len(report["gates"]), "units": report["units"]}

    def _stage_videos(self, ctx):
        frames = {f["shot_no"]: f for f in ctx["frames"]}
        ctx["videos"] = []
        reused = 0
        for shot in ctx["storyboard"]["shots"]:
            name = self._shot_name(ctx, shot["shot_no"])
            existing = self._existing_asset_uri(ctx, "video", name)
            if existing:
                row = self.assets.latest(
                    ctx["project"]["id"], "video", name)
                meta = json.loads(row["meta"]) if row else {}
                ctx["videos"].append({
                    "shot_no": shot["shot_no"], "uri": existing,
                    "duration": shot["duration"],
                    "provider": meta.get("provider", ""),
                    "audio_in_video": meta.get("audio_in_video"),
                    "video_quality": meta.get("video_quality", "medium"),
                    "video_resolution": meta.get(
                        "video_resolution", "720p")})
                reused += 1
                continue
            ctx["videos"].append(self._make_video(ctx, shot, frames))
        return {"count": len(ctx["videos"]), "reused": reused}

    def set_video_references(self, episode_id, shot_no, asset_ids):
        """保存某镜头从资产中心人工选定的 Seedance 参考图。"""
        episode = self.projects.get_episode(int(episode_id))
        if episode is None:
            raise AifosError("剧集不存在")
        storyboard, _ = self.projects.latest_document(
            episode["id"], "storyboard")
        shots = (storyboard or {}).get("shots", [])
        if not any(int(shot.get("shot_no", -1)) == int(shot_no)
                   for shot in shots):
            raise AifosError(f"镜头不存在: {shot_no}")
        unique_ids = []
        for value in asset_ids or []:
            asset_id = int(value)
            if asset_id not in unique_ids:
                unique_ids.append(asset_id)
        # multimodal2video 最多 9 张；首尾帧占 2 张。
        if len(unique_ids) > 7:
            raise AifosError("每个镜头最多选择 7 张资产参考图")
        selected = []
        for asset_id in unique_ids:
            row = self.assets.get(asset_id)
            if row is None or row["project_id"] != episode["project_id"]:
                raise AifosError(f"资产不存在或不属于本项目: {asset_id}")
            latest = self.assets.latest(
                row["project_id"], row["kind"], row["name"])
            if (latest is None or latest["id"] != row["id"]
                    or self.assets.is_deleted(row) or not row["uri"]):
                raise AifosError(f"资产已删除或不是最新版本: {asset_id}")
            if row["kind"] not in IMAGE_ASSET_KINDS:
                raise AifosError(f"资产不是可用图片: {row['kind']}")
            if not formal_reference_allowed(self._asset_quality(row)):
                raise AifosError(f"低质量候选图不能交给 Seedance: {row['name']}")
            uri = row["uri"]
            if (not uri.startswith(("http://", "https://"))
                    and not Path(uri).exists()):
                raise AifosError(f"资产文件不存在: {row['name']}")
            selected.append({
                "asset_id": row["id"], "kind": row["kind"],
                "name": row["name"], "version": row["version"],
            })
        document, _ = self.projects.latest_document(
            episode["id"], "video_references")
        document = document or {
            "schema": "aifos.video-references/v1", "shots": {}}
        document.setdefault("shots", {})[str(int(shot_no))] = selected
        document["updated_at"] = now()
        version = self.projects.save_document(
            episode["id"], "video_references", document)
        return {**document, "version": version}

    def _video_reference_rows(self, ctx, shot_no):
        document, _ = self.projects.latest_document(
            ctx["episode"]["id"], "video_references")
        selected = (document or {}).get("shots", {}).get(str(int(shot_no)), [])
        rows = []
        for item in selected:
            row = self.assets.get(item.get("asset_id"))
            if row is None or row["project_id"] != ctx["project"]["id"]:
                continue
            latest = self.assets.latest(
                row["project_id"], row["kind"], row["name"])
            if (latest is None or latest["id"] != row["id"]
                    or self.assets.is_deleted(row) or not row["uri"]
                    or not formal_reference_allowed(self._asset_quality(row))):
                continue
            uri = row["uri"]
            if (uri.startswith(("http://", "https://"))
                    or Path(uri).exists()):
                rows.append(row)
        return rows[:7]

    def _make_video(self, ctx, shot, frames):
        frame = frames[shot["shot_no"]]
        if not formal_reference_allowed(
                frame.get("image_quality", "medium")):
            raise AifosError(
                f"镜头{shot['shot_no']}首尾帧为低质量试错图，禁止交给 Seedance")
        quality = resolve_video_quality(
            ctx.get("quality_policy") or default_quality_policy(),
            shot_no=shot["shot_no"])
        reference_rows = self._video_reference_rows(ctx, shot["shot_no"])
        reference_assets = [{
            "asset_id": row["id"], "kind": row["kind"],
            "name": row["name"], "version": row["version"],
        } for row in reference_rows]
        result = self._call(ctx, "video", {
            "shot_no": shot["shot_no"],
            "unit_id": shot.get("unit_id"),
            "prompt": shot.get("seedance_prompt", shot["prompt"]),
            "duration": shot["duration"],
            "first": frame["first"],
            "last": frame["last"],
            "reference_images": [row["uri"] for row in reference_rows],
            "reference_assets": reference_assets,
            "dialogue": shot.get("dialogue"),
            "voice": ctx["production_profile"]["voice"],
            "lip_sync": ctx["production_profile"]["lip_sync"],
            "forbid_subtitles": not ctx["production_profile"]["burn_subtitles"],
            "video_quality": quality["level"],
            "video_resolution": quality["resolution"],
            "standard_fingerprint": ctx["production_profile"].get(
                "standard_fingerprint", ""),
            "aspect": ctx["aspect"], **ctx["dims"],
        }, "videos")
        provider = self.router.providers.get(result.provider)
        audio_in_video = bool(
            provider and provider.conf.get("audio_in_video"))
        # mock 是正式有声产线的离线契约模拟；它会把实际执行的
        # voice/lip_sync 回写结果，用结果而不是仅凭 production profile 判定。
        if (provider and "audio_in_video" not in provider.conf
                and provider.conf.get("type") == "mock"
                and result.data.get("voice") == "jimeng_builtin"
                and result.data.get("lip_sync")):
            audio_in_video = True
        self._register_shot_asset(ctx, "video", shot["shot_no"], result.uri,
                                  meta={"provider": result.provider,
                                        "audio_in_video": audio_in_video,
                                        "video_quality": quality["level"],
                                        "video_resolution": quality["resolution"],
                                        "quality_source": quality["source"],
                                        "reference_assets": reference_assets})
        return {"shot_no": shot["shot_no"], "uri": result.uri,
                "duration": shot["duration"], "provider": result.provider,
                "audio_in_video": audio_in_video,
                "video_quality": quality["level"],
                "video_resolution": quality["resolution"],
                "reference_assets": reference_assets}

    def _video_audio_states(self, ctx):
        """按实际视频资产/Provider 声明返回每镜是否内置配音。"""
        states = []
        for video in ctx.get("videos") or []:
            provider = self.router.providers.get(video.get("provider", ""))
            declared = (video.get("audio_in_video")
                        if video.get("audio_in_video") is not None
                        else (provider.conf.get("audio_in_video")
                              if provider is not None else False))
            states.append(bool(declared))
        return states

    def _videos_carry_audio(self, ctx):
        """视频是否全部自带配音(Seedance2 有声视频)。"""
        states = self._video_audio_states(ctx)
        if not states:
            return False
        return all(states)

    def _stage_voices(self, ctx):
        ctx["voices"] = []
        ctx["subtitles"] = []
        ctx["voice_mode"] = ctx["production_profile"]["voice"]
        ctx["lip_sync"] = ctx["production_profile"]["lip_sync"]
        lines = sum(len(scene.get("lines", []))
                    for scene in ctx["script"].get("scenes", []))
        audio_states = self._video_audio_states(ctx)
        all_videos_carry_audio = self._videos_carry_audio(ctx)
        if audio_states and any(audio_states) and not all(audio_states):
            raise AifosError(
                "同一集禁止混用有声与无声视频 Provider：会造成重复配音、"
                "口型错位或部分镜头无声")
        # 标准漫剧产线的声音与口型在即梦视频单元内完成，不再生成独立
        # 对白字幕或二次 TTS，避免音色、嘴型与镜头时长三者漂移。
        if all_videos_carry_audio:
            ctx["voice_carried"] = True
            self._task_providers.add("随视频配音(seedance2)")
            self.log.info(
                "director", "Seedance2 有声视频内置配音与口型，"
                "跳过独立 TTS 和对白字幕轨")
            return {"mode": "jimeng_builtin", "count": 0,
                    "reused": 0, "lines": lines,
                    "lip_sync": bool(ctx["lip_sync"]), "subtitles": 0,
                    "integrated_in_video": True,
                    "carried_by_video": True,
                    "provider_audio_confirmed": True}
        if (ctx["production_profile"].get("pipeline_version")
                == PIPELINE_VERSION
                and ctx["voice_mode"] == "jimeng_builtin"):
            raise AifosError(
                "SK V3.2 专业标准要求所有视频随 Seedance2 内置"
                "配音与对口型；当前视频 Provider 未声明 "
                "audio_in_video，已阻止错位的独立 TTS")
        line_no = 0
        reused = 0
        for scene in ctx["script"]["scenes"]:
            for line in scene["lines"]:
                line_no += 1
                name = self._line_name(ctx, line_no)
                existing = self._existing_asset_uri(ctx, "voice", name)
                if existing:
                    row = self.assets.latest(
                        ctx["project"]["id"], "voice", name)
                    meta = json.loads(row["meta"]) if row else {}
                    ctx["voices"].append({
                        "line_no": line_no, "uri": existing,
                        "duration": meta.get("duration") or round(
                            max(1.0, len(line["dialogue"]) * 0.18), 2)})
                    reused += 1
                else:
                    ctx["voices"].append(
                        self._make_voice(ctx, line_no, line))
                if ctx["production_profile"]["burn_subtitles"]:
                    ctx["subtitles"].append({
                        "line_no": line_no,
                        "character": line["character"],
                        "text": line["dialogue"],
                    })
        return {"mode": ctx["voice_mode"],
                "count": len(ctx["voices"]), "reused": reused,
                "lines": lines, "lip_sync": bool(ctx["lip_sync"]),
                "subtitles": len(ctx["subtitles"]),
                "integrated_in_video": False}

    def _make_voice(self, ctx, line_no, line):
        result = self._call(ctx, "voice", {
            "line_no": line_no,
            "character": line["character"],
            "text": line["dialogue"],
        }, "voices")
        self.assets.register(
            ctx["project"]["id"], "voice",
            f"e{ctx['episode']['number']:03d}_line{line_no:03d}",
            uri=result.uri,
            meta={"duration": result.data.get("duration", 0)})
        return {"line_no": line_no, "uri": result.uri,
                "duration": result.data.get("duration", 0)}

    def _stage_edit(self, ctx):
        result = self._call(ctx, "edit", {
            "shots": ctx["videos"],
            "voices": ctx["voices"],
            "subtitles": [] if not ctx["production_profile"]["burn_subtitles"]
            else ctx["subtitles"],
            "voice_mode": ctx.get("voice_mode", ""),
            "lip_sync": ctx.get("lip_sync", False),
            "forbid_subtitles": not ctx["production_profile"]["burn_subtitles"],
            "project_title": ctx["project"]["title"],
            "episode_number": ctx["episode"]["number"],
            "aspect": ctx["aspect"], **ctx["dims"],
        }, "edit")
        ctx["final_uri"] = result.uri
        ctx["edit_data"] = result.data
        self.assets.register(
            ctx["project"]["id"], "edit",
            f"e{ctx['episode']['number']:03d}_final", uri=result.uri,
            meta=result.data)
        return result.data

    def _stage_qc(self, ctx):
        """自动检查 + 图文检查板 + 逐段内容复核 + 交付脚本。"""
        content_review = build_content_review(
            ctx["script"], ctx["storyboard"], ctx["continuity"])
        content_path = ctx["out_root"] / "content_review.json"
        content_path.write_text(
            json.dumps(content_review, ensure_ascii=False, indent=2),
            encoding="utf-8")
        self.projects.save_document(
            ctx["episode"]["id"], "content_review", content_review)
        review_board = write_review_board(ctx, content_review)
        ctx["content_review"] = content_review
        ctx["review_board"] = review_board
        ep_name = f"e{ctx['episode']['number']:03d}"
        self.assets.register(
            ctx["project"]["id"], "review_board", ep_name,
            uri=review_board, meta={"passed": content_review["passed"]})
        max_retries = self.config.get("retry", "max_retries", default=2)
        report = None
        for attempt in range(max_retries + 1):
            report = self.qc.run(ctx["script"], ctx["storyboard"], ctx)
            self.log.info(
                "qc", f"质检第{attempt + 1}轮:得分 {report['score']}"
                f"(线 {report['pass_score']}),问题 {len(report['issues'])}")
            # 只要存在可自动重跑的缺失产物(即使总分达标)就先修复再定论
            fixable = report["rerun_shots"] or report["rerun_lines"]
            if not fixable or attempt == max_retries:
                break
            self._rerun(ctx, report)
        delivery = write_delivery_verifier(
            ctx, review_board, content_review)
        if not delivery.get("passed"):
            report["issues"].append({
                "check": "delivery", "severity": "error",
                "shot_no": None, "line_no": None, "rerunnable": False,
                "message": "交付复核脚本未通过",
            })
            report["score"] = max(0, report["score"] - 15)
            report["passed"] = False
        report["content_review"] = content_review
        report["review_board"] = review_board
        report["delivery_check"] = delivery
        report["technical_passed"] = not any(
            i["severity"] == "error" and i["check"] not in ("content",)
            for i in report["issues"])
        report["content_passed"] = content_review["passed"]
        ctx["qc_report"] = report
        self.projects.set_qc_score(ctx["episode"]["id"], report["score"])
        report_path = ctx["out_root"] / "qc_report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8")
        if not report["passed"]:
            self.log.warn(
                "qc", f"质检未通过(得分 {report['score']}),"
                "不可自动修复的问题已写入报告")
        return {"score": report["score"], "passed": report["passed"],
                "issues": len(report["issues"]),
                "content_passed": content_review["passed"],
                "delivery_passed": delivery.get("passed", False)}

    def _rerun(self, ctx, report):
        shots = {s["shot_no"]: s for s in ctx["storyboard"]["shots"]}
        frames = {f["shot_no"]: f for f in ctx["frames"]}
        for shot_no in report["rerun_shots"]:
            self.log.info("director", f"自动重跑镜头 {shot_no} 的视频")
            new_video = self._make_video(ctx, shots[shot_no], frames)
            ctx["videos"] = [
                new_video if v["shot_no"] == shot_no else v
                for v in ctx["videos"]]
            self.data.record(
                "case", "failure", prompt=shots[shot_no]["prompt"],
                meta={"reason": "qc_rerun", "shot_no": shot_no},
                episode_id=ctx["episode"]["id"])
        lines = {}
        line_no = 0
        for scene in ctx["script"]["scenes"]:
            for line in scene["lines"]:
                line_no += 1
                lines[line_no] = line
        for ln in report["rerun_lines"]:
            self.log.info("director", f"自动重跑台词 {ln} 的配音")
            new_voice = self._make_voice(ctx, ln, lines[ln])
            ctx["voices"] = [
                new_voice if v["line_no"] == ln else v
                for v in ctx["voices"]]
        self._stage_edit(ctx)

    def _stage_package(self, ctx):
        if not ctx.get("qc_report", {}).get("passed", False):
            self.log.warn("ops", "质检未通过,跳过封面/标题/拆条")
            return {"skipped": True}
        ep_name = f"e{ctx['episode']['number']:03d}"
        existing_cover = self._existing_asset_uri(ctx, "cover", ep_name)
        if existing_cover:
            ctx["cover_uri"] = existing_cover
        else:
            cover_characters = [c.get("name") for c in
                                ctx["script"].get("characters", [])
                                if c.get("name")]
            identity_refs = self._identity_references(
                ctx["project"]["id"], cover_characters,
                required=bool(cover_characters))
            cover = self.ops.make_cover(
                ctx["script"], ctx["out_root"] / "ops", aspect=ctx["aspect"],
                identity_references=identity_refs)
            self._task_cost += cover.cost
            self._task_providers.add(cover.provider)
            self.projects.add_episode_cost(ctx["episode"]["id"], cover.cost)
            ctx["cover_uri"] = cover.uri
        ctx["titles"] = self.ops.make_titles(
            ctx["script"], kind=ctx["project"]["kind"])
        ctx["clips"] = self.ops.make_clips(
            ctx["storyboard"], ctx["out_root"] / "ops")
        project_id = ctx["project"]["id"]
        self.assets.register(
            project_id, "cover", ep_name, uri=ctx["cover_uri"])
        self.assets.register(
            project_id, "title", ep_name, meta={"candidates": ctx["titles"]})
        for clip in ctx["clips"]:
            self.assets.register(
                project_id, "clip", f"{ep_name}_scene{clip['scene_no']:02d}",
                uri=clip["uri"])
        # 发布包:成片/封面/标题/话题标签一站式,供人工一键上传
        kit = self.ops.make_publish_kit(
            ctx["project"], ctx["episode"], ctx,
            ctx["out_root"] / "publish")
        ctx["publish_kit"] = kit
        return {"titles": len(ctx["titles"]), "clips": len(ctx["clips"]),
                "cover_reused": bool(existing_cover),
                "hashtags": len(kit["hashtags"])}

    def _stage_archive(self, ctx):
        """数据沉淀:Prompt、图片、视频、配音、成/败案例入库。"""
        episode_id = ctx["episode"]["id"]
        passed = ctx.get("qc_report", {}).get("passed", False)
        label = "success" if passed else "failure"
        for shot in ctx["storyboard"]["shots"]:
            self.data.record(
                "prompt", label, prompt=shot["prompt"],
                meta={"shot_no": shot["shot_no"]}, episode_id=episode_id)
        for image in ctx.get("images", []):
            self.data.record(
                "image", label, uri=image["uri"],
                meta={"shot_no": image["shot_no"]}, episode_id=episode_id)
        for video in ctx.get("videos", []):
            self.data.record(
                "video", label, uri=video["uri"],
                meta={"shot_no": video["shot_no"]}, episode_id=episode_id)
        for voice in ctx.get("voices", []):
            self.data.record(
                "voice", label, uri=voice["uri"],
                meta={"line_no": voice["line_no"]}, episode_id=episode_id)
        if not ctx.get("voices") and ctx.get("voice_mode") == "jimeng_builtin":
            self.data.record(
                "voice", label, uri=ctx.get("final_uri", ""),
                meta={"mode": "jimeng_builtin", "lip_sync": True,
                      "integrated_in_video": True}, episode_id=episode_id)
        self.data.record(
            "review", label, uri=ctx.get("review_board", ""),
            meta={"content_passed": ctx.get("content_review", {}).get("passed"),
                  "delivery_passed": ctx.get("qc_report", {}).get(
                      "delivery_check", {}).get("passed")},
            episode_id=episode_id)
        self.data.record(
            "case", label,
            prompt=f"《{ctx['project']['title']}》第{ctx['episode']['number']}集",
            uri=ctx.get("final_uri", ""),
            meta={
                "qc_score": ctx.get("qc_report", {}).get("score"),
                "cost": self.projects.get_episode(episode_id)["cost"],
                "standard_version": ctx.get("production_profile", {}).get(
                    "standard_version"),
                "standard_fingerprint": ctx.get(
                    "production_profile", {}).get("standard_fingerprint", ""),
            },
            episode_id=episode_id)
        return {"label": label}

    def _register_shot_asset(self, ctx, kind, shot_no, uri, meta=None):
        self.assets.register(
            ctx["project"]["id"], kind,
            f"e{ctx['episode']['number']:03d}_shot{shot_no:03d}", uri=uri,
            meta=meta)

    # ---- 打磨:剧本意见重写 / 单张图片附意见重画 ----
    def revise_script(self, project_title, episode_number, feedback,
                      run_id=None):
        """按修改意见重写剧本并重跑预生产,回到待确认。"""
        project = self.projects.get_project(project_title)
        if project is not None:
            episode = self.db.query_one(
                "SELECT * FROM episodes WHERE project_id=? AND number=?",
                (project["id"], episode_number))
            if episode is not None:
                self.data.record(
                    "case", "failure", prompt=feedback,
                    meta={"reason": "script_revision"},
                    episode_id=episode["id"])
        return self.produce(
            project_title, episode_number, force=True,
            pause_for_confirm=True, feedback=feedback, run_id=run_id)

    def regen_image(self, project_title, episode_number, target,
                    feedback="", prompt_override="", quality_override=None,
                    revision_source="manual"):
        """重画单张图:target = {"kind": character_art|scene_art|shot,
        "name"|"shot_no"};附意见时新画面按意见调整;
        prompt_override 非空则整句替换默认提示词(所见即所得)。
        镜头画面重画会连带重生成首尾帧并作废旧视频(补齐时重拍)。"""
        project = self.projects.get_project(project_title)
        if project is None:
            raise AifosError(f"项目不存在: {project_title}")
        episode = self.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=?",
            (project["id"], episode_number))
        if episode is None:
            raise AifosError(f"剧集不存在: 第{episode_number}集")
        script, _ = self.projects.latest_document(episode["id"], "script")
        if script is None:
            raise AifosError("本集尚无剧本,先完成预生产")
        aspect = (project["aspect"]
                  or self.config.get("defaults", "aspect", default="9:16"))
        standard, _ = self.projects.latest_document(
            episode["id"], "production_standard")
        blocking, _ = self.projects.latest_document(
            episode["id"], "blocking")
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "out_root": self._episode_dir(project, episode),
            "aspect": aspect,
            "dims": ASPECT_DIMS.get(aspect, ASPECT_DIMS["9:16"]),
            "script": script, "force": True,
            "production_standard": standard,
            "production_profile": production_profile(self.config, standard),
            "blocking": blocking,
        }
        self._task_cost = 0.0
        self._task_providers = set()
        style = project["style"] or DEFAULT_VISUAL_STYLE
        kind = target.get("kind")
        prompt_override = (prompt_override or "").strip()
        quality_choice = (normalize_quality(
            quality_override, allow_auto=True, field="image_quality")
            if quality_override is not None else "auto")
        item_id = {
            "character_sheet": lambda: f"sheet:{target.get('name', '')}",
            "scene_art": lambda: f"scene:{target.get('name', '')}",
            "shot": lambda: f"shot:{int(target.get('shot_no', 0))}",
            "frames": lambda: f"frames:{int(target.get('shot_no', 0))}",
        }.get(kind, lambda: "")()
        policy = self._episode_quality_policy(episode["id"])
        if quality_override is not None and item_id:
            policy = set_policy_choices(
                policy, image_overrides={item_id: quality_choice})
            self.projects.save_document(
                episode["id"], "quality_policy", policy)
        ctx["quality_policy"] = policy

        def next_revision(asset_kind, asset_name):
            """每次重画都进入提示词/占位种子,保证重画必然产生新画面。"""
            row = self.assets.latest(project["id"], asset_kind, asset_name)
            return (row["version"] + 1) if row else 1

        if kind == "character_art":
            raise AifosError(
                "最终立绘不能从文字直接重画。请重新生成5张人物候选并人工定版，"
                "或上传经过确认的最终立绘")
        elif kind == "character_sheet":
            raw = target["name"]
            if ":" not in raw:
                raise AifosError(f"资产名需为 角色:套件键,收到 {raw}")
            name, sheet_key = raw.split(":", 1)
            entry = next((s for s in CHARACTER_SHEETS
                          if s[0] == sheet_key), None)
            if entry is None:
                raise AifosError(f"未知人物资产套件: {sheet_key}")
            _, label, desc = entry
            role = next((c.get("role", "") for c in script["characters"]
                         if c["name"] == name), "")
            portrait = self.assets.latest(
                project["id"], "character_art", name)
            portrait_uri = (portrait["uri"]
                            if portrait and portrait["uri"]
                            and Path(portrait["uri"]).exists() else None)
            prompt = prompt_override or self._sheet_prompt(
                name, role, style, label, desc, key=sheet_key,
                design=self._character_design(project["id"], name))
            quality = resolve_image_quality(
                recommend_asset_quality("character_sheet"), policy,
                f"sheet:{raw}", explicit_override=quality_choice)
            payload = {
                    "character_sheet": sheet_key, "sheet_label": label,
                    "image_task_class": image_task_class_for(
                        quality["level"]),
                    "image_quality": quality["level"],
                    "quality_decision": quality,
                    "art_name": name, "role": role,
                    "shot_no": 0, "characters": [name], "location": "",
                    "prompt": prompt, "style": style,
                    "feedback": feedback,
                    "revision": next_revision("character_sheet", raw),
                    "character_refs": (
                        [portrait_uri] if portrait_uri else []),
                    "identity_references": self._identity_references(
                        project["id"], [name]),
                    "require_reference_images": True,
                    "reference_images": self._reference_uris(
                        project["id"], [name]),
                    "style_ref": self._style_anchor_uri(project["id"]),
                    "aspect": aspect, **ctx["dims"],
            }
            result = self._plan_run(
                ctx, f"sheet:{name}:{sheet_key}",
                lambda: self._call(ctx, "image", payload, "cast"),
                prompt=self._prompt_with_feedback(prompt, feedback),
                payload=payload, revision_source=revision_source)
            self.assets.register(
                project["id"], "character_sheet", raw, uri=result.uri,
                meta={"character": name, "sheet": sheet_key,
                      "label": label, **self._quality_meta(quality)},
                new_version=True)
        elif kind == "scene_art":
            name = target["name"]
            scene = next((s for s in script["scenes"]
                          if s["location"] == name), {})
            prompt = prompt_override or self._scene_prompt(name, style)
            references = self._reference_uris(project["id"], [name])
            style_ref = self._style_anchor_uri(project["id"])
            reuse_count = sum(1 for value in script["scenes"]
                              if value.get("location") == name)
            quality = resolve_image_quality(
                recommend_asset_quality("scene_art", reuse_count=reuse_count),
                policy, f"scene:{name}", explicit_override=quality_choice)
            payload = {
                    "scene_art": True, "art_name": name,
                    "image_task_class": image_task_class_for(
                        quality["level"]),
                    "image_quality": quality["level"],
                    "quality_decision": quality,
                    "shot_no": 0, "characters": [], "location": name,
                    "action": scene.get("action", ""),
                    "prompt": prompt,
                    "style": style, "feedback": feedback,
                    "revision": next_revision("scene_art", name),
                    "reference_images": references,
                    "style_ref": style_ref,
                    "require_reference_images": bool(
                        references or style_ref),
                    "aspect": aspect, **ctx["dims"],
            }
            result = self._plan_run(
                ctx, f"scene:{name}",
                lambda: self._call(ctx, "image", payload, "cast"),
                prompt=self._prompt_with_feedback(prompt, feedback),
                payload=payload, revision_source=revision_source)
            self.assets.register(project["id"], "scene_art", name,
                                 uri=result.uri,
                                 meta=self._quality_meta(quality),
                                 new_version=True)
        elif kind == "shot":
            shot_no = int(target["shot_no"])
            storyboard, _ = self.projects.latest_document(
                episode["id"], "storyboard")
            shot = next((s for s in storyboard["shots"]
                         if s["shot_no"] == shot_no), None)
            if shot is None:
                raise AifosError(f"镜头不存在: {shot_no}")
            ctx["storyboard"] = storyboard
            payload = self._shot_payload(
                ctx, shot, quality_override=quality_choice,
                item_id=f"shot:{shot_no}")
            payload["feedback"] = feedback
            payload["revision"] = next_revision(
                "image", self._shot_name(ctx, shot_no))
            if prompt_override:
                payload["prompt"] = prompt_override
                payload["seedance_prompt"] = prompt_override
            asset_name = self._shot_name(ctx, shot_no)
            result = self._plan_run(
                ctx, f"shot:{shot_no}",
                lambda: self._call(ctx, "image", payload, "images"),
                prompt=self._prompt_with_feedback(
                    payload["prompt"], feedback),
                payload=payload, revision_source=revision_source)
            self.assets.register(project["id"], "image", asset_name,
                                 uri=result.uri,
                                 meta=self._shot_image_meta(
                                     ctx, shot, payload["quality_decision"],
                                     {"revision": payload["revision"]}),
                                 new_version=True)
            scene_shots = [candidate for candidate in storyboard["shots"]
                           if candidate.get("scene_no") == shot.get("scene_no")]
            frames_payload = self._shot_payload(
                ctx, shot, continuity_anchor=len(scene_shots) > 1,
                item_id=f"frames:{shot_no}")
            if formal_reference_allowed(payload["image_quality"]):
                frames_payload["image_uri"] = result.uri
            else:
                frames_payload["draft_image_rejected"] = result.uri
            frames_payload["feedback"] = feedback
            frames_payload["revision"] = next_revision(
                "first_frame", asset_name)
            if prompt_override:
                frames_payload["prompt"] = prompt_override
                frames_payload["seedance_prompt"] = prompt_override
            prev = None
            for candidate in storyboard["shots"]:
                if candidate["shot_no"] >= shot_no:
                    break
                if candidate.get("scene_no") == shot.get("scene_no"):
                    prev = candidate
            if prev is not None:
                row = self.assets.latest(
                    project["id"], "last_frame",
                    self._shot_name(ctx, prev["shot_no"]))
                if (row and formal_reference_allowed(self._asset_quality(row))
                        and row["uri"] and Path(row["uri"]).exists()):
                    # 帧链衔接:重画也保持与上一镜尾帧连贯
                    frames_payload["chain_first_uri"] = row["uri"]
            frames = self._plan_run(
                ctx, f"frames:{shot_no}", lambda: self._call(
                    ctx, "frames", frames_payload, "frames"),
                prompt=self._prompt_with_feedback(
                    frames_payload["prompt"], feedback),
                payload=frames_payload, revision_source=revision_source)
            frame_meta = self._quality_meta(frames_payload["quality_decision"])
            self.assets.register(project["id"], "first_frame", asset_name,
                                 uri=frames.data["first"], meta=frame_meta,
                                 new_version=True)
            self.assets.register(project["id"], "last_frame", asset_name,
                                 uri=frames.data["last"], meta=frame_meta,
                                 new_version=True)
            # 画面变了 → 旧视频作废,「继续补齐」时重拍并重剪
            self.assets.delete(project["id"], "video", asset_name)
        elif kind == "frames":
            shot_no = int(target["shot_no"])
            storyboard, _ = self.projects.latest_document(
                episode["id"], "storyboard")
            shot = next((s for s in (storyboard or {}).get("shots", [])
                         if s["shot_no"] == shot_no), None)
            if shot is None:
                raise AifosError(f"镜头不存在: {shot_no}")
            ctx["storyboard"] = storyboard
            asset_name = self._shot_name(ctx, shot_no)
            image_row = self.assets.latest(project["id"], "image",
                                           asset_name)
            if not (image_row and image_row["uri"]
                    and Path(image_row["uri"]).exists()):
                raise AifosError(f"镜头{shot_no}尚无关键图,请先重画镜头")
            scene_shots = [candidate for candidate in storyboard["shots"]
                           if candidate.get("scene_no") == shot.get("scene_no")]
            frames_payload = {
                **self._shot_payload(
                    ctx, shot, continuity_anchor=len(scene_shots) > 1,
                    quality_override=quality_choice,
                    item_id=f"frames:{shot_no}"),
                "feedback": feedback,
                "revision": next_revision("first_frame", asset_name),
            }
            if formal_reference_allowed(self._asset_quality(image_row)):
                frames_payload["image_uri"] = image_row["uri"]
            else:
                frames_payload["draft_image_rejected"] = image_row["uri"]
            if prompt_override:
                frames_payload["prompt"] = prompt_override
                frames_payload["seedance_prompt"] = prompt_override
            prev = None
            for candidate in storyboard["shots"]:
                if candidate["shot_no"] >= shot_no:
                    break
                if candidate.get("scene_no") == shot.get("scene_no"):
                    prev = candidate
            if prev is not None:
                row = self.assets.latest(
                    project["id"], "last_frame",
                    self._shot_name(ctx, prev["shot_no"]))
                if (row and formal_reference_allowed(self._asset_quality(row))
                        and row["uri"] and Path(row["uri"]).exists()):
                    frames_payload["chain_first_uri"] = row["uri"]
            result = self._plan_run(
                ctx, f"frames:{shot_no}", lambda: self._call(
                    ctx, "frames", frames_payload, "frames"),
                prompt=self._prompt_with_feedback(
                    frames_payload["prompt"], feedback),
                payload=frames_payload, revision_source=revision_source)
            self.assets.register(project["id"], "first_frame", asset_name,
                                 uri=result.data["first"],
                                 meta=self._quality_meta(
                                     frames_payload["quality_decision"]),
                                 new_version=True)
            self.assets.register(project["id"], "last_frame", asset_name,
                                 uri=result.data["last"],
                                 meta=self._quality_meta(
                                     frames_payload["quality_decision"]),
                                 new_version=True)
            self.assets.delete(project["id"], "video", asset_name)
        else:
            raise AifosError(f"不支持的重画目标: {kind}")
        if feedback:
            self.data.record(
                "case", "failure", prompt=feedback,
                meta={"reason": "image_revision", "target": target},
                episode_id=episode["id"])
        self.log.info(
            "director",
            f"重画完成: {target}(意见: {feedback or '无'})")
        return {"target": target, "uri": result.uri,
                "quality": (result.data or {}).get(
                    "image_quality", quality_choice),
                "cost": round(self._task_cost, 2)}

    # ---- 人工修改素材导入(下载 → 外部修图/剪辑 → 上传替换) ----
    IMAGE_MAGIC = {".png": b"\x89PNG", ".jpg": b"\xff\xd8\xff",
                   ".jpeg": b"\xff\xd8\xff", ".webp": b"RIFF",
                   ".svg": b"<"}

    def _episode_ctx(self, project_title, episode_number):
        project = self.projects.get_project(project_title)
        if project is None:
            raise AifosError(f"项目不存在: {project_title}")
        episode = self.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=?",
            (project["id"], episode_number))
        if episode is None:
            raise AifosError(f"剧集不存在: 第{episode_number}集")
        return project, episode

    def _invalidate_cast_assets(self, project, script, reason):
        """让新一轮人物候选遮蔽旧版本,但保留旧文件和历史记录。"""
        for character in script.get("characters", []):
            name = character["name"]
            self.assets.register(
                project["id"], "character_identity", name, uri="",
                meta={"character": name, "locked": False,
                      "reason": reason}, new_version=True)
            for index in range(1, CHARACTER_CANDIDATES + 1):
                self.assets.register(
                    project["id"], "character_candidate",
                    f"{name}:{index:02d}", uri="",
                    meta={"character": name,
                          "role": character.get("role", ""),
                          "candidate_index": index,
                          "invalidated": reason}, new_version=True)
            for key, label, _desc in CHARACTER_SHEETS:
                self.assets.register(
                    project["id"], "character_sheet", f"{name}:{key}",
                    uri="", meta={"character": name, "sheet": key,
                                   "label": label,
                                   "invalidated": reason},
                    new_version=True)
        for location in dict.fromkeys(
                scene["location"] for scene in script.get("scenes", [])):
            self.assets.register(
                project["id"], "scene_art", location, uri="",
                meta={"invalidated": reason}, new_version=True)

    def regenerate_character_candidates(self, project_title, episode_number,
                                        run_id=None):
        """放弃当前人物选择并重新生成候选,不进入后续镜头生产。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        if episode["status"] != "awaiting_cast":
            raise AifosError("只能在人物选择阶段返回重新生成")
        script, _ = self.projects.latest_document(episode["id"], "script")
        if script is None:
            raise AifosError("本集尚无剧本,先完成剧本确认")
        self._invalidate_cast_assets(
            project, script, reason="manual_regenerate_cast")
        self.projects.set_episode_status(episode["id"], "cast")
        aspect = (project["aspect"]
                  or self.config.get("defaults", "aspect", default="9:16"))
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode),
               "script": script, "force": False, "aspect": aspect,
               "dims": ASPECT_DIMS.get(aspect, ASPECT_DIMS["9:16"]),
               "run_id": run_id,
               "quality_policy": self._episode_quality_policy(
                   episode["id"], persist=True)}
        self._task_cost = 0.0
        self._task_providers = set()
        try:
            report = self._stage_cast(ctx)
        except ProduceCancelled:
            self.projects.set_episode_status(episode["id"], "awaiting_cast")
            return {"status": "paused", "done": 0,
                    "note": "人物候选重新生成已暂停,已完成候选保留"}
        self.projects.set_episode_status(episode["id"], "awaiting_cast")
        self.log.info(
            "director", "已放弃当前人物选择,新候选已生成,等待重新定版"
            f"(episode_id={episode['id']})")
        return {"status": "awaiting_cast",
                "done": report.get("candidates", 0),
                "candidate_target": report.get("candidate_target", 0),
                "locked": 0}

    def import_image(self, project_title, episode_number, target,
                     file_bytes, ext):
        """上传替换图片:character_art / scene_art / shot(镜头画面)。
        镜头画面替换后自动按新图重做首尾帧并作废旧视频。"""
        ext = ext.lower()
        magic = self.IMAGE_MAGIC.get(ext)
        if magic is None:
            raise AifosError(f"不支持的图片格式: {ext}(png/jpg/webp/svg)")
        if not file_bytes or not file_bytes.lstrip()[:8].startswith(magic) \
                and not file_bytes.startswith(magic):
            raise AifosError("文件内容与图片格式不符")
        project, episode = self._episode_ctx(project_title, episode_number)
        out_root = self._episode_dir(project, episode)
        kind = target.get("kind")
        if kind in ("character_art", "scene_art", "character_sheet"):
            name = target["name"]
            latest = self.assets.latest(project["id"], kind, name)
            if latest is None:
                if kind != "character_art":
                    raise AifosError(f"资产不存在: {kind}/{name}")
                script, _ = self.projects.latest_document(
                    episode["id"], "script")
                known = {c.get("name") for c in
                         (script or {}).get("characters", [])}
                if name not in known:
                    raise AifosError(f"剧本中没有角色: {name}")
            version = (latest["version"] + 1) if latest else 1
            safe = "".join(c if c.isalnum() else "_" for c in name)[:40]
            path = (out_root / "cast"
                    / f"upload_{kind}_{safe}_v{version}{ext}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(file_bytes)
            uploaded_meta = {"uploaded": True, "image_quality": "high",
                             "recommended_quality": "high",
                             "quality_source": "manual_upload"}
            self.assets.register(project["id"], kind, name,
                                 uri=str(path), meta=uploaded_meta,
                                 new_version=True)
            if kind == "character_art":
                # 人工上传等同于明确确认最终立绘，同时建立真正的身份锚点。
                self.assets.register(
                    project["id"], "character_identity", name,
                    uri=str(path),
                    meta={"character": name, "locked": True,
                          "uploaded": True, "locked_at": now(),
                          "image_quality": "high",
                          "recommended_quality": "high",
                          "quality_source": "manual_upload"},
                    new_version=True)
            self.log.info("director", f"已上传替换 {kind}/{name}")
            return {"uri": str(path)}
        if kind == "shot":
            shot_no = int(target["shot_no"])
            storyboard, _ = self.projects.latest_document(
                episode["id"], "storyboard")
            script, _ = self.projects.latest_document(episode["id"], "script")
            shot = next((s for s in (storyboard or {}).get("shots", [])
                         if s["shot_no"] == shot_no), None)
            if shot is None:
                raise AifosError(f"镜头不存在: {shot_no}")
            asset_name = f"e{episode['number']:03d}_shot{shot_no:03d}"
            path = (out_root / "images"
                    / f"shot_{shot_no:03d}.upload{ext}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(file_bytes)
            aspect = (project["aspect"] or self.config.get(
                "defaults", "aspect", default="9:16"))
            ctx = {"project": dict(project), "episode": dict(episode),
                   "out_root": out_root, "aspect": aspect,
                   "dims": ASPECT_DIMS.get(aspect, ASPECT_DIMS["9:16"]),
                   "script": script, "storyboard": storyboard,
                   "force": True}
            self.assets.register(project["id"], "image", asset_name,
                                 uri=str(path),
                                 meta=self._shot_image_meta(
                                     ctx, shot,
                                     {"level": "high",
                                      "recommended": "high",
                                      "source": "manual_upload",
                                      "rule": "manual_upload",
                                      "reasons": ["人工上传正式图"]},
                                     {"uploaded": True}),
                                 new_version=True)
            # 按新图重做首尾帧(真实产线由 Codex 依据新图推导)
            self._task_cost = 0.0
            self._task_providers = set()
            payload = self._shot_payload(ctx, shot)
            frames = self._call(ctx, "frames", {
                **payload, "image_uri": str(path)}, "frames")
            self.assets.register(project["id"], "first_frame", asset_name,
                                 uri=frames.data["first"],
                                 meta={"image_quality": "high",
                                       "quality_source": "manual_upload"},
                                 new_version=True)
            self.assets.register(project["id"], "last_frame", asset_name,
                                 uri=frames.data["last"],
                                 meta={"image_quality": "high",
                                       "quality_source": "manual_upload"},
                                 new_version=True)
            self.assets.delete(project["id"], "video", asset_name)
            self.log.info(
                "director", f"已上传替换镜头{shot_no}画面,旧视频作废")
            return {"uri": str(path)}
        raise AifosError(f"不支持的上传目标: {kind}")

    def restyle_project(self, project_title, episode_number, style=None):
        """一键换画风后重新生成每人5张候选，禁止直接覆盖最终立绘。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        if style and style.strip():
            self.projects.update_project(project_title, style=style.strip())
            project = self.projects.get_project(project_title)
        script, _ = self.projects.latest_document(episode["id"], "script")
        if script is None:
            raise AifosError("本集尚无剧本,先完成剧本确认")
        # 新画风使旧身份锚点和下游人物资产失效，但保留历史版本/文件。
        # 用空的新版本遮蔽旧最新版，重新走5选1，不做破坏性删除。
        self._invalidate_cast_assets(project, script, reason="restyle")

        self.projects.set_episode_status(episode["id"], "cast")
        self.log.info(
            "director",
            f"新画风已生效，重新生成每人5张定妆候选: {project['style']}")
        aspect = (project["aspect"]
                  or self.config.get("defaults", "aspect", default="9:16"))
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode),
               "script": script, "force": False, "aspect": aspect,
               "dims": ASPECT_DIMS.get(aspect, ASPECT_DIMS["9:16"])}
        self._task_cost = 0.0
        self._task_providers = set()
        try:
            report = self._stage_cast(ctx)
        except ProduceCancelled:
            self.projects.set_episode_status(episode["id"], "awaiting_cast")
            self.log.info(
                "director",
                "换风格候选生成已暂停，已完成候选保留，可从断点继续")
            return {"status": "paused", "done": 0,
                    "style": project["style"]}
        self.projects.set_episode_status(episode["id"], "awaiting_cast")
        self.log.info(
            "director", "新画风人物候选已就绪，请逐个选定最终立绘；"
            "选定后才重做资产套件和场景")
        return {"status": "awaiting_cast",
                "done": report.get("candidates", 0),
                "style": project["style"]}

    PLAN_TARGETS = {
        "char": lambda parts: {"kind": "character_art", "name": parts[0]},
        "sheet": lambda parts: {"kind": "character_sheet",
                                "name": ":".join(parts)},
        "scene": lambda parts: {"kind": "scene_art", "name": parts[0]},
        "shot": lambda parts: {"kind": "shot", "shot_no": int(parts[0])},
        "frames": lambda parts: {"kind": "frames",
                                 "shot_no": int(parts[0])},
    }
    # 初始母资产只负责建立人物/场景基准；视觉 QC 只对后续镜头图执行。
    INITIAL_ASSET_CATEGORIES = frozenset({
        "character_candidate", "character_art", "character_sheet", "scene_art",
    })
    SHOT_QC_CATEGORIES = frozenset({"shot_image", "frames"})

    # ---- 单张/批量质检:核对已生成的图是否符合剧本要求 ----
    _FORBID = ["与设定形态不符的角色", "悬挂的衣物或衣架", "与设定不符的人"]

    def _plan_item_target(self, item_id):
        head, _, rest = item_id.partition(":")
        builder = self.PLAN_TARGETS.get(head)
        return builder(rest.split(":")) if builder else None

    def _plan_item_asset(self, project_id, ep_num, item):
        """清单条目 → (最新资产 uri, 质检要求 spec);无法解析返回 (None,None)。"""
        cat = item.get("category")
        shot_no = item.get("shot_no")
        name = item.get("name")
        prefix = f"e{ep_num:03d}"
        if cat == "character_art":
            row = self.assets.latest(project_id, "character_art", name)
            spec = self._qc_spec(project_id, [name], forbid=self._FORBID)
        elif cat == "character_sheet":
            asset_name = (name if ":" in str(name)
                          else f"{name}:{item.get('sheet', '')}")
            row = self.assets.latest(
                project_id, "character_sheet", asset_name)
            spec = self._qc_spec(project_id, [str(name).split(":")[0]],
                                 forbid=self._FORBID)
        elif cat == "scene_art":
            row = self.assets.latest(project_id, "scene_art", name)
            spec = self._qc_spec(project_id, [], location=name,
                                 forbid=self._FORBID)
        elif cat == "shot_image":
            row = self.assets.latest(
                project_id, "image", f"{prefix}_shot{shot_no:03d}")
            spec = None   # 分镜人物名单在下面按分镜补
        elif cat == "frames":
            # 首尾帧两张都要检:返回首帧,尾帧在 _qc_one 里另查
            row = self.assets.latest(
                project_id, "first_frame", f"{prefix}_shot{shot_no:03d}")
            spec = None
        else:
            return None, None
        uri = row["uri"] if row and row["uri"] else None
        if uri and not (uri.startswith("http") or Path(uri).exists()):
            uri = None
        return uri, spec

    def _qc_signature(self, uris, spec):
        """图片内容、最终立绘版本和质检规格都没变时复用质检结果。"""
        digest = hashlib.sha256()
        digest.update(json.dumps(spec, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8"))
        for label, uri in uris:
            digest.update(label.encode("utf-8"))
            digest.update(str(uri).encode("utf-8"))
            path = Path(uri)
            if path.exists() and path.is_file():
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
        return digest.hexdigest()

    def qc_item(self, project_title, episode_number, item_id):
        """单张质检:对清单里某条目的最新图做视觉核对,写回 qc 结果。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        plan = self._plan_read(ctx)
        item = next((i for i in plan["items"] if i["id"] == item_id), None)
        if item is None:
            raise AifosError(f"清单里没有该条目: {item_id}")
        category = item.get("category")
        if category in self.INITIAL_ASSET_CATEGORIES:
            raise AifosError(
                "初始人物/场景母资产不做视觉质检；请在分镜关键帧或首尾帧生成后质检")
        if category not in self.SHOT_QC_CATEGORIES:
            raise AifosError("该清单条目不支持镜头视觉质检")
        return self._qc_one(project, episode, ctx, item)

    def _qc_one(self, project, episode, ctx, item):
        project_id = project["id"]
        uri, spec = self._plan_item_asset(
            project_id, episode["number"], item)
        if not uri:
            self._plan_mark(ctx, item["id"], item.get("status", "done"),
                            extra={"qc": {"passed": False, "attempts": 0,
                                          "issues": ["尚无可检的图片"]}})
            return {"passed": False, "issues": ["尚无可检的图片"]}
        if spec is None:
            # 分镜/首尾帧:按分镜取人物名单/场景/动作
            storyboard, _ = self.projects.latest_document(
                episode["id"], "storyboard")
            shot = next((s for s in (storyboard or {}).get("shots", [])
                         if s["shot_no"] == item.get("shot_no")), None)
            if shot is not None:
                ctx["storyboard"] = storyboard
                if "script" not in ctx:
                    script, _ = self.projects.latest_document(
                        episode["id"], "script")
                    ctx["script"] = script or {"scenes": []}
                ctx.setdefault("aspect", project["aspect"] or "9:16")
                ctx.setdefault("dims", ASPECT_DIMS.get(
                    ctx["aspect"], ASPECT_DIMS["9:16"]))
                payload = self._shot_payload(ctx, shot)
                spec = self._qc_spec(
                    project_id, payload.get("characters", []),
                    location=payload.get("location", ""),
                    action=payload.get("action", ""),
                    forbid=self._FORBID + ["字幕条"])
                spec["camera"] = payload.get("camera", "")
            else:
                spec = self._qc_spec(project_id, [], forbid=self._FORBID)
        # 首尾帧:首帧 + 尾帧两张都要检,任一不符即整组不合格
        uris = [("首帧", uri)]
        if item.get("category") == "frames":
            last = self.assets.latest(
                project_id, "last_frame",
                f"e{episode['number']:03d}_shot{item.get('shot_no'):03d}")
            if last and last["uri"] and (last["uri"].startswith("http")
                                         or Path(last["uri"]).exists()):
                uris.append(("尾帧", last["uri"]))
        signature = self._qc_signature(uris, spec)
        previous = item.get("qc") or {}
        if previous.get("signature") == signature \
                and "passed" in previous:
            cached = dict(previous)
            cached["cached"] = True
            return cached
        passed_all, issues, cost = True, [], 0.0
        identity_checked_all = True
        try:
            for label, one in uris:
                result = self.router.call(
                    "image_qc", {**spec, "image_uri": one}, ctx["out_root"],
                    cancel=lambda: self._cancel_requested(ctx))
                cost += result.cost
                verdict = result.data or {}
                identity_checked = (not spec.get("identity_required")
                                    or bool(verdict.get("identity_checked")))
                if not identity_checked:
                    identity_checked_all = False
                    passed_all = False
                    issues.append(f"{label}:质检未确认已逐人比对最终立绘")
                if not bool(verdict.get("pass")):
                    passed_all = False
                    issues.extend(f"{label}:{x}"
                                  for x in (verdict.get("issues") or []))
        except (ProviderUnavailable, ProviderError) as exc:
            raise AifosError(f"质检产线不可用: {exc}") from exc
        report = {"passed": passed_all, "issues": issues,
                  "attempts": previous.get("attempts", 0),
                  "identity_checked": identity_checked_all,
                  "identity_references": len(
                      spec.get("identity_references") or []),
                  "signature": signature, "cached": False}
        self.projects.add_episode_cost(episode["id"], cost)
        self._plan_mark(ctx, item["id"], item.get("status", "done"),
                        extra={"qc": report})
        self.log.info(
            "director",
            f"质检 {item['id']}: "
            + ("通过" if report["passed"]
               else "未过 — " + "；".join(report["issues"])))
        return report

    def qc_all(self, project_title, episode_number):
        """批量质检:对清单里所有已生成的图逐张核对,可暂停。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        plan = self._plan_read(ctx)
        items = [i for i in plan["items"]
                 if i.get("status") in ("done", "reused")
                 and i.get("category") in self.SHOT_QC_CATEGORIES]
        previous_status = episode["status"]
        self.projects.set_episode_status(episode["id"], "cast")
        checked = passed = failed = 0
        try:
            for item in items:
                if self._cancel_requested(ctx):
                    raise ProduceCancelled("已手动暂停质检")
                try:
                    report = self._qc_one(project, episode, ctx, item)
                except AifosError as exc:
                    self.log.warn("director",
                                  f"质检 {item['id']} 跳过: {exc}")
                    continue
                checked += 1
                passed += 1 if report["passed"] else 0
                failed += 0 if report["passed"] else 1
        except ProduceCancelled:
            self.projects.set_episode_status(episode["id"], previous_status)
            return {"status": "paused", "checked": checked,
                    "passed": passed, "failed": failed}
        finally:
            row = self.projects.get_episode(episode["id"])
            if row and row["status"] in ("cast", "cancelling"):
                self.projects.set_episode_status(
                    episode["id"], previous_status)
        self.log.info(
            "director",
            f"批量质检完成:{checked} 张,通过 {passed},未过 {failed}")
        return {"status": "done", "checked": checked,
                "passed": passed, "failed": failed}

    def redo_items(self, project_title, episode_number, item_ids=None,
                   only_failed=False, quality_override=None, progress=None):
        """批量重画:按 item_ids 重画;only_failed=True 时重画所有质检
        未过的图。可暂停,重画后自动复检。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        plan = self._plan_read(ctx)
        by_id = {i["id"]: i for i in plan["items"]}
        if only_failed:
            targets = [i["id"] for i in plan["items"]
                       if (i.get("qc") or {}).get("passed") is False
                       and i.get("category") in self.SHOT_QC_CATEGORIES
                       and i.get("status") in ("done", "reused")]
        else:
            targets = [tid for tid in (item_ids or []) if tid in by_id]
        if not targets:
            if progress:
                progress(phase="done", total=0, completed=0, redone=0,
                         failed=0, note="没有需要重画的图")
            return {"status": "done", "redone": 0,
                    "note": "没有需要重画的图"}
        if only_failed:
            identity_words = ("人物不一致", "人物形象", "身份", "同一个人",
                              "脸", "发型", "发色", "服装")
            systemic = []
            for item_id in targets:
                item = by_id[item_id]
                text = "；".join((item.get("qc") or {}).get("issues") or [])
                if any(word in text for word in identity_words):
                    systemic.append(item_id)
            if len(systemic) >= 3:
                self.log.warn(
                    "director",
                    f"批量重画熔断:{len(systemic)}张出现同类人物身份问题；"
                    "应先修复/重新选择最终立绘，禁止沿用错误锚点逐张重画")
                result = {
                    "status": "blocked", "redone": 0,
                    "reason": "systemic_identity_failure",
                    "affected": len(systemic),
                    "note": "检测到系统性人物身份/发型/服装漂移，请先回人物定版，避免无效批量重画",
                }
                if progress:
                    progress(phase="blocked", total=len(targets),
                             completed=0, redone=0, failed=0,
                             note=result["note"])
                return result
        previous_status = episode["status"]
        self.projects.set_episode_status(episode["id"], "cast")
        self.log.info("director", f"开始批量重画 {len(targets)} 张")
        total = len(targets)
        redone = failed = checked = qc_passed = qc_failed = processed = 0
        if progress:
            progress(phase="queued", total=total, completed=0, redone=0,
                     failed=0, checked=0, qc_passed=0, qc_failed=0,
                     prompt_policy="auto_revision",
                     reference_policy="auto_attach")
        try:
            for index, item_id in enumerate(targets, 1):
                if self._cancel_requested(ctx):
                    raise ProduceCancelled("已手动暂停重画")
                target = self._plan_item_target(item_id)
                item = by_id[item_id]
                label = item.get("label") or item_id
                if target is None:
                    failed += 1
                    processed += 1
                    if progress:
                        progress(phase="redrawing", total=total,
                                 completed=processed, current_index=index,
                                 current_item=item_id, current_label=label,
                                 redone=redone, failed=failed)
                    continue
                issues = list((item.get("qc") or {}).get("issues") or [])
                if issues:
                    feedback = (
                        "批量重画自动修正：必须逐项解决上一版质检问题："
                        + "；".join(issues))[:800]
                    revision_source = "batch_qc"
                else:
                    feedback = (
                        "批量重新画：生成与上一版明显不同的有效新版本；"
                        "严格保持已锁定的人物身份、服装、场景、文字白名单"
                        "和前后镜头连续性")
                    revision_source = "batch_redraw"
                prompt_override = (item.get("prompt", "")
                                   if item.get("custom_prompt") else "")
                if progress:
                    progress(phase="redrawing", total=total,
                             completed=processed, current_index=index,
                             current_item=item_id, current_label=label,
                             redone=redone, failed=failed,
                             prompt_modified=True,
                             revision_note=feedback)
                try:
                    self.regen_image(
                        project_title, episode_number, target,
                        feedback=feedback, prompt_override=prompt_override,
                        quality_override=quality_override,
                        revision_source=revision_source)
                    redone += 1
                except AifosError as exc:
                    failed += 1
                    self.log.warn("director",
                                  f"重画 {item_id} 跳过: {exc}")
                    processed += 1
                    if progress:
                        progress(phase="redrawing", total=total,
                                 completed=processed, current_index=index,
                                 current_item=item_id, current_label=label,
                                 redone=redone, failed=failed,
                                 error=str(exc))
                    continue

                refreshed = next((entry for entry in self._plan_read(ctx)[
                    "items"] if entry["id"] == item_id), item)
                refs = refreshed.get("reference_inputs") or {}
                if progress:
                    progress(phase="checking", total=total,
                             completed=processed, current_index=index,
                             current_item=item_id, current_label=label,
                             redone=redone, failed=failed,
                             references_attached=bool(refs.get("attached")),
                             reference_count=int(refs.get("count") or 0))
                try:
                    report = self._qc_one(
                        project, episode, ctx, refreshed)
                    checked += 1
                    if report.get("passed"):
                        qc_passed += 1
                    else:
                        qc_failed += 1
                except AifosError as exc:
                    self.log.warn(
                        "director", f"重画后复检 {item_id} 跳过: {exc}")
                processed += 1
                if progress:
                    progress(phase="running", total=total,
                             completed=processed, current_index=index,
                             current_item=item_id, current_label=label,
                             redone=redone, failed=failed, checked=checked,
                             qc_passed=qc_passed, qc_failed=qc_failed,
                             references_attached=bool(refs.get("attached")),
                             reference_count=int(refs.get("count") or 0))
        except ProduceCancelled:
            self.projects.set_episode_status(episode["id"], previous_status)
            if progress:
                progress(phase="paused", total=total, completed=processed,
                         redone=redone, failed=failed, checked=checked,
                         qc_passed=qc_passed, qc_failed=qc_failed)
            return {"status": "paused", "redone": redone,
                    "failed": failed, "checked": checked}
        finally:
            row = self.projects.get_episode(episode["id"])
            if row and row["status"] in ("cast", "cancelling"):
                self.projects.set_episode_status(
                    episode["id"], previous_status)
        self.log.info("director", f"批量重画完成:{redone} 张")
        if progress:
            progress(phase="done", total=total, completed=processed,
                     redone=redone, failed=failed, checked=checked,
                     qc_passed=qc_passed, qc_failed=qc_failed,
                     current_item="", current_label="")
        return {"status": "done", "total": total, "redone": redone,
                "failed": failed, "checked": checked,
                "qc_passed": qc_passed, "qc_failed": qc_failed}

    def redo_placeholders(self, project_title, episode_number):
        """一键补真:把清单里落到占位产线的图,逐张用真实产线重画。
        可随时暂停,已补好的保留;真实产线仍不可用时保持占位并红标。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        ctx = {"episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        plan = self._plan_read(ctx)
        pending = [i for i in plan["items"]
                   if i.get("status") == "done" and i.get("real") is False]
        if not pending:
            return {"status": "done", "redone": 0,
                    "note": "清单里没有占位图"}
        previous_status = episode["status"]
        self.projects.set_episode_status(episode["id"], "cast")
        self.log.info(
            "director", f"开始补画 {len(pending)} 张占位图(真实产线)")
        redone = 0
        try:
            for item in pending:
                head, _, rest = item["id"].partition(":")
                builder = self.PLAN_TARGETS.get(head)
                if builder is None:
                    continue
                try:
                    self.regen_image(project_title, episode_number,
                                     builder(rest.split(":")))
                    redone += 1
                except AifosError as exc:
                    self.log.warn("director",
                                  f"补画 {item['id']} 跳过: {exc}")
        except ProduceCancelled:
            self.projects.set_episode_status(
                episode["id"], previous_status)
            return {"status": "paused", "redone": redone}
        finally:
            row = self.projects.get_episode(episode["id"])
            if row and row["status"] in ("cast", "cancelling"):
                self.projects.set_episode_status(
                    episode["id"], previous_status)
        self.log.info("director", f"占位图补画完成:{redone} 张")
        return {"status": "done", "redone": redone}

    # ---- 参考图管理:上传的参考图会自动进入出图提示(关联角色/场景) ----
    def add_reference(self, project_title, name, file_bytes, ext,
                      attach_to="", note=""):
        """上传参考图:attach_to 为空=全项目通用,否则只用于该角色/场景。"""
        ext = ext.lower()
        magic = self.IMAGE_MAGIC.get(ext)
        if magic is None:
            raise AifosError(f"不支持的图片格式: {ext}(png/jpg/webp/svg)")
        if not file_bytes or not file_bytes.lstrip()[:8].startswith(magic) \
                and not file_bytes.startswith(magic):
            raise AifosError("文件内容与图片格式不符")
        project = self.projects.get_project(project_title)
        if project is None:
            raise AifosError(f"项目不存在: {project_title}")
        name = (name or "").strip() or f"参考图{ext}"
        safe = "".join(c if c.isalnum() else "_" for c in name)[:40]
        existing = self.assets.latest(project["id"], "reference", name)
        version = (existing["version"] + 1) if existing else 1
        path = (self.artifacts_root / f"p{project['id']:03d}"
                / "references" / f"{safe}_v{version}{ext}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file_bytes)
        self.assets.register(
            project["id"], "reference", name, uri=str(path),
            meta={"attach_to": attach_to or "", "note": note or ""},
            new_version=existing is not None)
        self.log.info(
            "director", f"已上传参考图「{name}」"
            f"(关联: {attach_to or '全项目'});后续出图将自动参考")
        return {"name": name, "uri": str(path)}

    def delete_reference(self, project_title, name):
        project = self.projects.get_project(project_title)
        if project is None:
            raise AifosError(f"项目不存在: {project_title}")
        row = self.assets.latest(project["id"], "reference", name)
        if row is None or self.assets.is_deleted(row):
            raise AifosError(f"参考图不存在: {name}")
        self.assets.delete(project["id"], "reference", name)
        self.log.info("director", f"已删除参考图「{name}」")
        return {"deleted": name, "history_preserved": True}

    def delete_image_asset(self, project_title, asset_id):
        """资产中心删除已生产图：隐藏当前版本并安全作废下游产物。"""
        project = self.projects.get_project(project_title)
        if project is None:
            raise AifosError(f"项目不存在: {project_title}")
        row = self.assets.get(int(asset_id))
        if row is None or row["project_id"] != project["id"]:
            raise AifosError("图片资产不存在或不属于本项目")
        latest = self.assets.latest(project["id"], row["kind"], row["name"])
        if (latest is None or latest["id"] != row["id"]
                or self.assets.is_deleted(row)):
            raise AifosError("图片资产已删除或不是当前版本")
        if row["kind"] not in IMAGE_ASSET_KINDS - {"reference"}:
            raise AifosError(f"该资产不是可删除图片: {row['kind']}")
        self.assets.soft_delete(
            project["id"], row["kind"], row["name"],
            meta={"deleted_by": "asset_center"})
        invalidated = []

        def invalidate(kind, name):
            current = self.assets.latest(project["id"], kind, name)
            if current is not None and not self.assets.is_deleted(current):
                self.assets.soft_delete(
                    project["id"], kind, name,
                    meta={"invalidated_by_asset_id": row["id"]})
                invalidated.append(kind)

        if row["kind"] == "character_art":
            identity = self.assets.latest(
                project["id"], "character_identity", row["name"])
            if (identity is not None and identity["uri"] == row["uri"]
                    and not self.assets.is_deleted(identity)):
                invalidate("character_identity", row["name"])
        if row["kind"] == "image":
            for kind in ("first_frame", "last_frame", "video"):
                invalidate(kind, row["name"])
        elif row["kind"] in ("first_frame", "last_frame"):
            invalidate("video", row["name"])
        self.log.info(
            "director", f"资产中心已删除图片 {row['kind']}/{row['name']}"
            f"（历史保留；作废下游:{'、'.join(invalidated) or '无'}）")
        return {
            "deleted": {"asset_id": row["id"], "kind": row["kind"],
                        "name": row["name"]},
            "invalidated": invalidated,
            "history_preserved": True,
        }

    def import_video(self, project_title, episode_number, shot_no,
                     file_bytes, ext=".mp4"):
        """上传替换镜头视频(人工剪辑后的成片)。"""
        if ext.lower() != ".mp4":
            raise AifosError("视频仅支持 mp4")
        if b"ftyp" not in file_bytes[:32]:
            raise AifosError("文件内容不是合法 mp4")
        project, episode = self._episode_ctx(project_title, episode_number)
        out_root = self._episode_dir(project, episode)
        asset_name = f"e{episode['number']:03d}_shot{shot_no:03d}"
        path = out_root / "videos" / f"shot_{shot_no:03d}.upload.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file_bytes)
        self.assets.register(project["id"], "video", asset_name,
                             uri=str(path), meta={"uploaded": True},
                             new_version=True)
        self.log.info("director", f"已上传替换镜头{shot_no}视频")
        return {"uri": str(path)}
