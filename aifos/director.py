"""AI 导演中心:总控。拆解任务、调度 Provider、控制流程与成本。

生产流程(总体设计方案·五):
  需求 → 剧本 → 分镜 → 资产调用 → 图片 → 首尾帧 → 视频 → 配音
       → 剪映 → AI质检 → 封面/标题 → 数据沉淀
"""

import json
from pathlib import Path

from .db import now
from .errors import AifosError, BudgetExceeded

# 画幅 → 像素尺寸(视频/图片);封面用竖版比例
ASPECT_DIMS = {
    "9:16": {"width": 1080, "height": 1920},
    "16:9": {"width": 1920, "height": 1080},
}

STAGES = [
    ("script", "剧本"),
    ("cast", "人物/场景图"),
    ("storyboard", "分镜"),
    ("images", "镜头画面"),
    ("frames", "首尾帧"),
    ("videos", "视频"),
    ("voices", "配音"),
    ("edit", "剪映剪辑"),
    ("qc", "AI质检"),
    ("package", "封面/标题/拆条"),
    ("archive", "数据沉淀"),
]

# 预生产检查点:此阶段完成后可暂停等待用户确认,
# 确认后才进入视频生产(真实产线从这里开始消耗即梦额度)
CONFIRM_AFTER = "frames"


