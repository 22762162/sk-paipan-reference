"""内置 Mock Provider:确定性占位生成。

作用:在未接入 Codex / 即梦 CLI / 剪映 / API 的环境中,让完整生产流程
(剧本→分镜→图片→首尾帧→视频→配音→剪辑→封面)端到端可运行、可测试。
所有产物均为确定性内容(相同输入 → 相同输出),媒体类产物以 JSON/SVG
占位描述文件落盘,由真实 Provider 接入后替换为真实媒体。
"""

import hashlib
import json
import re
from html import escape
from pathlib import Path

from .base import Provider, ProviderResult
from .cinematic import render_cover, render_portrait, render_shot


def _safe_name(name):
    return "".join(c if c.isalnum() else "_" for c in str(name))[:40]

SURNAMES = ["林", "苏", "顾", "沈", "陆", "叶", "秦", "白"]
GIVEN = ["昭", "砚", "青", "澈", "离", "澜", "霁", "衡"]
PARTNERS = ["小狐", "阿禾", "墨童", "云雀", "石头"]
VILLAINS = ["蚀骨妖王", "夜枭真君", "赤瞳魔尊", "幽泉老祖"]
LOCATIONS = ["古镇长街", "藏经阁", "迷雾山谷", "断桥残雪", "妖市地穴", "青云峰顶"]
CAMERAS = ["远景推近", "特写", "过肩镜头", "俯拍全景", "手持跟拍", "环绕运镜"]

# AI 虚拟偶像模板素材池
IDOL_TOPICS = ["新歌翻唱", "今日穿搭", "粉丝问答", "日常vlog", "舞蹈挑战"]
IDOL_SPOTS = ["直播间", "天台夕阳", "便利店门口", "录音棚", "地铁站台"]
IDOL_MEMBERS = ["林小鹿", "夏栀", "程果", "阿柒", "白桃", "乐乐"]
GROUP_TOPICS = ["首秀舞台", "新歌打歌", "团综日常", "练习室初舞台"]

# 题材关键词 → 模板;开工时未明确类型则按 标题+前提+风格 自动识别
GENRE_KEYWORDS = {
    "idol": ("女团", "男团", "偶像", "爱豆", "打歌", "出道", "组合",
             "主播", "虚拟人"),
    "urban": ("都市", "职场", "公司", "白领", "创业", "办公室"),
    "campus": ("校园", "同学", "高中", "大学", "社团", "毕业"),
    "xianxia": ("仙侠", "修仙", "妖", "灵", "剑", "封印", "宗门"),
}

