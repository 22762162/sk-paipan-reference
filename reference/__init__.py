"""独立的传统命理/易学盲写参考实现。

这里的模块与 ``paipan_ref`` 正式契约实现物理隔离，只用于算法对拍。
所有函数均为确定性的纯函数，运行时不访问网络、时钟或外部历法数据。
"""

from .dayun_ref import calculate_dayun, dayun, dayun_direction, dayun_sequence, ganzhi_sequence
from .liuyao_ref import liuyao
from .meihua_ref import meihua
from .relations_ref import branch_relations, find_branch_relations, ten_god
from .shensha_ref import shensha
from .wuhudun_ref import flowing_months, first_month_stem, wuhudun

__all__ = [
    "branch_relations",
    "calculate_dayun",
    "dayun",
    "dayun_direction",
    "dayun_sequence",
    "find_branch_relations",
    "first_month_stem",
    "flowing_months",
    "ganzhi_sequence",
    "liuyao",
    "meihua",
    "shensha",
    "ten_god",
    "wuhudun",
]
