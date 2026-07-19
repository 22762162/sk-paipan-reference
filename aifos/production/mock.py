"""内置 Mock Provider:确定性占位生成。

作用:在未接入 Codex / 即梦 CLI / 剪映 / API 的环境中,让完整生产流程
(剧本→分镜→图片→首尾帧→视频→配音→剪辑→封面)端到端可运行、可测试。
所有产物均为确定性内容(相同输入 → 相同输出),媒体类产物以 JSON/SVG
占位描述文件落盘,由真实 Provider 接入后替换为真实媒体。
"""

import hashlib
import json
from html import escape
from pathlib import Path

from .base import Provider, ProviderResult

SURNAMES = ["林", "苏", "顾", "沈", "陆", "叶", "秦", "白"]
GIVEN = ["昭", "砚", "青", "澈", "离", "澜", "霁", "衡"]
PARTNERS = ["小狐", "阿禾", "墨童", "云雀", "石头"]
VILLAINS = ["蚀骨妖王", "夜枭真君", "赤瞳魔尊", "幽泉老祖"]
LOCATIONS = ["古镇长街", "藏经阁", "迷雾山谷", "断桥残雪", "妖市地穴", "青云峰顶"]
CAMERAS = ["远景推近", "特写", "过肩镜头", "俯拍全景", "手持跟拍", "环绕运镜"]


def _digest(payload):
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).digest()


def _pick(seq, seed, salt):
    return seq[(seed[salt % len(seed)] + salt) % len(seq)]


def _svg(path, lines, seed, width=960, height=540):
    r, g, b = 40 + seed[0] % 120, 40 + seed[1] % 120, 40 + seed[2] % 120
    texts = "".join(
        f'<text x="40" y="{90 + i * 48}" font-size="30" fill="#ffffff">'
        f"{escape(line)}</text>"
        for i, line in enumerate(lines)
    )
    content = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}"><rect width="100%" height="100%" '
        f'fill="rgb({r},{g},{b})"/>{texts}</svg>'
    )
    Path(path).write_text(content, encoding="utf-8")
    return str(path)


def _json_artifact(path, data):
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


