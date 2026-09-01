"""Production gates for explicit character identity facts.

Reference images may lock a face after the story facts are known, but they are
not a substitute for declaring the character's gender presentation and visible
age range before portrait generation.
"""

from __future__ import annotations

import re


AMBIGUOUS_IDENTITY_TOKENS = (
    "未指定", "未知", "不详", "未明示", "待定", "待确认", "待补充",
    "待人工", "需人工", "参考图再定", "由参考图决定",
    "以参考图为准", "以剧本为准", "按参考图", "按剧本",
    "自行判断", "自行推断", "模型判断", "自由发挥",
)

GENDER_TOKENS = (
    "男性", "女性", "男", "女", "非二元", "无性别", "不适用",
)

AGE_TOKENS = (
    "婴儿", "幼儿", "儿童", "孩童", "少年", "少女", "青少年",
    "未成年", "青年", "成年", "中年", "老年", "老人",
)

_AGE_WITH_UNIT = re.compile(
    r"(?:\d{1,3}|[零〇一二两三四五六七八九十百]{1,5})"
    r"\s*(?:至|到|[-—~～])?\s*"
    r"(?:\d{1,3}|[零〇一二两三四五六七八九十百]{0,5})\s*岁"
)

# age_range 字段里的裸数字/数字区间(如「25-30」「17」)语义无歧义,
# 不能因为编剧没写「岁」就打回人工——实测门禁曾因此熔断整条 cast 产线。
_AGE_BARE_NUMERIC = re.compile(
    r"^\d{1,3}\s*(?:(?:至|到|[-—~～])\s*\d{1,3})?$"
)


def _clean(value):
    return str(value or "").strip()


def is_ambiguous_identity_value(value):
    text = _clean(value)
    return not text or any(token in text for token in AMBIGUOUS_IDENTITY_TOKENS)


def explicit_gender(value):
    """Return whether a drawable gender presentation is explicitly declared."""
    text = _clean(value)
    return (
        not is_ambiguous_identity_value(text)
        and any(token in text for token in GENDER_TOKENS)
    )


def explicit_age_range(value):
    """Return whether a visible age or age band is explicitly declared."""
    text = _clean(value)
    return (
        not is_ambiguous_identity_value(text)
        and (
            bool(_AGE_WITH_UNIT.search(text))
            or bool(_AGE_BARE_NUMERIC.match(text))
            or any(token in text for token in AGE_TOKENS)
        )
    )


def unresolved_identity_fields(character):
    """Return user-facing names of unresolved production identity fields."""
    fields = []
    if not explicit_gender((character or {}).get("gender")
                           or (character or {}).get("sex")):
        fields.append("性别")
    if not explicit_age_range((character or {}).get("age_range")):
        fields.append("年龄段")
    return fields