# 剧情短剧题材素材(三幕:发现 → 对峙 → 解决)
DRAMA_FLAVORS = {
    "xianxia": {
        "locations": LOCATIONS,
        "partners": PARTNERS,
        "villains": VILLAINS,
        "beats": [
            ("{hero}察觉{loc}妖气异动,循迹而至。", [
                ("{hero}", "这股妖气……和《{title}》里记载的一模一样。"),
                ("{partner}", "{hero},小心!它就藏在附近。")]),
            ("{villain}现身{loc},双方对峙。", [
                ("{villain}", "区区凡人,也敢窥探《{title}》的秘密?"),
                ("{hero}", "第{number}页的封印,今天必须取回!"),
                ("{partner}", "我来引开它,你去取封印!")]),
            ("{hero}在{loc}完成封印,收获新一页图录。", [
                ("{hero}", "《{title}》第{number}页,收录完成。"),
                ("{partner}", "下一站,我们去哪儿?")]),
        ],
        "logline": "{hero}与{partner}在{loc}追查《{title}》的线索,直面{villain}。",
    },
    "urban": {
        "locations": ["写字楼大厅", "深夜办公室", "咖啡店", "天台",
                      "会议室", "地铁站"],
        "partners": ["同事阿凯", "实习生小雨", "闺蜜安安", "老同学大树"],
        "villains": ["竞对周总", "甲方王总", "空降李总监"],
        "beats": [
            ("{hero}在{loc}发现项目出了大问题。", [
                ("{hero}", "这份数据不对劲,方案要出事。"),
                ("{partner}", "{hero},现在改还来得及吗?")]),
            ("{villain}在{loc}施压,气氛剑拔弩张。", [
                ("{villain}", "明早交不出方案,这单就归我了。"),
                ("{hero}", "第{number}版方案,今晚一定拿下!"),
                ("{partner}", "我陪你通宵,资料我来整!")]),
            ("{hero}在{loc}漂亮翻盘,赢得掌声。", [
                ("{hero}", "《{title}》第{number}关,通关!"),
                ("{partner}", "走,庆功奶茶我请!")]),
        ],
        "logline": "{hero}和{partner}在{loc}迎战{villain},一夜逆风翻盘。",
    },
    "campus": {
        "locations": ["教室", "操场", "天台", "社团活动室", "图书馆", "小卖部"],
        "partners": ["同桌小七", "学委安然", "篮球队阿豪"],
        "villains": ["隔壁班学霸", "毒舌评委", "神秘转学生"],
        "beats": [
            ("{hero}在{loc}接下了一个大挑战。", [
                ("{hero}", "这次比赛,我想试试。"),
                ("{partner}", "{hero},我们都陪你练!")]),
            ("{villain}在{loc}放话,火药味十足。", [
                ("{villain}", "就凭你们,也想赢?"),
                ("{hero}", "第{number}轮,见真章!"),
                ("{partner}", "别怕,我们练的比谁都多!")]),
            ("{hero}在{loc}稳稳拿下,全场欢呼。", [
                ("{hero}", "《{title}》第{number}战,赢了!"),
                ("{partner}", "说好的,一起去吃火锅!")]),
        ],
        "logline": "{hero}带着{partner}在{loc}迎战{villain},青春全力一搏。",
    },
}


def _detect_genre(payload):
    """开工未明确类型时,按 标题+前提+风格 关键词识别题材。"""
    if payload.get("template") == "idol":
        return "idol"
    text = " ".join(str(payload.get(k) or "")
                    for k in ("project_title", "premise", "style"))
    for genre, keywords in GENRE_KEYWORDS.items():
        if any(k in text for k in keywords):
            return genre
    return "xianxia"


def _persona_from_title(title):
    """「苏念的一天」→「苏念」:标题带『X的…』时取 X 作人设名。"""
    match = re.match(r"^(.{1,4}?)的.+", title or "")
    return match.group(1) if match else (title or "小艾")


def _strip_genre_words(text):
    out = text or ""
    for words in GENRE_KEYWORDS.values():
        for w in words:
            out = out.replace(w, "")
    return out.strip(" ,,、的")


