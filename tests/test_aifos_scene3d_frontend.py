"""3D 调度独立前端契约：真值、手机操控和显式修复不得回退。"""
from pathlib import Path


PAGE = (Path(__file__).resolve().parents[1]
        / "aifos" / "web" / "static" / "scene3d.html")


def source():
    return PAGE.read_text(encoding="utf-8")


def test_truth_is_default_and_silent_repairs_are_preview_only():
    html = source()

    assert 'let previewMode="truth"' in html
    assert "function contractRoutePath(" in html
    assert 'NAV.actors[i]=previewMode==="repair"?' in html
    assert 'e=previewMode==="repair"?resolveEye(eRaw):eRaw' in html
    assert 'cs=previewMode==="repair"?resolveEye(csRaw):csRaw' in html
    assert '(previewMode==="repair"&&lens&&sceneGeo.ranges)' in html
    assert "function contractFacingYaw(" in html
    assert 'if(previewMode!=="truth")' in html
    assert "修复预览未保存，不能进入导出" in html


def test_structure_and_realistic_modes_have_one_tap_semantics():
    html = source()

    assert 'if(displayMode==="structure")' in html
    assert "show.control=true;show.frus=true;show.axis=true" in html
    assert "else show.control=false" in html
    assert 'id="truthMode"' in html
    assert 'id="repairMode"' in html


def test_mobile_stage_keeps_large_canvas_and_touch_controls():
    html = source()

    assert "@media(max-width:600px)" in html
    assert "#view{min-height:55vh}" in html
    assert "min-height:44px" in html
    assert "#shots{flex-wrap:nowrap!important" in html
    assert 'id="zoomIn"' in html
    assert 'id="zoomOut"' in html
    assert 'id="resetView"' in html
    assert "const pointers=new Map()" in html
    assert "function gestureMetrics(" in html
    assert "panOrbit(current.x-gesture.x,current.y-gesture.y)" in html
    assert "gesture.distance/Math.max(1,current.distance)" in html


def test_object_ray_pick_editor_and_revision_save_contract():
    html = source()

    assert 'locationEntry("scene_model_contracts")' in html
    assert 'locationEntry("scene_models")' in html
    assert "obj.semantic_type" in html
    assert "function rayRangeDistance(" in html
    assert "function pickObject(" in html
    for element_id in (
            "editX", "editY", "editZ", "editW", "editH", "editD",
            "editYaw", "editMount", "editSupport", "undoEdit",
            "redoEdit", "saveEdits"):
        assert f'id="{element_id}"' in html
    assert 'method:"POST"' in html
    assert 'method:"PATCH"' in html
    for field in (
            "physical_scene_id", "expected_revision", "updates",
            "position_3d", "rotation_y_deg", "support_id", "mount_type"):
        assert field in html
    assert "response.status===409" in html


def test_quality_is_split_and_issues_can_locate_objects():
    html = source()

    assert "function updateQualityBadges(" in html
    for label in ("几何 ", "语义 ", "材质 ", "灯光 已验证", "灯光 未验证"):
        assert label in html
    assert "function issueObjectKey(" in html
    assert "focusObject(row.objectKey)" in html
    assert "` · 真实物件 ${renderStats.semanticPrefabs}/${renderStats.objects}`" not in html
    assert '!["unverified","default","fallback","category_render_default"]' in html
    assert "opaque luminous backing turned a large lattice window" in html


def test_material_lighting_issues_are_grouped_and_have_safe_refresh_action():
    html = source()

    assert 'id="analyzeAppearance"' in html
    assert 'id="appearanceStatus"' in html
    assert '"/api/scene3d/appearance?episode="' in html
    assert "读取全景可见材质与主光，不会改物体位置和尺寸" in html
    assert "function warn(" in html
    for label in ("材质", "灯光", "空间关系", "几何"):
        assert f'return "{label}"' in html
    assert "groupSummary" in html
    assert 'raw.verified!==true' in html


def test_actor_stature_is_separate_from_pose_envelope():
    html = source()

    assert "function actorStature(" in html
    assert '"start_stature_m"' in html
    assert 'actor.stature_m' in html
    assert 'ratios={sitting:1.22/1.68' in html
    assert "function addActorSolid(kit,x,y,z,envelope,stature" in html
    assert 'actorPose==="sitting"||actorPose==="leaning_seated"' in html
    assert 'actorPose==="lying"' in html
    assert "startStature=actorStature(" in html
    assert "endStature=actorStature(" in html
    assert "addActorSolid(kit,s[0],s[1]+bob,s[2],h0,stature0" in html
