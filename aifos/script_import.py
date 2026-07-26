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

_SCENE_RE = re.compile(
    r"^\s*(?:【?第\s*([0-9一二三四五六七八九十百]+)\s*场】?|场景\s*(\d+))"
    r"[：:.\s]*(.*)$")
_SHOT_SCENE_RE = re.compile(
    r"^\s*(?:[-+]\s*)?(?:#+\s*)?【?镜头\s*(?P<number>\d+)】?"
    r"\s*(?:[（(]\s*(?P<duration>\d+(?:\.\d+)?)\s*秒\s*[）)])?\s*$")
_LINE_RE = re.compile(r"^\s*([^\s：:]{1,12})\s*[：:]\s*(.+)$")
_NAME = r"[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff·]{0,11}"
_MANNER = (
    r"(?:一字一顿地|不耐烦地|压低声音|平静地|严肃地|认真地|疑惑地|"
    r"咬牙|冷笑|沉声|冷声|厉声|轻声|低声|柔声|温声|颤声|怒声|"
    r"高声|朗声|大声|小声|哭着|笑着|皱眉|叹息|喃喃|幽幽|淡淡|"
    r"缓缓|慢慢|忽然|忙|急忙)?")
_SPEECH_VERB = (
    r"(?:开口说道|开口问道|开口道|说道|问道|答道|喊道|叫道|喝道|斥道|"
    r"怒喝|怒骂|反问|追问|低语|嘀咕|开口|说|问|答|喊|叫|道)")
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
_MARKDOWN_DIRECTIVE_RE = re.compile(
    r"^\s*(?:[-+]\s*)?\*{1,2}\s*"
    r"(?P<label>[^*：:\n]{1,20})\s*[：:]\s*"
    r"\*{0,2}\s*(?P<value>.*?)\s*\*{0,2}\s*$")
_CONTROL_HEADING_RE = re.compile(
    r"^\s*(?P<label>[^：:\n]{1,20})\s*[：:]\s*$")
_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?(?P<value>.*)$")
_NARRATOR_NAME = "旁白（画外声）"

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10}


class ScriptImportError(ValueError):
    pass


def normalize_entity_label(value):
    """Remove Markdown decoration without changing a real character name."""
    value = str(value or "").strip()
    value = re.sub(r"^\s*(?:[-+]\s*)?", "", value)
    value = value.strip(" \t*_`#")
    return value.strip(" \t，。！？；、:：")


def non_person_label_kind(value):
    """Classify screenplay control labels that must never become cast."""
    value = normalize_entity_label(value)
    if not value:
        return ""
    if value.startswith(("（非人物·", "(非人物·")):
        if any(token in value for token in ("音效", "声音", "拟音")):
            return "sound"
        if any(token in value for token in ("字幕", "屏幕文字", "画面文字")):
            return "screen_text"
        return "metadata"
    if value in {
            "旁白", "旁白（画外声）", "画外音", "解说", "内心旁白"}:
        return "narrator"
    if value.upper() in {"SFX", "BGM"} or value in {
            "音效", "拟音", "环境音", "声音", "音乐"}:
        return "sound"
    if value in {
            "字幕", "片尾字幕", "屏幕文字", "画面文字", "标题",
            "大字", "文字"}:
        return "screen_text"
    if value in {
            "优势", "缺点", "劣势", "总结", "总时长", "亮点", "钩子",
            "解析噪声", "说明", "备注", "节奏", "人物少", "场景少"}:
        return "metadata"
    return ""


def _clean_markdown_value(value):
    value = str(value or "").strip()
    quote = _BLOCKQUOTE_RE.match(value)
    if quote:
        value = quote.group("value").strip()
    value = value.strip(" \t*_`")
    return _unwrap_dialogue(value)


def _looks_like_sound_cue(value):
    value = _clean_markdown_value(value)
    return bool(re.fullmatch(
        r"(?:砰|嘭|轰|咚|啪|咔|嗖|唰|嗡|叮|哐|咣|吱|铛){1,4}"
        r"[！!…~～]*",
        value))


