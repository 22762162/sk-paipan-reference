"""剧本导入:用户自带剧本 → 平台标准剧本 JSON。

支持两种输入:
1. JSON:直接给标准剧本结构(同 AI 编剧输出格式),校验后使用;
2. 纯文本剧本:自动解析场次/角色/台词/动作,例如::

    第1场 古镇长街
    夜色渐深,妖气翻涌。
    林昭:这股妖气不对劲。
    小狐:小心,它就在附近!

    第2场 藏经阁
    ……

解析规则:
- 「第N场 地点」「场景N:地点」「【第N场】地点」开新场;
- 「角色名:台词」(中英文冒号均可,角色名 ≤ 12 字)为台词;
- 其余非空行并入本场动作描述;
- 全文无场次标记时归入单场「主场景」。
角色表自动汇总:台词最多者记为主角,其余为配角。
"""

import json
import re

from .adapters.claude_script import validate_script

_SCENE_RE = re.compile(
    r"^\s*(?:【?第\s*([0-9一二三四五六七八九十百]+)\s*场】?|场景\s*(\d+))"
    r"[::.\s]*(.*)$")
_LINE_RE = re.compile(r"^\s*([^\s::]{1,12})\s*[::]\s*(.+)$")

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10}


class ScriptImportError(ValueError):
    pass


def parse_text_script(text, project_title, episode_number):
    scenes = []
    current = None

    def open_scene(location):
        nonlocal current
        current = {"scene_no": len(scenes) + 1,
                   "location": location or f"场景{len(scenes) + 1}",
                   "characters": [], "action": "", "lines": []}
        scenes.append(current)

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        scene_match = _SCENE_RE.match(line)
        if scene_match:
            open_scene(scene_match.group(3).strip())
            continue
        if current is None:
            open_scene("主场景")
        line_match = _LINE_RE.match(line)
        if line_match:
            current["lines"].append({
                "character": line_match.group(1),
                "dialogue": line_match.group(2).strip(),
            })
        else:
            current["action"] = (
                (current["action"] + " " + line).strip()
                if current["action"] else line)

    scenes = [s for s in scenes if s["lines"] or s["action"]]
    if not any(s["lines"] for s in scenes):
        raise ScriptImportError(
            "未能从文本中解析出任何台词;格式示例:「角色名:台词」,"
            "场次用「第1场 地点」标记")

    counts = {}
    for scene in scenes:
        speakers = []
        for line in scene["lines"]:
            counts[line["character"]] = counts.get(line["character"], 0) + 1
            speakers.append(line["character"])
        scene["characters"] = sorted(set(speakers))
    ordered = sorted(counts, key=lambda n: -counts[n])
    characters = [{"name": name,
                   "role": "主角" if i == 0 else "配角"}
                  for i, name in enumerate(ordered)]

    first_action = next((s["action"] for s in scenes if s["action"]), "")
    script = {
        "project_title": project_title,
        "episode_number": episode_number,
        "episode_title": scenes[0]["location"],
        "logline": first_action or scenes[0]["lines"][0]["dialogue"],
        "characters": characters,
        "scenes": scenes,
    }
    return script


def parse_any(text, project_title, episode_number):
    """JSON 或纯文本剧本 → 标准剧本 JSON(带校验)。"""
    text = (text or "").strip()
    if not text:
        raise ScriptImportError("剧本内容为空")
    if text.startswith("{"):
        try:
            script = json.loads(text)
        except ValueError as exc:
            raise ScriptImportError(f"剧本 JSON 解析失败: {exc}") from exc
        error = validate_script(
            script, {"project_title": project_title,
                     "episode_number": episode_number})
        if error:
            raise ScriptImportError(f"剧本 JSON 校验失败: {error}")
        return script
    return parse_text_script(text, project_title, episode_number)


def load_script_file(path, project_title, episode_number):
    with open(path, encoding="utf-8") as f:
        return parse_any(f.read(), project_title, episode_number)
