"""AI 运营中心:自动封面、标题、拆条;后续扩展发布与数据分析。"""

import json
from pathlib import Path


class OpsCenter:
    def __init__(self, router):
        self.router = router

    def make_cover(self, script, out_dir):
        payload = {
            "title": script["project_title"],
            "episode": script["episode_number"],
            "tagline": script.get("logline", ""),
        }
        return self.router.call("cover", payload, out_dir)

    def make_titles(self, script):
        """确定性生成 3 个候选标题(后续可路由到大模型优化)。"""
        title = script["project_title"]
        number = script["episode_number"]
        hero = next(
            (c["name"] for c in script.get("characters", [])
             if c.get("role") == "主角"), "主角")
        villain = next(
            (c["name"] for c in script.get("characters", [])
             if c.get("role") == "反派"), "强敌")
        return [
            f"《{title}》第{number}集:{script.get('episode_title', '')}",
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
