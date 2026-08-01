
# ---- 临时垫片(仅本分支):基底 bb71b58 的 director.py 引用公有名
# canvas_from_world,但该提交内 spatial_blocking 只有私有 _canvas_from_world
# (公有化改名在 Codex 未提交切片中)。别名垫平使本分支可独立跑测试;
# 合并 Codex 空间切片后删除本段。
import aifos.spatial_blocking as _sb  # noqa: E402
if not hasattr(_sb, "canvas_from_world"):
    _sb.canvas_from_world = _sb._canvas_from_world
