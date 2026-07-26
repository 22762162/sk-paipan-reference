"""剧本/小说导入:用户正文 → 平台标准剧本 JSON。

支持三种输入:
1. JSON:直接给标准剧本结构(同 AI 编剧输出格式),校验后使用;
2. 纯文本剧本:自动解析场次/角色/台词/动作,例如::

    第1场 古镇长街
    夜色渐深,妖气翻涌。
    林昭:这股妖气不对劲。
    小狐:小心,它就在附近!

    第2场 藏经阁
    ……
3. 小说正文:识别中文引号对白和前置/后置说话人,例如::

    乾清宫内烛影摇曳。
    朱慈烺咬牙道:“父皇,儿臣请战!”
    “你可知此去凶险?”崇祯沉声问道。

解析规则:
- 「第N场 地点」「场景N:地点」「【第N场】地点」开新场;
- 「角色名:台词」(中英文冒号均可,角色名 ≤ 12 字)为台词;
- 支持“某某说道:‘……’”“‘……’某某问道”和独立引号对白;
- 原文对白逐字保留;未明确说话人的独立对白会结合上下文推断并标记统计;
- 其余非空行并入本场动作描述;
- 全文无场次标记时归入单场「主场景」。
角色表自动汇总:台词最多者记为主角,其余为配角。
"""

import json
import re

from .adapters.claude_script import validate_script
# 转出给既有调用方;唯一事实源在 speaker_labels(零依赖叶子模块)。
from .speaker_labels import is_non_person_label

_SCENE_RE = re.compile(
    r"^\s*(?:【?第\s*([0-9一二三四五六七八九十百]+)\s*场】?|场景\s*(\d+))"
    r"[：:.\s]*(.*)$")
_LINE_RE = re.compile(r"^\s*([^\s：:]{1,12})\s*[：:]\s*(.+)$")
_NAME = r"[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff·]{0,11}"
_MANNER = (
    r"(?:一字一顿地|不耐烦地|压低声音|平静地|严肃地|认真地|疑惑地|"
    r"咬牙|冷笑|沉声|冷声|厉声|轻声|低声|柔声|温声|颤声|怒声|"
    r"高声|朗声|大声|小声|哭着|笑着|皱眉|叹息|喃喃|幽幽|淡淡|"
    r"缓缓|慢慢|忽然|忙|急忙)?")
_SPEECH_VERB = (
    r"(?:开口说道|开口问道|开口道|说道|问道|答道|喊道|叫道|喝道|斥道|"
    r"怒喝|反问|追问|低语|嘀咕|开口|说|问|答|喊|叫|道)")
_PREFIX_COLON_RE = re.compile(
    rf"(?:^|[，。！？；、\s])(?P<speaker>{_NAME})\s*[：:]\s*$")
_QUOTE_PATTERNS = tuple(
    re.compile(re.escape(left) + r"(?P<dialogue>.+?)" + re.escape(right))
    for left, right in (("“", "”"), ("「", "」"), ("『", "』"),
                        ("‘", "’"), ('"', '"'))
)
_INVALID_SPEAKERS = {
    "他", "她", "它", "他们", "她们", "众人", "有人", "对方", "来人",
    "一个人", "那人", "男人", "女人", "少年", "少女", "老人", "声音",
    "那个人", "这个人", "其中一人", "屋里人", "门外人", "问话的人",
}
_PLACEHOLDER_SPEAKER = "待确认说话人"
_PERFORMANCE_PHRASES = tuple(sorted({
    "一字一顿地", "不耐烦地", "压低声音", "小心翼翼地",
    "连忙躬身回话", "躬身垂手", "哑着嗓子", "试探着",
    "有些好奇", "咬牙", "冷笑", "沉声", "冷声", "厉声", "轻声",
    "低声", "柔声", "温声", "颤声", "怒声", "高声", "朗声",
    "大声", "小声", "哭着", "笑着", "皱眉", "叹息", "喃喃",
    "幽幽", "淡淡", "缓缓", "慢慢", "忽然", "急忙", "连忙", "忙",
}, key=len, reverse=True))
_PERFORMANCE_ENDINGS = (
    "地", "着", "嗓子", "回话", "垂手", "好奇", "说道", "问道",
    "答道", "喊道", "叫道", "喝道", "斥道", "低语", "嘀咕",
)

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10}


class ScriptImportError(ValueError):
    pass


def is_likely_performance_label(value):
    """说话方式/动作不是人物实体，绝不能触发独立人物资产。"""
    value = str(value or "").strip(" \t，。！？；、:：")
    if not value:
        return False
    if value in _PERFORMANCE_PHRASES:
        return True
    if any(value.endswith(ending) for ending in _PERFORMANCE_ENDINGS):
        return True
    return any(
        token in value for token in (
            "躬身", "垂手", "回话", "嗓子", "试探着", "小心翼翼",
            "有些好奇", "冷声", "温声", "沉声", "厉声", "低声",
        ))


