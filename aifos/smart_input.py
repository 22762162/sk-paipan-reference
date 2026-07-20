"""作品名自由识别:一句话不必带《》和「第N集」,自动匹配项目与集数。

  开始制作《万妖图录》第15集   → (万妖图录, 15)   标准格式仍然支持
  万妖图录第16集              → (万妖图录, 16)
  把万妖图录再来一集          → (万妖图录, 下一集)  已有项目子串匹配
  苏念的一天 第3期            → (苏念的一天, 3)
  万妖图录第十五集            → (万妖图录, 15)     中文数字
  苏念的一天                  → (苏念的一天, 下一集/1)
"""

import re

from .errors import AifosError

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# 集数写法:第N集 / 第十五集 / EP7 / 7集(期/话 同义)
_EP_PATTERNS = [
    re.compile(r"第\s*(\d+)\s*[集期话]"),
    re.compile(r"第\s*([零一二两三四五六七八九十]+)\s*[集期话]"),
    re.compile(r"[eE][pP]\.?\s*(\d+)"),
    re.compile(r"(\d+)\s*[集期话]"),
]

# 指令词与语气词:剥掉后剩下的才是作品名
_PREFIXES = ["开始制作", "开始生产", "继续制作", "重新制作", "帮我制作",
             "帮我做", "给我做", "我要做", "我想做", "制作", "生成", "生产",
             "开拍", "再来一集", "来一集", "一集", "做", "把", "将", "拍"]
_SUFFIXES = ["的下一集", "下一集", "的新一集", "新一集", "再来一集",
             "来一集", "一集", "吧", "呗", "啊", "呀", "哦", "喔", "了"]
_TRIM_CHARS = " \t《》「」『』“”\"'‘’,,。.!!??::;;、~~"


def cn_to_int(text):
    """中文数字(一~九十九)→ int;识别失败返回 None。"""
    if not text:
        return None
    if "十" in text:
        tens, _, units = text.partition("十")
        t = 1 if tens == "" else _CN_DIGITS.get(tens)
        u = 0 if units == "" else _CN_DIGITS.get(units)
        if t is None or u is None:
            return None
        return t * 10 + u
    value = 0
    for char in text:
        if char not in _CN_DIGITS:
            return None
        value = value * 10 + _CN_DIGITS[char]
    return value or None


def _extract_episode(text):
    """→ (集数或 None, 去掉集数表述后的文本)。"""
    for pattern in _EP_PATTERNS:
        match = pattern.search(text)
        if match:
            raw = match.group(1)
            number = int(raw) if raw.isdigit() else cn_to_int(raw)
            if number:
                return number, text[:match.start()] + text[match.end():]
    return None, text


def _strip_noise(text):
    """反复剥掉指令词前缀、语气词后缀与引号,直到稳定。"""
    text = text.strip(_TRIM_CHARS)
    changed = True
    while changed and text:
        changed = False
        for prefix in sorted(_PREFIXES, key=len, reverse=True):
            if text.startswith(prefix) and len(text) > len(prefix):
                text = text[len(prefix):].strip(_TRIM_CHARS)
                changed = True
                break
        for suffix in sorted(_SUFFIXES, key=len, reverse=True):
            if text.endswith(suffix) and len(text) > len(suffix):
                text = text[:-len(suffix)].strip(_TRIM_CHARS)
                changed = True
                break
    return text


def parse_free_text(text, known_titles=()):
    """→ (作品名或 None, 集数或 None, 是否命中已有项目)。"""
    text = (text or "").strip()
    if not text:
        return None, None, False
    number, working = _extract_episode(text)
    # 1) 《作品名》优先
    match = re.search(r"《(.+?)》", working)
    if match:
        title = match.group(1).strip()
        return title, number, title in set(known_titles)
    # 2) 已有项目名子串匹配(最长优先),想怎么说都行
    hits = [t for t in known_titles if t and len(t) >= 2 and t in working]
    if hits:
        return max(hits, key=len), number, True
    # 3) 剥掉指令词/语气词,剩下的就是新作品名
    title = _strip_noise(working)
    return (title or None), number, False


def resolve_produce_target(app, text, title=None, number=None):
    """一句话 → (项目名, 集数, 提示语)。

    - 作品名模糊匹配已有项目(全等 → 去空格全等 → 双向子串);
    - 没写集数:已有项目自动接着做下一集,新项目从第 1 集开始。
    """
    titles = [p["title"] for p in app.projects.list_projects()]
    matched = False
    if not title:
        title, parsed_no, matched = parse_free_text(text, titles)
        if number is None:
            number = parsed_no
    if not title:
        raise AifosError(
            "没认出作品名。直接写作品名即可,如「万妖图录 第3集」"
            "或「苏念的一天」(集数可不写,自动接着做下一集)")
    if title not in titles:
        normalized = title.replace(" ", "")
        candidates = [t for t in titles
                      if t.replace(" ", "") == normalized
                      or (len(title) >= 2 and (title in t or t in title))]
        if candidates:
            title = max(candidates, key=len)
            matched = True
    note = ""
    if number is None:
        project = app.projects.get_project(title)
        if project is not None:
            row = app.db.query_one(
                "SELECT MAX(number) AS n FROM episodes WHERE project_id=?",
                (project["id"],))
            number = (row["n"] or 0) + 1
            note = f"没写集数,自动接着做《{title}》第{number}集"
        else:
            number = 1
            note = f"新作品《{title}》,从第1集开始"
    elif matched:
        note = f"已匹配到项目《{title}》第{number}集"
    return title, int(number), note