class Director:
    def __init__(self, db, config, logger, projects, assets, router, qc, ops,
                 data_center, artifacts_root):
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

    # ---- 入口:一句话开工 ----
    def produce(self, project_title, episode_number, premise="", style="",
                force=False, script=None, pause_for_confirm=False,
                kind=None, feedback=""):
        """force=False 时增量生产:已有且落盘完好的产物直接复用,
        只补齐缺失部分——真实产线(即梦按镜头计费)断点续产的关键。
        script:用户自带剧本(标准 JSON);提供时跳过 AI 编剧,
        人物/场次/分镜等全部从该剧本自动推导。
        pause_for_confirm=True:预生产(剧本/人物图/场景图/分镜/首尾帧)
        完成后暂停等待确认(status=awaiting_confirm);确认后再次调用
        produce(不带该参数)即从断点继续自动完成视频→配音→剪辑→质检。"""
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
        }
        stage_reports = []
        failed = False
        paused = False
        for stage, stage_cn in STAGES:
            report = self._run_stage(stage, stage_cn, ctx)
            stage_reports.append(report)
            if report["status"] == "failed":
                failed = True
                break
            if pause_for_confirm and stage == CONFIRM_AFTER:
                paused = True
                break

        episode = self.projects.get_episode(episode["id"])
        if failed:
            self.projects.set_episode_status(episode["id"], "failed")
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
        except Exception as exc:
            self.db.execute(
                "UPDATE tasks SET status='failed', provider=?, cost=?, "
                "error=?, updated_at=? WHERE id=?",
                (",".join(sorted(self._task_providers)), self._task_cost,
                 str(exc)[:1000], now(), task_id))
            self.log.error("director", f"阶段 {stage} 失败: {exc}")
            return {"stage": stage, "name": stage_cn, "status": "failed",
                    "cost": round(self._task_cost, 2), "error": str(exc)}

    def _call(self, ctx, capability, payload, sub_dir):
        """经由路由器调用 Provider,并做预算与成本记账。"""
        episode = self.projects.get_episode(ctx["episode"]["id"])
        budget = self.config.get("budget", "per_episode", default=0)
        if budget and episode["cost"] >= budget:
            raise BudgetExceeded(
                f"单集成本 {episode['cost']:.2f} 已达预算 {budget},停止调度")
        result = self.router.call(
            capability, payload, ctx["out_root"] / sub_dir)
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
            self.log.info("director", f"使用用户自带剧本(v{version}),"
                          "人物/分镜将自动推导")
            return {"version": version, "provided": True,
                    "scenes": len(provided["scenes"])}
        if not ctx.get("force"):
            existing, version = self.projects.latest_document(
                episode["id"], "script")
            if existing is not None:
                ctx["script"] = existing
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
        self.data.record(
            "prompt", "success", prompt=f"script:{ctx['project']['title']}"
            f":e{episode['number']}", uri=result.uri,
            meta={"version": version}, episode_id=episode["id"])
        return {"version": version, "scenes": len(script["scenes"])}

    def _stage_storyboard(self, ctx):
        if not ctx.get("force"):
            existing, version = self.projects.latest_document(
                ctx["episode"]["id"], "storyboard")
            if existing is not None:
                ctx["storyboard"] = existing
                self.log.info("director", f"复用已有分镜 v{version}")
                return {"version": version, "reused": True,
                        "shots": len(existing["shots"])}
        result = self._call(
            ctx, "storyboard", {"script": ctx["script"]}, "storyboard")
        storyboard = result.data
        version = self.projects.save_document(
            ctx["episode"]["id"], "storyboard", storyboard)
        ctx["storyboard"] = storyboard
        for shot in storyboard["shots"]:
            self.assets.register(
                ctx["project"]["id"], "prompt",
                f"e{ctx['episode']['number']:03d}_shot{shot['shot_no']:03d}",
                meta={"prompt": shot["prompt"]})
        return {"version": version, "shots": len(storyboard["shots"])}

    def _stage_cast(self, ctx):
        """人物立绘与场景概念图:项目级资产,跨集复用保证形象一致。"""
        project_id = ctx["project"]["id"]
        style = ctx["project"]["style"] or "国风漫剧"
        reused, created = 0, 0
        cast = []
        for character in ctx["script"].get("characters", []):
            name = character["name"]
            role = character.get("role", "")
            self.assets.acquire(
                project_id, "character", name, meta={"role": role})
            cast.append(name)
            existing = self._existing_asset_uri(ctx, "character_art", name)
            if existing:
                reused += 1
                continue
            result = self._call(ctx, "image", {
                "portrait": True, "art_name": name, "role": role,
                "shot_no": 0, "characters": [name], "location": "",
                "prompt": f"角色立绘:{name}({role}),{style},全身,正面",
                "style": style,
                "aspect": ctx["aspect"], **ctx["dims"],
            }, "cast")
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
                continue
            result = self._call(ctx, "image", {
                "scene_art": True, "art_name": location,
                "shot_no": 0, "characters": [], "location": location,
                "action": scene.get("action", ""),
                "prompt": f"场景概念图:{location},{style},空镜,氛围感",
                "style": style,
                "aspect": ctx["aspect"], **ctx["dims"],
            }, "cast")
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
        return {
            "shot_no": shot["shot_no"],
            "prompt": shot["prompt"],
            "characters": shot["characters"],
            "location": location,
            "dialogue": shot.get("dialogue"),
            "camera": shot.get("camera", ""),
            "action": shot.get("description", ""),
            "style": ctx["project"]["style"] or "",
            "aspect": ctx["aspect"], **ctx["dims"],
            **self._art_refs(ctx, shot["characters"], location),
        }

    def _stage_images(self, ctx):
        ctx["images"] = []
        reused = 0
        for shot in ctx["storyboard"]["shots"]:
            existing = self._existing_asset_uri(
                ctx, "image", self._shot_name(ctx, shot["shot_no"]))
            if existing:
                ctx["images"].append(
                    {"shot_no": shot["shot_no"], "uri": existing})
                reused += 1
                continue
            result = self._call(
                ctx, "image", self._shot_payload(ctx, shot), "images")
            self._register_shot_asset(ctx, "image", shot["shot_no"],
                                      result.uri)
            ctx["images"].append(
                {"shot_no": shot["shot_no"], "uri": result.uri})
        return {"count": len(ctx["images"]), "reused": reused}

    def _stage_frames(self, ctx):
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
                continue
            result = self._call(ctx, "frames", {
                **self._shot_payload(ctx, shot),
                "image_uri": images[shot["shot_no"]],
            }, "frames")
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

    def _stage_videos(self, ctx):
        frames = {f["shot_no"]: f for f in ctx["frames"]}
        ctx["videos"] = []
        reused = 0
        for shot in ctx["storyboard"]["shots"]:
            existing = self._existing_asset_uri(
                ctx, "video", self._shot_name(ctx, shot["shot_no"]))
            if existing:
                ctx["videos"].append({
                    "shot_no": shot["shot_no"], "uri": existing,
                    "duration": shot["duration"]})
                reused += 1
                continue
            ctx["videos"].append(self._make_video(ctx, shot, frames))
        return {"count": len(ctx["videos"]), "reused": reused}

    def _make_video(self, ctx, shot, frames):
        frame = frames[shot["shot_no"]]
        result = self._call(ctx, "video", {
            "shot_no": shot["shot_no"],
            "prompt": shot["prompt"],
            "duration": shot["duration"],
            "first": frame["first"],
            "last": frame["last"],
            "aspect": ctx["aspect"], **ctx["dims"],
        }, "videos")
        self._register_shot_asset(ctx, "video", shot["shot_no"], result.uri)
        return {"shot_no": shot["shot_no"], "uri": result.uri,
                "duration": shot["duration"]}

    def _stage_voices(self, ctx):
        ctx["voices"] = []
        ctx["subtitles"] = []
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
                ctx["subtitles"].append({
                    "line_no": line_no,
                    "character": line["character"],
                    "text": line["dialogue"],
                })
        return {"count": len(ctx["voices"]), "reused": reused}

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
            "subtitles": ctx["subtitles"],
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
        """质检 + 按评分自动重跑(重生成缺失镜头/配音后重剪、复检)。"""
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
                "issues": len(report["issues"])}

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
        self.data.record(
            "case", label,
            prompt=f"《{ctx['project']['title']}》第{ctx['episode']['number']}集",
            uri=ctx.get("final_uri", ""),
            meta={
                "qc_score": ctx.get("qc_report", {}).get("score"),
                "cost": self.projects.get_episode(episode_id)["cost"],
            },
            episode_id=episode_id)
        return {"label": label}

    def _register_shot_asset(self, ctx, kind, shot_no, uri):
        self.assets.register(
            ctx["project"]["id"], kind,
            f"e{ctx['episode']['number']:03d}_shot{shot_no:03d}", uri=uri)

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
                    feedback=""):
        """重画单张图:target = {"kind": character_art|scene_art|shot,
        "name"|"shot_no"};附意见时新画面按意见调整。
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
        if kind == "character_art":
            name = target["name"]
            role = next((c.get("role", "") for c in script["characters"]
                         if c["name"] == name), "")
            result = self._call(ctx, "image", {
                "portrait": True, "art_name": name, "role": role,
                "shot_no": 0, "characters": [name], "location": "",
                "prompt": f"角色立绘:{name}({role}),{style},全身,正面",
                "style": style, "feedback": feedback,
                "aspect": aspect, **ctx["dims"],
            }, "cast")
            self.assets.register(project["id"], "character_art", name,
                                 uri=result.uri, meta={"role": role},
                                 new_version=True)
        elif kind == "scene_art":
            name = target["name"]
            scene = next((s for s in script["scenes"]
                          if s["location"] == name), {})
            result = self._call(ctx, "image", {
                "scene_art": True, "art_name": name,
                "shot_no": 0, "characters": [], "location": name,
                "action": scene.get("action", ""),
                "prompt": f"场景概念图:{name},{style},空镜,氛围感",
                "style": style, "feedback": feedback,
                "aspect": aspect, **ctx["dims"],
            }, "cast")
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
            asset_name = self._shot_name(ctx, shot_no)
            result = self._call(ctx, "image", payload, "images")
            self.assets.register(project["id"], "image", asset_name,
                                 uri=result.uri, new_version=True)
            frames = self._call(ctx, "frames", {
                **payload, "image_uri": result.uri}, "frames")
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