class MockProvider(Provider):
    def generate(self, capability, payload, out_dir):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        handler = getattr(self, f"_gen_{capability}", None)
        if handler is None:
            raise ValueError(f"mock 不支持能力: {capability}")
        data, uri = handler(payload, out_dir)
        return ProviderResult(
            provider=self.name, cost=self.cost_per_call, data=data, uri=uri)

    # ---- 剧本 ----
    def _gen_script(self, payload, out_dir):
        seed = _digest(payload)
        title = payload.get("project_title", "未命名")
        number = payload.get("episode_number", 1)
        premise = payload.get("premise", "")
        hero = _pick(SURNAMES, seed, 0) + _pick(GIVEN, seed, 1)
        partner = _pick(PARTNERS, seed, 2)
        villain = _pick(VILLAINS, seed, 3)
        locations = [_pick(LOCATIONS, seed, 4 + i) for i in range(3)]
        logline = (
            f"{hero}与{partner}在{locations[0]}追查《{title}》的线索,"
            f"直面{villain}。" + (f"背景:{premise}" if premise else "")
        )
        scenes = []
        beats = [
            (f"{hero}察觉{locations[0]}妖气异动,循迹而至。",
             [(hero, f"这股妖气……和《{title}》里记载的一模一样。"),
              (partner, f"{hero},小心!它就藏在附近。")]),
            (f"{villain}现身{locations[1]},双方对峙。",
             [(villain, f"区区凡人,也敢窥探《{title}》的秘密?"),
              (hero, f"第{number}页的封印,今天必须取回!"),
              (partner, "我来引开它,你去取封印!")]),
            (f"{hero}在{locations[2]}完成封印,收获新一页图录。",
             [(hero, f"《{title}》第{number}页,收录完成。"),
              (partner, "下一站,我们去哪儿?")]),
        ]
        for idx, (action, lines) in enumerate(beats, start=1):
            scenes.append({
                "scene_no": idx,
                "location": locations[idx - 1],
                "characters": sorted({name for name, _ in lines}),
                "action": action,
                "lines": [
                    {"character": name, "dialogue": text}
                    for name, text in lines
                ],
            })
        script = {
            "project_title": title,
            "episode_number": number,
            "episode_title": f"{villain}之章",
            "logline": logline,
            "characters": [
                {"name": hero, "role": "主角"},
                {"name": partner, "role": "同伴"},
                {"name": villain, "role": "反派"},
            ],
            "scenes": scenes,
        }
        uri = _json_artifact(out_dir / "script.json", script)
        return script, uri

    # ---- 分镜 ----
    def _gen_storyboard(self, payload, out_dir):
        script = payload["script"]
        seed = _digest(payload)
        shots = []
        shot_no = 0
        for scene in script["scenes"]:
            # 每场:1 个环境镜头 + 每句台词 1 个对白镜头
            shot_no += 1
            shots.append({
                "shot_no": shot_no,
                "scene_no": scene["scene_no"],
                "kind": "environment",
                "description": scene["action"],
                "camera": _pick(CAMERAS, seed, shot_no),
                "duration": 2.5,
                "characters": scene["characters"],
                "dialogue": None,
                "prompt": (
                    f"漫剧风格,{scene['location']},{scene['action']}"
                    f",镜头:{_pick(CAMERAS, seed, shot_no)}"
                ),
            })
            for line in scene["lines"]:
                shot_no += 1
                shots.append({
                    "shot_no": shot_no,
                    "scene_no": scene["scene_no"],
                    "kind": "dialogue",
                    "description": f"{line['character']}说话",
                    "camera": _pick(CAMERAS, seed, shot_no),
                    "duration": 3.0,
                    "characters": [line["character"]],
                    "dialogue": line,
                    "prompt": (
                        f"漫剧风格,{scene['location']},{line['character']}"
                        f"正在说:「{line['dialogue']}」"
                        f",镜头:{_pick(CAMERAS, seed, shot_no)}"
                    ),
                })
        storyboard = {"episode_title": script.get("episode_title", ""),
                      "shots": shots}
        uri = _json_artifact(out_dir / "storyboard.json", storyboard)
        return storyboard, uri

    # ---- 镜头关键图 ----
    def _gen_image(self, payload, out_dir):
        seed = _digest(payload)
        shot_no = payload["shot_no"]
        uri = _svg(
            out_dir / f"shot_{shot_no:03d}.keyframe.svg",
            [f"Shot {shot_no:03d}", payload.get("prompt", "")[:36]],
            seed,
        )
        return {"shot_no": shot_no}, uri

    # ---- 首尾帧 ----
    def _gen_frames(self, payload, out_dir):
        seed = _digest(payload)
        shot_no = payload["shot_no"]
        first = _svg(out_dir / f"shot_{shot_no:03d}.first.svg",
                     [f"Shot {shot_no:03d} 首帧"], seed)
        last = _svg(out_dir / f"shot_{shot_no:03d}.last.svg",
                    [f"Shot {shot_no:03d} 尾帧"], seed[::-1])
        return {"first": first, "last": last}, first

    # ---- 视频 ----
    def _gen_video(self, payload, out_dir):
        shot_no = payload["shot_no"]
        duration = float(payload.get("duration", 3.0))
        uri = _json_artifact(out_dir / f"shot_{shot_no:03d}.video.json", {
            "type": "mock-video",
            "shot_no": shot_no,
            "duration": duration,
            "first_frame": payload.get("first", ""),
            "last_frame": payload.get("last", ""),
            "prompt": payload.get("prompt", ""),
        })
        return {"shot_no": shot_no, "duration": duration}, uri

    # ---- 配音 ----
    def _gen_voice(self, payload, out_dir):
        line_no = payload["line_no"]
        text = payload.get("text", "")
        duration = round(max(1.0, len(text) * 0.18), 2)
        uri = _json_artifact(out_dir / f"line_{line_no:03d}.voice.json", {
            "type": "mock-voice",
            "line_no": line_no,
            "character": payload.get("character", ""),
            "text": text,
            "duration": duration,
        })
        return {"line_no": line_no, "duration": duration}, uri

    # ---- 剪辑(剪映草稿 + 成片) ----
    def _gen_edit(self, payload, out_dir):
        shots = payload.get("shots", [])
        voices = payload.get("voices", [])
        subtitles = payload.get("subtitles", [])
        video_track, t = [], 0.0
        for shot in shots:
            video_track.append({
                "material": shot["uri"],
                "start": round(t, 2),
                "duration": shot["duration"],
            })
            t += shot["duration"]
        audio_track = [
            {"material": v["uri"], "line_no": v["line_no"],
             "duration": v["duration"]}
            for v in voices
        ]
        draft = {
            "type": "jianying-draft",
            "tracks": {
                "video": video_track,
                "audio": audio_track,
                "subtitle": subtitles,
            },
            "total_duration": round(t, 2),
        }
        draft_uri = _json_artifact(out_dir / "draft_content.json", draft)
        final_uri = _json_artifact(out_dir / "episode.final.json", {
            "type": "mock-final-video",
            "draft": draft_uri,
            "total_duration": round(t, 2),
            "shot_count": len(video_track),
        })
        return {"draft": draft_uri, "total_duration": round(t, 2)}, final_uri

    # ---- 封面 ----
    def _gen_cover(self, payload, out_dir):
        seed = _digest(payload)
        uri = _svg(
            out_dir / "cover.svg",
            [f"《{payload.get('title', '')}》",
             f"第{payload.get('episode', 0)}集",
             payload.get("tagline", "")[:32]],
            seed, width=810, height=1080,
        )
        return {}, uri