def sanitize_script_entities(script):
    """Move sound/text/Markdown controls out of the formal character table.

    The operation is deterministic and idempotent. Narration remains a
    background voice when it is not attributed to a named character; sound,
    screen text and editorial metadata become scene cues rather than people.
    """
    if not isinstance(script, dict):
        return script
    existing_characters = [
        item for item in script.get("characters", [])
        if isinstance(item, dict) and item.get("name")
    ]
    profiles = {}
    for item in existing_characters:
        name = normalize_entity_label(item.get("name"))
        kind = non_person_label_kind(name)
        if kind in {"sound", "screen_text", "metadata"}:
            continue
        if kind == "narrator":
            name = _NARRATOR_NAME
            item = dict(item)
            item["role"] = "背景人物"
            item["asset_policy"] = "scene_only_no_individual_asset"
        item = dict(item)
        item["name"] = name
        profiles.setdefault(name, item)

    removed = 0
    for scene in script.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        declared = []
        for raw_name in scene.get("characters", []) or []:
            name = normalize_entity_label(raw_name)
            kind = non_person_label_kind(name)
            if kind in {"sound", "screen_text", "metadata"}:
                removed += 1
                continue
            if kind == "narrator":
                name = _NARRATOR_NAME
            if name and name not in declared:
                declared.append(name)

        clean_lines = []
        for line in scene.get("lines", []) or []:
            if not isinstance(line, dict):
                continue
            line = dict(line)
            name = normalize_entity_label(line.get("character"))
            kind = non_person_label_kind(name)
            text = _clean_markdown_value(line.get("dialogue"))
            if kind in {"sound", "screen_text", "metadata"}:
                removed += 1
                if text and text != "**":
                    field = {
                        "sound": "sound_cues",
                        "screen_text": "screen_text_cues",
                        "metadata": "discarded_import_cues",
                    }[kind]
                    cue = {"text": text}
                    source = normalize_entity_label(
                        line.get("source_character_label") or name)
                    if source:
                        cue["source_label"] = source
                    scene.setdefault(field, []).append(cue)
                continue
            if kind == "narrator":
                name = _NARRATOR_NAME
            if not name or not text or text == "**":
                removed += 1
                continue
            line["character"] = name
            line["dialogue"] = text
            clean_lines.append(line)
            if name not in declared:
                declared.append(name)
        scene["lines"] = clean_lines
        scene["characters"] = declared
        for name in declared:
            if name not in profiles:
                profiles[name] = {
                    "name": name,
                    "role": (
                        "背景人物" if name == _NARRATOR_NAME else "配角"),
                    **({"asset_policy": "scene_only_no_individual_asset"}
                       if name == _NARRATOR_NAME else {}),
                }

    # Preserve the authored character-table order. Scene character lists are
    # often produced through set/sort operations and must not silently change
    # the protagonist or first-appearance order.
    ordered = list(profiles)
    for scene in script.get("scenes", []):
        for name in scene.get("characters", []) or []:
            if name in profiles and name not in ordered:
                ordered.append(name)
    real_names = [
        name for name in ordered
        if profiles[name].get("role") != "背景人物"
        and profiles[name].get("asset_policy")
        != "scene_only_no_individual_asset"
    ]
    generic_roles = {"", "角色", "配角", "次要角色", "待定"}
    if (real_names and not any(
            "主角" in str(profiles[name].get("role") or "")
            for name in real_names)
            and all(
                str(profiles[name].get("role") or "") in generic_roles
                for name in real_names)):
        profiles[real_names[0]]["role"] = "主角"
    script["characters"] = [profiles[name] for name in ordered]
    script["declared_character_names"] = ordered
    imported = script.setdefault("import_analysis", {})
    imported["character_count"] = len(ordered)
    if removed:
        imported["non_person_cues_removed"] = max(
            int(imported.get("non_person_cues_removed") or 0), removed)
    return script


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
    if (not value or len(value) > 12 or value in _INVALID_SPEAKERS
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
    pending_directive = ""
    pending_blockquote_speaker = ""
    has_shot_headings = bool(re.search(
        r"(?m)^\s*(?:[-+]\s*)?(?:#+\s*)?【?镜头\s*\d+】?",
        text))
    seen_shot_heading = False

    def open_scene(location):
        nonlocal current
        current = {"scene_no": len(scenes) + 1,
                   "location": location or f"场景{len(scenes) + 1}",
                   "characters": [], "action": "", "lines": []}
        scenes.append(current)

    def append_directive(label, value):
        kind = non_person_label_kind(label)
        value = _clean_markdown_value(value)
        if kind == "narrator":
            if value:
                current["lines"].append({
                    "character": _NARRATOR_NAME,
                    "dialogue": value,
                    "source_character_label": normalize_entity_label(label),
                })
            return True
        if kind in {"sound", "screen_text", "metadata"}:
            if value:
                field = {
                    "sound": "sound_cues",
                    "screen_text": "screen_text_cues",
                    "metadata": "discarded_import_cues",
                }[kind]
                current.setdefault(field, []).append({
                    "text": value,
                    "source_label": normalize_entity_label(label),
                })
            return True
        return False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if (seen_shot_heading
                and re.match(r"^\s*#{1,6}\s*总时长", line)):
            break
        shot_match = _SHOT_SCENE_RE.match(line)
        if shot_match:
            seen_shot_heading = True
            open_scene(f"镜头{shot_match.group('number')}")
            if shot_match.group("duration"):
                current["duration"] = float(shot_match.group("duration"))
            pending_directive = ""
            pending_blockquote_speaker = ""
            continue
        scene_match = _SCENE_RE.match(line)
        if scene_match:
            open_scene(scene_match.group(3).strip())
            continue
        if has_shot_headings and not seen_shot_heading:
            # Introductory advice and Markdown titles before the first shot
            # are source-document metadata, not shootable action.
            continue
        if line == "---":
            continue
        if current is None:
            open_scene("主场景")
        directive = _MARKDOWN_DIRECTIVE_RE.match(line)
        if directive:
            label = normalize_entity_label(directive.group("label"))
            value = directive.group("value")
            if non_person_label_kind(label):
                if value:
                    append_directive(label, value)
                else:
                    pending_directive = label
                continue
        control_heading = _CONTROL_HEADING_RE.match(
            line.strip(" \t*_`"))
        if control_heading:
            label = normalize_entity_label(control_heading.group("label"))
            if non_person_label_kind(label):
                pending_directive = label
                continue
        blockquote = _BLOCKQUOTE_RE.match(line)
        if pending_directive:
            if blockquote:
                append_directive(
                    pending_directive, blockquote.group("value"))
                pending_directive = ""
                continue
            pending_directive = ""
        elif blockquote:
            if pending_blockquote_speaker:
                current["lines"].append({
                    "character": pending_blockquote_speaker,
                    "dialogue": _clean_markdown_value(
                        blockquote.group("value")),
                })
                pending_blockquote_speaker = ""
                explicit_dialogue_count += 1
                quote_dialogue_count += 1
                continue
            if _looks_like_sound_cue(blockquote.group("value")):
                append_directive("音效", blockquote.group("value"))
                continue
            # Markdown screenplay blockquotes without an explicit label are
            # narration/voiceover, not action prose and never a visual actor.
            append_directive("旁白", blockquote.group("value"))
            continue
        elif pending_blockquote_speaker:
            pending_blockquote_speaker = ""
        speech_heading = line.strip(" \t*_`")
        if speech_heading.endswith(("：", ":")):
            body, verb = _attribution_part(
                speech_heading, prefix=True)
            speaker, _ = _split_speaker_performance(body, known)
            if verb and speaker:
                pending_blockquote_speaker = speaker
                _append_action(
                    current, speech_heading.rstrip("：:"))
                continue
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
        scene["characters"] = sorted(set(speakers))
    ordered = sorted(counts, key=lambda n: -counts[n])
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
    return sanitize_script_entities(script)


def parse_any(text, project_title, episode_number):
    """JSON 或纯文本剧本 → 标准剧本 JSON(带校验)。"""
    text = (text or "").strip()
    if not text:
        raise ScriptImportError("剧本内容为空")
    if text.startswith("{"):
        # Lazy import avoids a cycle: Claude adapters also reuse the
        # deterministic non-person sanitizer from this module.
        from .adapters.claude_script import validate_script
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