def _dims(payload, default_w=1080, default_h=1920):
    return (int(payload.get("width", default_w)),
            int(payload.get("height", default_h)))


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

    # ---- 剧本:按题材分流(偶像/都市/校园/仙侠) ----
    def _gen_script(self, payload, out_dir):
        genre = _detect_genre(payload)
        if genre == "idol":
            return self._gen_idol_script(payload, out_dir)
        return self._gen_drama_script(payload, out_dir, genre)

    def _gen_drama_script(self, payload, out_dir, genre):
        flavor = DRAMA_FLAVORS[genre]
        seed = _digest(payload)
        title = payload.get("project_title", "未命名")
        number = payload.get("episode_number", 1)
        premise = payload.get("premise", "")
        hero = (_persona_from_title(title)
                if _persona_from_title(title) != title
                else _pick(SURNAMES, seed, 0) + _pick(GIVEN, seed, 1))
        partner = _pick(flavor["partners"], seed, 2)
        villain = _pick(flavor["villains"], seed, 3)
        locations = [_pick(flavor["locations"], seed, 4 + i)
                     for i in range(3)]
        fmt = dict(hero=hero, partner=partner, villain=villain,
                   title=title, number=number)
        logline = (flavor["logline"].format(loc=locations[0], **fmt)
                   + (f"背景:{premise}" if premise else ""))
        scenes = []
        for idx, (action_tpl, line_tpls) in enumerate(
                flavor["beats"], start=1):
            lines = [
                {"character": who.format(**fmt),
                 "dialogue": text.format(**fmt)}
                for who, text in line_tpls
            ]
            scenes.append({
                "scene_no": idx,
                "location": locations[idx - 1],
                "characters": sorted({ln["character"] for ln in lines}),
                "action": action_tpl.format(loc=locations[idx - 1], **fmt),
                "lines": lines,
            })
        script = {
            "project_title": title,
            "episode_number": number,
            "episode_title": f"{villain}之章" if genre == "xianxia"
            else f"对决{villain}",
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

    def _gen_idol_script(self, payload, out_dir):
        """虚拟偶像:单人口播或女团/男团多成员,开场钩子 → 内容 → 引导关注。"""
        seed = _digest(payload)
        text = " ".join(str(payload.get(k) or "")
                        for k in ("project_title", "premise", "style"))
        group = any(k in text for k in ("女团", "男团", "组合", "团"))
        raw = payload.get("persona") or payload.get("project_title", "小艾")
        idol = _persona_from_title(raw)
        number = payload.get("episode_number", 1)
        premise = payload.get("premise", "")
        topic = (_strip_genre_words(premise)
                 or (_pick(GROUP_TOPICS, seed, 0) if group
                     else _pick(IDOL_TOPICS, seed, 0)))
        if group:
            return self._gen_idol_group_script(
                payload, out_dir, idol, number, topic, seed)
        spot = _pick(IDOL_SPOTS, seed, 1)
        scenes = [
            {"scene_no": 1, "location": "直播间",
             "characters": [idol],
             "action": f"{idol}对镜头挥手开场",
             "lines": [
                 {"character": idol,
                  "dialogue": f"哈喽大家好,我是{idol}!今天聊聊{topic}~"},
             ]},
            {"scene_no": 2, "location": spot,
             "characters": [idol],
             "action": f"{idol}在{spot}展示{topic}",
             "lines": [
                 {"character": idol,
                  "dialogue": f"这次{topic}我准备了一个小惊喜。"},
                 {"character": idol,
                  "dialogue": "三、二、一,一起来看!"},
             ]},
            {"scene_no": 3, "location": "直播间",
             "characters": [idol],
             "action": f"{idol}比心收尾",
             "lines": [
                 {"character": idol,
                  "dialogue": f"喜欢的话点个关注,第{number}期我们下期见!"},
             ]},
        ]
        script = {
            "project_title": payload.get("project_title", idol),
            "episode_number": number,
            "episode_title": f"{topic}篇",
            "logline": f"{idol}的第{number}期:{topic}。",
            "characters": [{"name": idol, "role": "主角"}],
            "scenes": scenes,
        }
        uri = _json_artifact(out_dir / "script.json", script)
        return script, uri

    def _gen_idol_group_script(self, payload, out_dir, lead, number,
                               topic, seed):
        """女团/男团:队长带队,练习室 → 舞台 → 后台三幕。"""
        pool = [m for m in IDOL_MEMBERS if m != lead]
        m2 = _pick(pool, seed, 1)
        m3 = _pick([m for m in pool if m != m2], seed, 2)
        members = [lead, m2, m3]
        roles = ["队长", "主唱", "舞担"]
        m1 = lead
        scenes = [
            {"scene_no": 1, "location": "练习室",
             "characters": members,
             "action": f"清晨的练习室,{m1}带队热身,镜面墙映出三人身影",
             "lines": [
                 {"character": m1,
                  "dialogue": f"今天{topic},大家状态怎么样?"},
                 {"character": m2, "dialogue": "早就准备好了,走一遍!"},
                 {"character": m3, "dialogue": "音乐起,五、六、七、八!"},
             ]},
            {"scene_no": 2, "location": "舞台",
             "characters": members,
             "action": f"灯光亮起,{topic}正式开始,三人走位干净利落",
             "lines": [
                 {"character": m1, "dialogue": f"这一段副歌,交给{m2}!"},
                 {"character": m2, "dialogue": "看我的,高音稳住!"},
                 {"character": m3, "dialogue": "队形变换,跟上节奏!"},
             ]},
            {"scene_no": 3, "location": "后台",
             "characters": members,
             "action": "演出结束,三人击掌相拥,汗水与笑容",
             "lines": [
                 {"character": m1,
                  "dialogue": f"第{number}期{topic},圆满完成!"},
                 {"character": m2,
                  "dialogue": "喜欢我们就点关注,别错过下一期!"},
             ]},
        ]
        script = {
            "project_title": payload.get("project_title", lead),
            "episode_number": number,
            "episode_title": f"{topic}篇",
            "logline": f"{m1}、{m2}、{m3}的第{number}期:{topic}。",
            "characters": [
                {"name": name, "role": role}
                for name, role in zip(members, roles)
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

    # ---- 镜头关键图(电影感分镜画面) ----
    def _shot_svg(self, payload, variant):
        seed = int.from_bytes(_digest(payload)[:4], "big")
        width, height = _dims(payload)
        return render_shot(
            width, height, seed,
            payload.get("location", "")
            or self._location_from_prompt(payload.get("prompt", "")),
            characters=payload.get("characters", []),
            dialogue=payload.get("dialogue"),
            shot_no=payload.get("shot_no", 0),
            camera=payload.get("camera", ""),
            action=payload.get("action", ""),
            variant=variant,
        )

    @staticmethod
    def _location_from_prompt(prompt):
        # 分镜 Prompt 形如「漫剧风格,<地点>,<内容>…」,取地点段
        parts = (prompt or "").split(",")
        return parts[1] if len(parts) > 1 else ""

    def _gen_image(self, payload, out_dir):
        seed = int.from_bytes(_digest(payload)[:4], "big")
        width, height = _dims(payload)
        if payload.get("portrait"):
            name = payload.get("art_name", "")
            path = out_dir / f"portrait_{_safe_name(name)}.svg"
            path.write_text(
                render_portrait(width, height, seed, name,
                                payload.get("role", "")),
                encoding="utf-8")
            return {"name": name}, str(path)
        if payload.get("scene_art"):
            name = payload.get("art_name", "")
            path = out_dir / f"scene_{_safe_name(name)}.svg"
            path.write_text(
                render_shot(width, height, seed, name,
                            action=payload.get("action", ""),
                            variant="key"),
                encoding="utf-8")
            return {"name": name}, str(path)
        shot_no = payload["shot_no"]
        path = out_dir / f"shot_{shot_no:03d}.keyframe.svg"
        path.write_text(self._shot_svg(payload, "key"), encoding="utf-8")
        return {"shot_no": shot_no}, str(path)

    # ---- 首尾帧(带轻微位移,连播产生动感) ----
    def _gen_frames(self, payload, out_dir):
        shot_no = payload["shot_no"]
        first = out_dir / f"shot_{shot_no:03d}.first.svg"
        last = out_dir / f"shot_{shot_no:03d}.last.svg"
        first.write_text(self._shot_svg(payload, "first"), encoding="utf-8")
        last.write_text(self._shot_svg(payload, "last"), encoding="utf-8")
        return {"first": str(first), "last": str(last)}, str(first)

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
        seed = int.from_bytes(_digest(payload)[:4], "big")
        width, height = _dims(payload, 1080, 1920)
        path = out_dir / "cover.svg"
        path.write_text(
            render_cover(width, height, seed,
                         payload.get("title", ""),
                         payload.get("episode", 0),
                         payload.get("tagline", "")),
            encoding="utf-8")
        return {}, str(path)
