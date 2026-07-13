"""干支名称与柱输出的公共纯函数。

名称顺序直接来自 ``contracts/paipan-spec.md`` 第 2 节。
"""

STEMS = ("甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸")
BRANCHES = ("子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥")


def pillar_from_indexes(stem: int, branch: int) -> dict[str, str]:
    """把已经计算出的干、支序号转换成协议中的柱对象。"""

    stem_name = STEMS[stem]
    branch_name = BRANCHES[branch]
    return {
        "stem": stem_name,
        "branch": branch_name,
        "ganzhi": stem_name + branch_name,
    }