def _speaker(value, *, direct=False):
    value = str(value or "").strip(" \t，。！？；、:：")
    # 旁白/音效即使写成标准「角色:台词」格式也不是人物,direct 也要拦。
    if (not value or len(value) > 12 or value in _INVALID_SPEAKERS
            or is_non_person_label(value)
            or re.search(r"[，。！？；、:：\"“”‘’「」『』]", value)
            or (not direct and is_likely_performance_label(value))):
        return ""
    return value


def _quote_segments(line):
    """返回一行中成对引号的稳定位置,不把书名号/普通强调当成对白。"""
    found = []
    for pattern in _QUOTE_PATTERNS:
        for match in pattern.finditer(line):
            found.append({
                "start": match.start(),
                "end": match.end(),
                "dialogue": match.group("dialogue"),
            })
    found.sort(key=lambda item: (item["start"], item["end"]))
    result = []
    last_end = -1
    for item in found:
        if item["start"] < last_end:
            continue
        result.append(item)
        last_end = item["end"]
    return result


def _split_speaker_performance(value, known=()):
    value = str(value or "").strip(" \t，。！？；、:：")
    if not value:
        return "", ""
    for name in sorted(set(known or ()), key=len, reverse=True):
        if value == name:
            return name, ""
        if value.startswith(name):
            performance = value[len(name):].strip()
            if performance and is_likely_performance_label(performance):
                return name, performance
    original = value
    removed = []
    changed = True
    while changed and value:
        changed = False
        for phrase in _PERFORMANCE_PHRASES:
            if value.endswith(phrase):
                removed.insert(0, phrase)
                value = value[:-len(phrase)].strip()
                changed = True
                break
    if value and _speaker(value):
        return _speaker(value), "".join(removed)
    if value in _INVALID_SPEAKERS and removed:
        return "", "".join(removed)
    if is_likely_performance_label(original):
        return "", original
    return _speaker(original), ""


def _attribution_part(text, *, prefix):
    """从引号外叙述拆出「人物 + 表演提示 + 说话动词」。"""
    value = str(text or "")
    if prefix:
        value = value.rstrip(" \t，。！？；、:：")
        match = re.search(rf"{_SPEECH_VERB}\s*$", value)
        if not match:
            return "", ""
        body = value[:match.start()]
        body = re.split(r"[，。！？；、\s]", body)[-1]
        return body, match.group(0).strip()
    value = value.lstrip(" \t，。！？；、:：")
    match = re.match(
        rf"(?P<body>[A-Za-z0-9\u4e00-\u9fff·]{{0,24}}?)"
        rf"(?P<verb>{_SPEECH_VERB})(?=$|[，。！？；、:：\s])",
        value)
    if not match:
        return "", ""
    return match.group("body"), match.group("verb")


def _attribution(prefix, suffix, known=()):
    for value, is_prefix in ((prefix, True), (suffix, False)):
        body, _ = _attribution_part(value, prefix=is_prefix)
        if body:
            speaker, performance = _split_speaker_performance(body, known)
            return speaker, performance
    match = _PREFIX_COLON_RE.search(prefix)
    if match:
        return _speaker(match.group("speaker"), direct=True), ""
    return "", ""


def _attributed_speaker(prefix, suffix, known=()):
    return _attribution(prefix, suffix, known)[0]


def _has_speech_attribution(prefix, suffix):
    return bool(
        _PREFIX_COLON_RE.search(prefix)
        or _attribution_part(prefix, prefix=True)[1]
        or _attribution_part(suffix, prefix=False)[1])


def _unwrap_dialogue(value):
    value = str(value or "").strip()
    segments = _quote_segments(value)
    if (len(segments) == 1 and segments[0]["start"] == 0
            and segments[0]["end"] == len(value)):
        return segments[0]["dialogue"]
    return value


def _known_speakers(text):
    names = []

    def add(name):
        name = _speaker(name, direct=True)
        if name and name not in names:
            names.append(name)

    for raw in text.splitlines():
        line = raw.strip()
        segments = _quote_segments(line)
        direct = None if segments else _LINE_RE.match(line)
        if direct:
            add(direct.group(1))
        for segment in segments:
            add(_attributed_speaker(
                line[:segment["start"]], line[segment["end"]:]))
    return names


def _infer_speaker(prefix, suffix, known, scene_lines):
    nearby = [
        name for name in known
        if name and (name in prefix or name in suffix)
    ]
    if nearby:
        return nearby[-1]
    scene_speakers = list(dict.fromkeys(
        item["character"] for item in scene_lines
        if item.get("character") != _PLACEHOLDER_SPEAKER))
    pool = scene_speakers or known
    if len(pool) == 1:
        return pool[0]
    if pool:
        last = scene_lines[-1]["character"] if scene_lines else ""
        return next((name for name in pool if name != last), pool[0])
    return _PLACEHOLDER_SPEAKER


def _append_action(scene, value):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if not value:
        return
    scene["action"] = (
        f"{scene['action']} {value}".strip()
        if scene["action"] else value)


