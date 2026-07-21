"""AI 导演中心:总控。拆解任务、调度 Provider、控制流程与成本。

生产流程(SK 漫剧工业流):
  需求 → 剧本 → 连续性圣经 → 五维分镜 → 关键帧/文字锁定 → 首尾帧
       → 生产门禁 → Seedance2 视频(随视频声音/口型) → 剪辑
       → 抽帧检查板 + 内容复核 + 交付脚本 → 包装 → 数据沉淀
"""

import json
from pathlib import Path

from .db import now
from .errors import AifosError, BudgetExceeded, ProduceCancelled
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

STAGES = [
    ("script", "剧本"),
    ("continuity", "连续性圣经"),
    ("cast", "人物/场景图"),
    ("storyboard", "五维分镜"),
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

    # ---- 入口:一句话开工 ----
    def produce(self, project_title, episode_number, premise="", style="",
                force=False, script=None, pause_for_confirm=False,
                kind=None, feedback=""):
        """force=False 时增量生产:已有且落盘完好的产物直接复用,
        只补齐缺失部分——真实产线(即梦按镜头计费)断点续产的关键。
        script:用户自带剧本(标准 JSON);提供时跳过 AI 编剧,
        人物/场次/分镜等全部从该剧本自动推导。
        pause_for_confirm=True:预生产(连续性、五维分镜、关键帧、首尾帧、门禁)
        完成后暂停等待确认(status=awaiting_confirm);确认后再次调用
        produce(不带该参数)即从断点继续自动完成 Seedance 声画、无字幕剪辑
        与三层质检。"""
        if script is not None:
            force = True  # 剧本变了,旧镜头/配音不可复用
        project, created = self.projects.get_or_create_project(
            project_title, style=style,
            kind=kind if kind in ("drama", "idol") else "drama")
        if (not created and kind in ("drama", "idol")
                and project["kind"] != kind):
            # 用户明确改了内容类型 → 更新项目
            project = self.projects.update_project(project_title, kind=kind)
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
            landing = ("awaiting_confirm" if gate_done and gate_done["n"]
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
            "INSERT INTO tasks(episode_id, stage, name, status, created_at, "
            "updated_at) VALUES(?,?,?,?,?,?)",
            (episode_id, stage, stage_cn, "running", ts, ts))
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
            self.log.error("director", f"阶段 {stage} 失败: {exc}")
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
    def _portrait_prompt(self, name, role, style):
        return f"角色立绘:{name}({role}),{style},全身,正面"

    def _scene_prompt(self, location, style):
        return f"场景概念图:{location},{style},空镜,氛围感"

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
                if prev.get("custom_prompt"):
                    item["prompt"] = prev.get("prompt", item["prompt"])
                    item["custom_prompt"] = True
        plan["items"] = rest + items
        self._plan_write(ctx, plan)

    def _plan_mark(self, ctx, item_id, status, error="", prompt=None,
                   only_pending=False):
        plan = self._plan_read(ctx)
        for item in plan["items"]:
            if item["id"] != item_id:
                continue
            if only_pending and item.get("status") not in ("pending",
                                                           "failed"):
                return
            item["status"] = status
            item["error"] = error
            if prompt is not None and prompt != item.get("prompt"):
                item["prompt"] = prompt
                item["custom_prompt"] = True
            self._plan_write(ctx, plan)
            return

    def _plan_run(self, ctx, item_id, fn, prompt=None):
        """包住一次出图调用:生成中 → 完成/失败;手动停止落回排队。"""
        self._plan_mark(ctx, item_id, "generating", prompt=prompt)
        try:
            result = fn()
        except ProduceCancelled:
            self._plan_mark(ctx, item_id, "pending")
            raise
        except Exception as exc:
            self._plan_mark(ctx, item_id, "failed", error=str(exc)[:300])
            raise
        self._plan_mark(ctx, item_id, "done")
        return result

    def _plan_seed_shots(self, ctx):
        """分镜确定后,把每个镜头的关键帧与首尾帧登记进清单。"""
        shots = (ctx.get("storyboard") or {}).get("shots") or []
        self._plan_seed(ctx, "shot_image", [
            {"id": f"shot:{s['shot_no']}", "category": "shot_image",
             "label": f"镜头 {s['shot_no']:02d}"
                      + (f" · 第{s['scene_no']}场" if s.get("scene_no")
                         else ""),
             "shot_no": s["shot_no"], "prompt": s["prompt"]}
            for s in shots])
        self._plan_seed(ctx, "frames", [
            {"id": f"frames:{s['shot_no']}", "category": "frames",
             "label": f"镜头 {s['shot_no']:02d} 首尾帧",
             "shot_no": s["shot_no"],
             "prompt": s.get("seedance_prompt", s["prompt"])}
            for s in shots])

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
                    and existing.get("production_profile", {}).get(
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
                    and existing.get("profile", {}).get(
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
        storyboard = enrich_storyboard(
            ctx["script"], result.data, ctx["continuity"],
            ctx["production_profile"], style=ctx["project"].get("style", ""))
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

    def _stage_cast(self, ctx):
        """人物立绘与场景概念图:项目级资产,跨集复用保证形象一致。"""
        project_id = ctx["project"]["id"]
        style = ctx["project"]["style"] or "国风漫剧"
        characters = ctx["script"].get("characters", [])
        locations = []
        for scene in ctx["script"]["scenes"]:
            if scene["location"] not in locations:
                locations.append(scene["location"])
        self._plan_seed(ctx, "character_art", [
            {"id": f"char:{c['name']}", "category": "character_art",
             "label": f"{c['name']}({c.get('role') or '角色'})",
             "name": c["name"],
             "prompt": self._portrait_prompt(
                 c["name"], c.get("role", ""), style)}
            for c in characters])
        self._plan_seed(ctx, "scene_art", [
            {"id": f"scene:{loc}", "category": "scene_art",
             "label": loc, "name": loc,
             "prompt": self._scene_prompt(loc, style)}
            for loc in locations])
        reused, created = 0, 0
        cast = []
        for character in characters:
            name = character["name"]
            role = character.get("role", "")
            self.assets.acquire(
                project_id, "character", name, meta={"role": role})
            cast.append(name)
            existing = self._existing_asset_uri(ctx, "character_art", name)
            if existing:
                reused += 1
                self._plan_mark(ctx, f"char:{name}", "reused",
                                only_pending=True)
                continue
            result = self._plan_run(ctx, f"char:{name}", lambda: self._call(
                ctx, "image", {
                    "portrait": True, "art_name": name, "role": role,
                    "shot_no": 0, "characters": [name], "location": "",
                    "prompt": self._portrait_prompt(name, role, style),
                    "style": style,
                    "aspect": ctx["aspect"], **ctx["dims"],
                }, "cast"))
            self.assets.register(
                project_id, "character_art", name, uri=result.uri,
                meta={"role": role})
            created += 1
        for scene in ctx["script"]["scenes"]:
            location = scene["location"]
            self.assets.acquire(project_id, "scene", location)
            existing = self._existing_asset_uri(ctx, "scene_art", location)
            if existing:
                reused += 1
                self._plan_mark(ctx, f"scene:{location}", "reused",
                                only_pending=True)
                continue
            result = self._plan_run(
                ctx, f"scene:{location}", lambda: self._call(ctx, "image", {
                    "scene_art": True, "art_name": location,
                    "shot_no": 0, "characters": [], "location": location,
                    "action": scene.get("action", ""),
                    "prompt": self._scene_prompt(location, style),
                    "style": style,
                    "aspect": ctx["aspect"], **ctx["dims"],
                }, "cast"))
            self.assets.register(
                project_id, "scene_art", location, uri=result.uri)
            created += 1
        ctx["cast"] = cast
        return {"reused": reused, "created": created,
                "characters": len(cast),
                "scenes": len(ctx["script"]["scenes"])}

    def _scene_locations(self, ctx):
        return {s["scene_no"]: s["location"]
                for s in ctx["script"]["scenes"]}

    def _art_refs(self, ctx, characters, location):
        """人物立绘/场景概念图路径 → 出图参考(跨镜头角色一致性)。"""
        project_id = ctx["project"]["id"]
        refs = {"character_refs": []}
        for name in characters or []:
            row = self.assets.latest(project_id, "character_art", name)
            if row and row["uri"] and Path(row["uri"]).exists():
                refs["character_refs"].append(row["uri"])
        if location:
            row = self.assets.latest(project_id, "scene_art", location)
            if row and row["uri"] and Path(row["uri"]).exists():
                refs["scene_ref"] = row["uri"]
        return refs

    def _shot_payload(self, ctx, shot):
        locations = self._scene_locations(ctx)
        location = locations.get(shot["scene_no"], "")
        profile = (ctx.get("production_profile")
                   or (ctx.get("storyboard") or {}).get("profile")
                   or production_profile(
                       self.config, ctx.get("production_standard")))
        return {
            "shot_no": shot["shot_no"],
            "unit_id": shot.get("unit_id"),
            "prompt": shot["prompt"],
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
            "readable_text": shot.get("readable_text", {}),
            "performance": shot.get("performance", {}),
            "shot_contract": shot.get("shot_contract", {}),
            "sound_design": shot.get("sound_design", {}),
            "standard_fingerprint": profile.get("standard_fingerprint", ""),
            "forbid_subtitles": not profile["burn_subtitles"],
            "style": ctx["project"]["style"] or "",
            "aspect": ctx["aspect"], **ctx["dims"],
            **self._art_refs(ctx, shot["characters"], location),
        }

    def _stage_images(self, ctx):
        self._plan_seed_shots(ctx)
        ctx["images"] = []
        reused = 0
        for shot in ctx["storyboard"]["shots"]:
            existing = self._existing_asset_uri(
                ctx, "image", self._shot_name(ctx, shot["shot_no"]))
            if existing:
                ctx["images"].append(
                    {"shot_no": shot["shot_no"], "uri": existing})
                reused += 1
                self._plan_mark(ctx, f"shot:{shot['shot_no']}", "reused",
                                only_pending=True)
                continue
            result = self._plan_run(
                ctx, f"shot:{shot['shot_no']}", lambda: self._call(
                    ctx, "image", self._shot_payload(ctx, shot), "images"))
            self._register_shot_asset(ctx, "image", shot["shot_no"],
                                      result.uri)
            ctx["images"].append(
                {"shot_no": shot["shot_no"], "uri": result.uri})
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
        self._plan_seed_shots(ctx)
        images = {i["shot_no"]: i["uri"] for i in ctx["images"]}
        ctx["frames"] = []
        reused = 0
        for shot in ctx["storyboard"]["shots"]:
            name = self._shot_name(ctx, shot["shot_no"])
            first = self._existing_asset_uri(ctx, "first_frame", name)
            last = self._existing_asset_uri(ctx, "last_frame", name)
            if first and last:
                ctx["frames"].append({"shot_no": shot["shot_no"],
                                      "first": first, "last": last})
                reused += 1
                self._plan_mark(ctx, f"frames:{shot['shot_no']}", "reused",
                                only_pending=True)
                continue
            result = self._plan_run(
                ctx, f"frames:{shot['shot_no']}", lambda: self._call(
                    ctx, "frames", {
                        **self._shot_payload(ctx, shot),
                        "image_uri": images[shot["shot_no"]],
                    }, "frames"))
            self._register_shot_asset(
                ctx, "first_frame", shot["shot_no"], result.data["first"])
            self._register_shot_asset(
                ctx, "last_frame", shot["shot_no"], result.data["last"])
            ctx["frames"].append({
                "shot_no": shot["shot_no"],
                "first": result.data["first"],
                "last": result.data["last"],
            })
        return {"count": len(ctx["frames"]), "reused": reused}

    def _stage_preflight(self, ctx):
        """确认前硬门禁：任一项未过都不能消耗 Seedance 额度。"""
        report = build_preflight(
            ctx["script"], ctx["storyboard"], ctx["continuity"],
            ctx["text_assets"], ctx["frames"], ctx["production_profile"])
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
                    "audio_in_video": meta.get("audio_in_video")})
                reused += 1
                continue
            ctx["videos"].append(self._make_video(ctx, shot, frames))
        return {"count": len(ctx["videos"]), "reused": reused}

    def _make_video(self, ctx, shot, frames):
        frame = frames[shot["shot_no"]]
        result = self._call(ctx, "video", {
            "shot_no": shot["shot_no"],
            "unit_id": shot.get("unit_id"),
            "prompt": shot.get("seedance_prompt", shot["prompt"]),
            "duration": shot["duration"],
            "first": frame["first"],
            "last": frame["last"],
            "dialogue": shot.get("dialogue"),
            "voice": ctx["production_profile"]["voice"],
            "lip_sync": ctx["production_profile"]["lip_sync"],
            "forbid_subtitles": not ctx["production_profile"]["burn_subtitles"],
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
                                        "audio_in_video": audio_in_video})
        return {"shot_no": shot["shot_no"], "uri": result.uri,
                "duration": shot["duration"], "provider": result.provider,
                "audio_in_video": audio_in_video}

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
            cover = self.ops.make_cover(
                ctx["script"], ctx["out_root"] / "ops", aspect=ctx["aspect"])
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
    def revise_script(self, project_title, episode_number, feedback):
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
            pause_for_confirm=True, feedback=feedback)

    def regen_image(self, project_title, episode_number, target,
                    feedback="", prompt_override=""):
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
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "out_root": self._episode_dir(project, episode),
            "aspect": aspect,
            "dims": ASPECT_DIMS.get(aspect, ASPECT_DIMS["9:16"]),
            "script": script, "force": True,
        }
        self._task_cost = 0.0
        self._task_providers = set()
        style = project["style"] or "国风漫剧"
        kind = target.get("kind")
        prompt_override = (prompt_override or "").strip()
        if kind == "character_art":
            name = target["name"]
            role = next((c.get("role", "") for c in script["characters"]
                         if c["name"] == name), "")
            prompt = prompt_override or self._portrait_prompt(
                name, role, style)
            result = self._plan_run(ctx, f"char:{name}", lambda: self._call(
                ctx, "image", {
                    "portrait": True, "art_name": name, "role": role,
                    "shot_no": 0, "characters": [name], "location": "",
                    "prompt": prompt,
                    "style": style, "feedback": feedback,
                    "aspect": aspect, **ctx["dims"],
                }, "cast"), prompt=prompt)
            self.assets.register(project["id"], "character_art", name,
                                 uri=result.uri, meta={"role": role},
                                 new_version=True)
        elif kind == "scene_art":
            name = target["name"]
            scene = next((s for s in script["scenes"]
                          if s["location"] == name), {})
            prompt = prompt_override or self._scene_prompt(name, style)
            result = self._plan_run(ctx, f"scene:{name}", lambda: self._call(
                ctx, "image", {
                    "scene_art": True, "art_name": name,
                    "shot_no": 0, "characters": [], "location": name,
                    "action": scene.get("action", ""),
                    "prompt": prompt,
                    "style": style, "feedback": feedback,
                    "aspect": aspect, **ctx["dims"],
                }, "cast"), prompt=prompt)
            self.assets.register(project["id"], "scene_art", name,
                                 uri=result.uri, new_version=True)
        elif kind == "shot":
            shot_no = int(target["shot_no"])
            storyboard, _ = self.projects.latest_document(
                episode["id"], "storyboard")
            shot = next((s for s in storyboard["shots"]
                         if s["shot_no"] == shot_no), None)
            if shot is None:
                raise AifosError(f"镜头不存在: {shot_no}")
            ctx["storyboard"] = storyboard
            payload = self._shot_payload(ctx, shot)
            payload["feedback"] = feedback
            if prompt_override:
                payload["prompt"] = prompt_override
                payload["seedance_prompt"] = prompt_override
            asset_name = self._shot_name(ctx, shot_no)
            result = self._plan_run(
                ctx, f"shot:{shot_no}",
                lambda: self._call(ctx, "image", payload, "images"),
                prompt=payload["prompt"])
            self.assets.register(project["id"], "image", asset_name,
                                 uri=result.uri, new_version=True)
            frames = self._plan_run(
                ctx, f"frames:{shot_no}", lambda: self._call(ctx, "frames", {
                    **payload, "image_uri": result.uri}, "frames"))
            self.assets.register(project["id"], "first_frame", asset_name,
                                 uri=frames.data["first"], new_version=True)
            self.assets.register(project["id"], "last_frame", asset_name,
                                 uri=frames.data["last"], new_version=True)
            # 画面变了 → 旧视频作废,「继续补齐」时重拍并重剪
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
        if kind in ("character_art", "scene_art"):
            name = target["name"]
            latest = self.assets.latest(project["id"], kind, name)
            if latest is None:
                raise AifosError(f"资产不存在: {kind}/{name}")
            version = latest["version"] + 1
            safe = "".join(c if c.isalnum() else "_" for c in name)[:40]
            path = (out_root / "cast"
                    / f"upload_{kind}_{safe}_v{version}{ext}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(file_bytes)
            self.assets.register(project["id"], kind, name,
                                 uri=str(path), meta={"uploaded": True},
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
            self.assets.register(project["id"], "image", asset_name,
                                 uri=str(path), meta={"uploaded": True},
                                 new_version=True)
            # 按新图重做首尾帧(真实产线由 Codex 依据新图推导)
            aspect = (project["aspect"] or self.config.get(
                "defaults", "aspect", default="9:16"))
            ctx = {"project": dict(project), "episode": dict(episode),
                   "out_root": out_root, "aspect": aspect,
                   "dims": ASPECT_DIMS.get(aspect, ASPECT_DIMS["9:16"]),
                   "script": script, "storyboard": storyboard,
                   "force": True}
            self._task_cost = 0.0
            self._task_providers = set()
            payload = self._shot_payload(ctx, shot)
            frames = self._call(ctx, "frames", {
                **payload, "image_uri": str(path)}, "frames")
            self.assets.register(project["id"], "first_frame", asset_name,
                                 uri=frames.data["first"], new_version=True)
            self.assets.register(project["id"], "last_frame", asset_name,
                                 uri=frames.data["last"], new_version=True)
            self.assets.delete(project["id"], "video", asset_name)
            self.log.info(
                "director", f"已上传替换镜头{shot_no}画面,旧视频作废")
            return {"uri": str(path)}
        raise AifosError(f"不支持的上传目标: {kind}")

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
