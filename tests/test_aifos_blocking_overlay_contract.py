from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_blocking_overlay_separates_scene_truth_from_movement_control():
    source = (ROOT / "aifos" / "web" / "static" / "app.js").read_text(
        encoding="utf-8")

    assert "进入真实布景·3D片场" in source
    assert "人物/机位控制，仅控制走位" in source
    assert "控制图的网格、文字、路线与图标禁止进入最终画面" in source


def test_blocking_overlay_surfaces_failed_previz_instead_of_calling_it_pending():
    source = (ROOT / "aifos" / "web" / "static" / "app.js").read_text(
        encoding="utf-8")

    assert "const previzIssues = blocking.validation?.previz_issues || []" in source
    assert "空间门禁未通过" in source
    assert "人物/摄影机路径检查发现" in source


def test_mobile_shell_cache_is_bumped_for_scene_truth_ui():
    sw = (ROOT / "aifos" / "web" / "static" / "sw.js").read_text(
        encoding="utf-8")
    index = (ROOT / "aifos" / "web" / "static" / "index.html").read_text(
        encoding="utf-8")

    assert 'aifos-mobile-shell-v8' in sw
    assert "20260811-scene-truth-1" in index
