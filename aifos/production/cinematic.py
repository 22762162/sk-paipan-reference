"""电影感分镜画面生成(SVG):Mock 产线的正式出图。

按场景关键词自动配色与构图:天空渐变、光源、远近景剪影、雾带、
人物剪影与暗角。镜头画面默认不绘制名牌、台词字幕条或说明文字；
对白只用于表演和口型，避免 Mock 预览给正式产线错误示范。
真实图片产线(Codex/即梦)接入后自动取代;此实现保证"未接产线
也能看到成品级的动态分镜"。
"""

from html import escape

# 场景关键词 → [天空上, 天空下, 光源, 远景, 近景, 强调色]
THEMES = [
    (("夜", "雨", "码头", "地穴", "暗"),
     ["#0b1026", "#1c2f4a", "#7fb4ff", "#0e1a2e", "#060b16", "#9fc7ff"]),
    (("古镇", "长街", "集市", "桥"),
     ["#2b1a2f", "#7a3b2e", "#ffb36b", "#3a2233", "#150d18", "#ffd9a0"]),
    (("山", "谷", "峰", "雾", "林"),
     ["#122430", "#2f5d5a", "#bfe8d9", "#183742", "#0a1a20", "#d8f4ea"]),
    (("直播", "录音", "舞台", "台"),
     ["#1a1030", "#4a1e63", "#c77dff", "#2a1745", "#120a20", "#e9c8ff"]),
    (("阁", "库", "仓", "店"),
     ["#221607", "#59431c", "#ffd27a", "#33270f", "#120c05", "#ffe4a8"]),
]
FALLBACKS = [t[1] for t in THEMES]


def _theme(location, seed):
    for keywords, palette in THEMES:
        if any(k in (location or "") for k in keywords):
            return palette
    return FALLBACKS[seed % len(FALLBACKS)]


def _ridge(seed, width, base_y, amp, step_count=8):
    points = [f"0,{base_y + (seed % amp)}"]
    for i in range(1, step_count + 1):
        x = round(width * i / step_count)
        y = base_y + ((seed >> (i * 3)) % (2 * amp)) - amp
        points.append(f"{x},{y}")
    points.append(f"{width},{base_y}")
    return " ".join(points)


def _character(x, ground_y, scale, name, accent, show_name):
    head_r = round(26 * scale)
    body_w = round(72 * scale)
    body_h = round(120 * scale)
    head_cy = ground_y - body_h - head_r
    parts = [
        f'<ellipse cx="{x}" cy="{head_cy}" rx="{head_r}" ry="{head_r}" '
        f'fill="#05070c" opacity="0.94"/>',
        f'<path d="M {x - body_w // 2} {ground_y} '
        f'Q {x} {ground_y - body_h - 18} {x + body_w // 2} {ground_y} Z" '
        f'fill="#05070c" opacity="0.94"/>',
        f'<ellipse cx="{x - 3}" cy="{head_cy - 3}" rx="{head_r}" '
        f'ry="{head_r}" fill="{accent}" opacity="0.18"/>',
    ]
    if show_name and name:
        label_w = 28 * len(name) + 24
        parts.append(
            f'<rect x="{x - label_w // 2}" y="{head_cy - head_r - 52}" '
            f'width="{label_w}" height="38" rx="19" fill="#000000" '
            f'opacity="0.5"/>'
            f'<text x="{x}" y="{head_cy - head_r - 25}" font-size="26" '
            f'fill="{accent}" text-anchor="middle">{escape(name)}</text>')
    return "".join(parts)


