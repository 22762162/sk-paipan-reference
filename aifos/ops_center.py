"""AI 运营中心:自动封面、标题、拆条;后续扩展发布与数据分析。"""

import json
from pathlib import Path


class OpsCenter:
    def __init__(self, router):
        self.router = router

    COVER_DIMS = {
        "9:16": {"width": 1080, "height": 1920},
        "16:9": {"width": 1920, "height": 1080},
    }

    def make_cover(self, script, out_dir, aspect="9:16"):
        payload = {
            "title": script["project_title"],
            "episode": script["episode_number"],
            "tagline": script.get("logline", ""),
            "aspect": aspect,
            **self.COVER_DIMS.get(aspect, self.COVER_DIMS["9:16"]),
        }
        return self.router.call("cover", payload, out_dir)

    def make_titles(self, script, kind="drama"):
        """确定性生成 3 个候选标题(后续可路由到大模型优化)。"""
        title = script["project_title"]
        number = script["episode_number"]
        episode_title = script.get("episode_title", "")
        if kind == "idol":
            return [
                f"{title}第{number}期:{episode_title}",
                f"{script.get('logline', '')[:24]}",
                f"{episode_title}|{title}的日常",
            ]
        hero = next(
            (c["name"] for c in script.get("characters", [])
             if c.get("role") == "主角"), "主角")
        villain = next(
            (c["name"] for c in script.get("characters", [])
             if c.get("role") == "反派"), "强敌")
        return [
            f"《{title}》第{number}集:{episode_title}",
            f"{hero}对决{villain},第{number}集高能开场!",
            f"{script.get('logline', '')[:24]}……第{number}集",
        ]

    def make_clips(self, storyboard, out_dir):
        """按场拆条:每场一个切片描述文件,供短视频分发。"""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        clips = []
        scenes = {}
        t = 0.0
        for shot in storyboard["shots"]:
            entry = scenes.setdefault(
                shot["scene_no"], {"start": t, "duration": 0.0, "shots": []})
            entry["duration"] = round(entry["duration"] + shot["duration"], 2)
            entry["shots"].append(shot["shot_no"])
            t = round(t + shot["duration"], 2)
        for scene_no in sorted(scenes):
            entry = scenes[scene_no]
            path = out_dir / f"clip_scene_{scene_no:02d}.json"
            path.write_text(
                json.dumps({
                    "scene_no": scene_no,
                    "start": entry["start"],
                    "duration": entry["duration"],
                    "shots": entry["shots"],
                }, ensure_ascii=False, indent=2),
                encoding="utf-8")
            clips.append({"scene_no": scene_no, "uri": str(path),
                          "duration": entry["duration"]})
        return clips

    # ---- 发布包:一集的分发素材一站式汇总(抖音等平台人工一键上传) ----
    def make_hashtags(self, project, script):
        title = project["title"]
        number = script.get("episode_number", 0)
        if project["kind"] == "idol":
            return [f"#{title}", "#AI虚拟偶像", "#虚拟偶像", "#日常vlog"]
        return [f"#{title}", "#漫剧", "#AI短剧", f"#第{number}集"]

    def make_publish_kit(self, project, episode, ctx, out_dir):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        kit = {
            "account": project["account"] or project["title"],
            "project": project["title"],
            "kind": project["kind"],
            "episode": episode["number"],
            "aspect": ctx.get("aspect", "9:16"),
            "video": ctx.get("final_uri", ""),
            "cover": ctx.get("cover_uri", ""),
            "titles": ctx.get("titles", []),
            "hashtags": self.make_hashtags(project, ctx.get("script", {})),
            "clips": ctx.get("clips", []),
            "duration": ctx.get("edit_data", {}).get("total_duration"),
            "qc_score": ctx.get("qc_report", {}).get("score"),
            "publish_hint": "高峰时段建议:12:00 / 18:00 / 21:00;"
                            "拆条隔 2-4 小时错峰发布",
        }
        path = out_dir / "publish.json"
        path.write_text(
            json.dumps(kit, ensure_ascii=False, indent=2), encoding="utf-8")
        kit["uri"] = str(path)
        return kit
