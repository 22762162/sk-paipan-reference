"""AI 导演中心:总控。拆解任务、调度 Provider、控制流程与成本。

生产流程(总体设计方案·五):
  需求 → 剧本 → 分镜 → 资产调用 → 图片 → 首尾帧 → 视频 → 配音
       → 剪映 → AI质检 → 封面/标题 → 数据沉淀
"""

import json
from pathlib import Path

from .db import now
from .errors import BudgetExceeded

STAGES = [
    ("script", "剧本"),
    ("storyboard", "分镜"),
    ("assets", "资产调用"),
    ("images", "图片"),
    ("frames", "首尾帧"),
    ("videos", "视频"),
    ("voices", "配音"),
    ("edit", "剪映剪辑"),
    ("qc", "AI质检"),
    ("package", "封面/标题/拆条"),
    ("archive", "数据沉淀"),
]


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
                force=False):
        """force=False 时增量生产:已有且落盘完好的产物直接复用,
        只补齐缺失部分——真实产线(即梦按镜头计费)断点续产的关键。"""
        project, _ = self.projects.get_or_create_project(
            project_title, style=style)
        episode, _ = self.projects.get_or_create_episode(
            project["id"], episode_number, premise=premise)
        self.log.info(
            "director",
            f"开始制作《{project_title}》第{episode_number}集 "
            f"(episode_id={episode['id']},force={force})")

        ctx = {
            "project": dict(project),
            "episode": dict(episode),
            "out_root": self._episode_dir(project, episode),
            "force": force,
        }
        stage_reports = []
        failed = False
        for stage, stage_cn in STAGES:
            report = self._run_stage(stage, stage_cn, ctx)
            stage_reports.append(report)
            if report["status"] == "failed":
                failed = True
                break

        episode = self.projects.get_episode(episode["id"])
        if failed:
            self.projects.set_episode_status(episode["id"], "failed")
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
        if not ctx.get("force"):
            existing, version = self.projects.latest_document(
                episode["id"], "script")
            if existing is not None:
                ctx["script"] = existing
                self.log.info("director", f"复用已有剧本 v{version}")
                return {"version": version, "reused": True,
                        "scenes": len(existing["scenes"])}
        result = self._call(ctx, "script", {
            "project_title": ctx["project"]["title"],
            "episode_number": episode["number"],
            "premise": episode["premise"],
            "style": ctx["project"]["style"],
        }, "script")
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
        return {"version": version, "shots": len(storyboard["shots"])}

    def _stage_assets(self, ctx):
        """资产调用:角色/场景优先复用 IP 资产中心已有资产。"""
        project_id = ctx["project"]["id"]
        reused, created = 0, 0
        cast = []
        for character in ctx["script"].get("characters", []):
            row, was_reused = self.assets.acquire(
                project_id, "character", character["name"],
                meta={"role": character.get("role", "")})
            cast.append(character["name"])
            reused += int(was_reused)
            created += int(not was_reused)
        for scene in ctx["script"]["scenes"]:
            _, was_reused = self.assets.acquire(
                project_id, "scene", scene["location"])
            reused += int(was_reused)
            created += int(not was_reused)
        for shot in ctx["storyboard"]["shots"]:
            self.assets.register(
                project_id, "prompt",
                f"e{ctx['episode']['number']:03d}_shot{shot['shot_no']:03d}",
                meta={"prompt": shot["prompt"]})
        ctx["cast"] = cast
        return {"reused": reused, "created": created}

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
            result = self._call(ctx, "image", {
                "shot_no": shot["shot_no"],
                "prompt": shot["prompt"],
                "characters": shot["characters"],
            }, "images")
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
                "shot_no": shot["shot_no"],
                "image_uri": images[shot["shot_no"]],
                "prompt": shot["prompt"],
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
        cover = self.ops.make_cover(ctx["script"], ctx["out_root"] / "ops")
        self._task_cost += cover.cost
        self._task_providers.add(cover.provider)
        self.projects.add_episode_cost(ctx["episode"]["id"], cover.cost)
        ctx["cover_uri"] = cover.uri
        ctx["titles"] = self.ops.make_titles(ctx["script"])
        ctx["clips"] = self.ops.make_clips(
            ctx["storyboard"], ctx["out_root"] / "ops")
        project_id = ctx["project"]["id"]
        ep = f"e{ctx['episode']['number']:03d}"
        self.assets.register(project_id, "cover", ep, uri=cover.uri)
        self.assets.register(
            project_id, "title", ep, meta={"candidates": ctx["titles"]})
        for clip in ctx["clips"]:
            self.assets.register(
                project_id, "clip", f"{ep}_scene{clip['scene_no']:02d}",
                uri=clip["uri"])
        return {"titles": len(ctx["titles"]), "clips": len(ctx["clips"])}

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