def render_shot(width, height, seed, location, characters=None,
                dialogue=None, shot_no=0, camera="", action="",
                variant="key", annotations=False):
    """variant: key | first | last(首尾帧带轻微位移,连播时产生动感)。"""
    sky_top, sky_bottom, glow, far, near, accent = _theme(location, seed)
    shift = {"key": 0, "first": -1, "last": 1}.get(variant, 0)
    uid = f"g{seed % 99999}{shot_no}{variant}"
    glow_cx = round(width * (0.28 + (seed % 45) / 100)) + shift * width // 25
    glow_cy = round(height * 0.22)
    glow_r = round(min(width, height) * 0.16)
    far_y = round(height * 0.55)
    near_y = round(height * 0.68)
    ground_y = round(height * 0.84)
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">',
        "<defs>",
        f'<linearGradient id="{uid}s" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{sky_top}"/>'
        f'<stop offset="1" stop-color="{sky_bottom}"/></linearGradient>',
        f'<radialGradient id="{uid}v" cx="0.5" cy="0.46" r="0.75">'
        f'<stop offset="0.62" stop-color="#000000" stop-opacity="0"/>'
        f'<stop offset="1" stop-color="#000000" stop-opacity="0.5"/>'
        f'</radialGradient>',
        "</defs>",
        f'<rect width="{width}" height="{height}" fill="url(#{uid}s)"/>',
        f'<circle cx="{glow_cx}" cy="{glow_cy}" r="{glow_r * 2}" '
        f'fill="{glow}" opacity="0.14"/>',
        f'<circle cx="{glow_cx}" cy="{glow_cy}" r="{glow_r}" '
        f'fill="{glow}" opacity="0.55"/>',
        f'<polygon points="{_ridge(seed, width, far_y, height // 14)}" '
        f'fill="{far}"/>',
        f'<rect x="0" y="{far_y}" width="{width}" '
        f'height="{near_y - far_y}" fill="{glow}" opacity="0.05"/>',
        f'<polygon points="{_ridge(seed >> 7, width, near_y, height // 10)}"'
        f' fill="{near}"/>',
        f'<rect x="0" y="{ground_y}" width="{width}" '
        f'height="{height - ground_y}" fill="#04060b"/>',
    ]
    cast = [c for c in (characters or []) if c][:3]
    if cast:
        spread = width // (len(cast) + 1)
        for i, name in enumerate(cast):
            x = spread * (i + 1) + shift * width // 40
            svg.append(_character(
                x, ground_y + 8, min(width, height) / 900,
                name, accent, show_name=annotations))
    if annotations and dialogue:
        bar_h = round(height * 0.105)
        bar_y = height - bar_h - round(height * 0.035)
        text = dialogue.get("dialogue", "")
        max_chars = max(8, width // 34)
        if len(text) > max_chars:
            text = text[:max_chars - 1] + "…"
        svg.append(
            f'<rect x="{round(width * 0.05)}" y="{bar_y}" '
            f'width="{round(width * 0.9)}" height="{bar_h}" rx="16" '
            f'fill="#000000" opacity="0.55"/>'
            f'<text x="{round(width * 0.09)}" y="{bar_y + bar_h // 2 - 8}" '
            f'font-size="{round(bar_h * 0.3)}" fill="{accent}">'
            f'{escape(dialogue.get("character", ""))}</text>'
            f'<text x="{round(width * 0.09)}" '
            f'y="{bar_y + bar_h - round(bar_h * 0.22)}" '
            f'font-size="{round(bar_h * 0.36)}" fill="#ffffff">'
            f'{escape(text)}</text>')
    elif annotations and (location or action):
        base_y = height - round(height * 0.05)
        if action:
            act = action if len(action) <= width // 30 else \
                action[:width // 30 - 1] + "…"
            svg.append(
                f'<text x="{round(width * 0.06)}" y="{base_y}" '
                f'font-size="{round(height * 0.028)}" fill="#ffffff" '
                f'opacity="0.85">{escape(act)}</text>')
        svg.append(
            f'<text x="{round(width * 0.06)}" '
            f'y="{base_y - round(height * 0.045)}" '
            f'font-size="{round(height * 0.036)}" fill="{accent}">'
            f'{escape(location or "")}</text>')
    if annotations and shot_no:
        svg.append(
            f'<text x="{round(width * 0.05)}" y="{round(height * 0.07)}" '
            f'font-size="{round(height * 0.024)}" fill="#ffffff" '
            f'opacity="0.55">#{shot_no:02d}{" · " + escape(camera) if camera else ""}'
            f"</text>")
    svg.append(f'<rect width="{width}" height="{height}" '
               f'fill="url(#{uid}v)"/>')
    svg.append("</svg>")
    return "".join(svg)


def render_portrait(width, height, seed, name, role=""):
    """人物立绘:主题底 + 居中大剪影 + 名牌与角色标签。"""
    sky_top, sky_bottom, glow, far, near, accent = _theme(role + name, seed)
    uid = f"p{seed % 99999}"
    cx = width // 2
    ground_y = round(height * 0.9)
    scale = min(width, height) / 420
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">',
        f'<defs><linearGradient id="{uid}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{sky_top}"/>'
        f'<stop offset="1" stop-color="{sky_bottom}"/></linearGradient></defs>',
        f'<rect width="{width}" height="{height}" fill="url(#{uid})"/>',
        f'<circle cx="{cx}" cy="{round(height * 0.34)}" '
        f'r="{round(width * 0.42)}" fill="{glow}" opacity="0.12"/>',
        _character(cx, ground_y, scale, "", accent, show_name=False),
        f'<text x="{cx}" y="{round(height * 0.13)}" text-anchor="middle" '
        f'font-size="{round(width * 0.12)}" font-weight="bold" '
        f'fill="#ffffff">{escape(name)}</text>',
    ]
    if role:
        chip_w = round(width * 0.09) * len(role) + 40
        svg.append(
            f'<rect x="{cx - chip_w // 2}" y="{round(height * 0.16)}" '
            f'width="{chip_w}" height="{round(height * 0.055)}" '
            f'rx="{round(height * 0.027)}" fill="{accent}" opacity="0.25"/>'
            f'<text x="{cx}" y="{round(height * 0.198)}" '
            f'text-anchor="middle" font-size="{round(width * 0.055)}" '
            f'fill="{accent}">{escape(role)}</text>')
    svg.append("</svg>")
    return "".join(svg)


def render_cover(width, height, seed, title, episode, tagline=""):
    sky_top, sky_bottom, glow, far, near, accent = _theme(tagline, seed)
    base = render_shot(width, height, seed, tagline or title,
                       characters=None, shot_no=0, variant="key")
    base = base[:-len("</svg>")]
    cy = round(height * 0.42)
    tag = tagline if len(tagline) <= width // 40 else \
        tagline[:width // 40 - 1] + "…"
    extra = [
        f'<rect x="{round(width * 0.08)}" y="{cy - round(height * 0.09)}" '
        f'width="{round(width * 0.84)}" height="{round(height * 0.2)}" '
        f'rx="20" fill="#000000" opacity="0.42"/>',
        f'<text x="{width // 2}" y="{cy}" text-anchor="middle" '
        f'font-size="{round(width * 0.11)}" font-weight="bold" '
        f'fill="#ffffff">{escape(title)}</text>',
        f'<text x="{width // 2}" y="{cy + round(height * 0.065)}" '
        f'text-anchor="middle" font-size="{round(width * 0.052)}" '
        f'fill="{accent}">第{episode}集</text>',
        f'<text x="{width // 2}" y="{height - round(height * 0.06)}" '
        f'text-anchor="middle" font-size="{round(width * 0.036)}" '
        f'fill="#ffffff" opacity="0.8">{escape(tag)}</text>',
        "</svg>",
    ]
    return base + "".join(extra)
