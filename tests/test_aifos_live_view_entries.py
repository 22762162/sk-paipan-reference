"""生产实况/恢复视图必须能进分镜表、画布与 3D 空间调度。

实测反馈:关键帧生产中的页面只有「图片清单 + 暂停生成」,分镜头、画布和
3D 空间全都进不去——因为 renderCanvasView 有条硬分流「制作进行中一律进
生产直播页」,而直播页工具栏没带这些入口。
"""

from pathlib import Path

STATIC = Path(__file__).parents[1] / "aifos" / "web" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def test_board_and_canvas_subroutes_exist():
    # 子路由是生产中进完整视图的唯一通道
    assert r"/^#\/episode\/(\d+)(?:\/(board|canvas))?$/" in JS
    assert 'renderCanvasView(Number(m[1]), m[2] || "")' in JS


def test_force_view_bypasses_the_live_view_branch():
    assert 'async function renderCanvasView(episodeId, forceView = "")' in JS
    # 有分镜才放行:缺分镜时仍回落原分流,不给空壳页
    assert '!stable.includes(ep.status) && !(forceView && sb)' in JS


def test_sub_route_decides_which_view_opens():
    assert 'forceView === "canvas" ? "canvas"' in JS
    assert 'forceView === "board" ? "theater"' in JS


def test_live_toolbar_exposes_all_three_entries():
    for ident in ("btn-board-live", "btn-canvas-live", "btn-blocking-live"):
        assert f'id="{ident}"' in JS, f"实况工具栏缺 {ident}"
    assert 'document.getElementById("btn-board-live")?.addEventListener' in JS
    assert 'document.getElementById("btn-canvas-live")?.addEventListener' in JS
    assert '"btn-blocking-live")?.addEventListener' in JS


def test_recovery_toolbar_exposes_all_three_entries():
    for ident in ("btn-board-recovery", "btn-canvas-recovery",
                  "btn-blocking-recovery"):
        assert f'id="{ident}"' in JS, f"恢复视图工具栏缺 {ident}"


def test_blocking_stays_an_overlay_not_a_navigation():
    # 3D 空间调度是浮层,任何视图都能开,不该跳走丢掉生产实况
    assert '"btn-blocking-live")?.addEventListener(\n    "click", () => showBlockingOverlay(episodeId))' in JS \
        or 'showBlockingOverlay(episodeId))' in JS