def parse_text_script(text, project_title, episode_number):
    scenes = []
    current = None
    known = _known_speakers(text)
    explicit_dialogue_count = 0
    inferred_dialogue_count = 0
    unresolved_dialogue_count = 0
    quote_dialogue_count = 0

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
        quoted = _quote_segments(line)
        # 带引号的“某某说道:……”优先走小说归属分析，避免把“说道”
        # 错当成人名；无引号的标准「角色:台词」仍走直接解析。
        line_match = None if quoted else _LINE_RE.match(line)
        if line_match:
            speaker = _speaker(line_match.group(1), direct=True)
            if not speaker:
                _append_action(current, line)
                continue
            current["lines"].append({
                "character": speaker,
                "dialogue": _unwrap_dialogue(line_match.group(2)),
            })
            explicit_dialogue_count += 1
            continue

        accepted = []
        for segment in quoted:
            prefix = line[:segment["start"]]
            suffix = line[segment["end"]:]
            speaker, performance = _attribution(prefix, suffix, known)
            is_standalone = not prefix.strip()
            # 没有说话归属、又嵌在叙述中的引号通常是书名/术语,不误判成台词。
            if (not speaker and not is_standalone
                    and not _has_speech_attribution(prefix, suffix)):
                continue
            if speaker:
                explicit_dialogue_count += 1
            else:
                speaker = _infer_speaker(
                    prefix, suffix, known, current["lines"])
                inferred_dialogue_count += 1
                if speaker == _PLACEHOLDER_SPEAKER:
                    unresolved_dialogue_count += 1
            dialogue_line = {
                "character": speaker,
                # 台词必须逐字保留,不做标点/空白重写。
                "dialogue": segment["dialogue"],
            }
            if performance:
                dialogue_line["performance"] = performance
            current["lines"].append(dialogue_line)
            quote_dialogue_count += 1
            accepted.append((segment["start"], segment["end"]))

        if not accepted:
            _append_action(current, line)
            continue
        # 引号内台词进入 lines;引号外叙述/表演信息仍保留为动作。
        action_parts = []
        cursor = 0
        for start, end in accepted:
            action_parts.append(line[cursor:start])
            cursor = end
        action_parts.append(line[cursor:])
        _append_action(current, " ".join(action_parts))

    scenes = [s for s in scenes if s["lines"] or s["action"]]
    if not any(s["lines"] for s in scenes):
        raise ScriptImportError(
            "未识别到对白。已支持「角色名:台词」、中文引号对白、"
            "「某某说道:“……”」和「“……”某某问道」;请粘贴完整小说正文。"
            "如果只有故事梗概,请切换「AI 自动编剧」。")

    counts = {}
    for scene in scenes:
        speakers = []
        for line in scene["lines"]:
            counts[line["character"]] = counts.get(line["character"], 0) + 1
            speakers.append(line["character"])
        scene["characters"] = sorted(
            name for name in set(speakers) if not is_non_person_label(name))
    # 兜底:任何路径漏进来的旁白/音效标签都不得进入正式人物表。
    ordered = [name for name in sorted(counts, key=lambda n: -counts[n])
               if not is_non_person_label(name)]
    characters = [{
        "name": name,
        "role": (
            "待确认说话人" if name == _PLACEHOLDER_SPEAKER
            else "主角" if i == 0 else "配角"),
        **({"asset_policy": "unresolved_no_generation"}
           if name == _PLACEHOLDER_SPEAKER else {}),
    } for i, name in enumerate(ordered)]
    performance_count = sum(
        1 for scene in scenes for line in scene["lines"]
        if line.get("performance"))

    first_action = next((s["action"] for s in scenes if s["action"]), "")
    script = {
        "project_title": project_title,
        "episode_number": episode_number,
        "episode_title": scenes[0]["location"],
        "logline": first_action or scenes[0]["lines"][0]["dialogue"],
        "characters": characters,
        "scenes": scenes,
        "import_analysis": {
            "source_format": (
                "novel" if quote_dialogue_count else "script_text"),
            "dialogue_count": sum(
                len(scene["lines"]) for scene in scenes),
            "explicit_dialogue_count": explicit_dialogue_count,
            "inferred_dialogue_count": inferred_dialogue_count,
            "unresolved_dialogue_count": unresolved_dialogue_count,
            "performance_cue_count": performance_count,
            "character_count": len(characters),
            "scene_count": len(scenes),
            "dialogue_preserved_verbatim": True,
        },
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


def script_to_text(script):
    """标准剧本 JSON → 可读/可再导入的文本格式。"""
    blocks = []
    for scene in script.get("scenes", []):
        lines = [f"第{scene['scene_no']}场 {scene.get('location', '')}"]
        if scene.get("action"):
            lines.append(scene["action"])
        for line in scene.get("lines", []):
            lines.append(f"{line['character']}:{line['dialogue']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def load_script_file(path, project_title, episode_number):
    with open(path, encoding="utf-8") as f:
        return parse_any(f.read(), project_title, episode_number)
