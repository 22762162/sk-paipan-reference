"""图片视觉质检:核对剧本要求,不合格自动重画;镜头景别多样性。"""

from pathlib import Path

import pytest

from aifos.app import App
from aifos.errors import AifosError
from aifos.production.base import ProviderResult


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _preproduce(app, title="小鹿的一天Vlog", number=1):
    app.director.produce(title, number, pause_for_confirm=True)
    summary = app.director.produce(title, number, pause_for_confirm=True)
    if summary["status"] == "awaiting_cast":
        project = app.projects.get_project(title)
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=?",
            (project["id"], number))
        script, _ = app.projects.latest_document(episode["id"], "script")
        for character in script["characters"]:
            app.director.select_character_candidate(
                title, number, character["name"], 1)
        app.director.produce(title, number, pause_for_confirm=True)
    return app.projects.get_project(title)


def test_qc_prompt_and_validation():
    from aifos.adapters.claude_script import (build_prompt,
                                              validate_image_qc)
    prompt = build_prompt("image_qc", {
        "image_uri": "/tmp/shot.png", "characters": ["小鹿", "石头"],
        "count": 2, "designs": "小鹿(发型:双丸子头)",
        "location": "夜市", "action": "追查线索",
        "camera": "全景", "forbid": ["字幕条"]})
    assert "/tmp/shot.png" in prompt
    assert "小鹿、石头" in prompt and "共 2 个" in prompt
    assert "名字不代表物种" in prompt
    assert "设定写明物种就按设定画" in prompt
    assert "悬挂的衣物" in prompt
    assert "全景" in prompt
    ok = {"pass": True, "issues": []}
    assert validate_image_qc(ok) is None
    bad = {"pass": False, "issues": "镜头9画成了动物"}
    assert validate_image_qc(bad) is None
    assert bad["issues"] == ["镜头9画成了动物"]
    assert validate_image_qc({"issues": []}) == "缺少 pass 字段"


def test_qc_fail_triggers_auto_redraw(app, tmp_path):
    """质检不过 → 自动带意见重画 → 通过;意见进入重画载荷。"""
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
    calls = {"image": [], "qc": []}

    class StubRouter:
        def call(self, capability, payload, out_dir, cancel=None):
            if capability == "image":
                calls["image"].append(dict(payload))
                return ProviderResult(provider="codex", cost=1.0,
                                      uri=str(image))
            calls["qc"].append(dict(payload))
            first = len(calls["qc"]) == 1
            return ProviderResult(
                provider="claude", cost=0.5,
                data={"pass": not first,
                      "issues": (["小鹿被画成了动物"] if first else [])})

    app.director.router = StubRouter()
    result = app.director._generate_image_with_qc(
        "image", {"prompt": "x", "shot_no": 1}, tmp_path, None,
        {"characters": ["小鹿"], "count": 1, "designs": "",
         "location": "", "action": "", "forbid": []})
    assert len(calls["image"]) == 2          # 首画 + 质检重画
    assert "小鹿被画成了动物" in calls["image"][1]["feedback"]
    assert result.qc["passed"] is True
    assert result.qc["attempts"] == 2
    assert result.cost == 3.0        # 两次出图(2.0)+两次质检(1.0)


def test_qc_report_lands_in_plan(app):
    """初始母资产不空耗视觉 QC；正式镜头图必须带通过结果。"""
    import json
    project = _preproduce(app)
    plan = json.loads(
        (app.workspace.artifacts_dir / f"p{project['id']:03d}" / "e001"
         / "render_plan.json").read_text(encoding="utf-8"))
    for cat in ("character_candidate", "character_sheet", "scene_art"):
        drawn = [i for i in plan["items"]
                 if i["category"] == cat
                 and i["status"] in ("done", "reused")]
        assert drawn, f"{cat} 无生成条目"
        assert all("qc" not in i for i in drawn), f"{cat} 不应做初始视觉质检"
    # 关键帧在生成阶段自动质检；首尾帧由整集批量质检同时核对
    # 首、尾两张，避免把帧容器条目误当成单张生成结果。
    drawn = [i for i in plan["items"]
             if i["category"] == "shot_image"
             and i["status"] in ("done", "reused")]
    assert drawn, "shot_image 无生成条目"
    assert all(i.get("qc", {}).get("passed") for i in drawn), \
        "shot_image 缺质检结果"

    report = app.director.qc_all(project["title"], 1)
    assert report["failed"] == 0
    plan = json.loads(
        (app.workspace.artifacts_dir / f"p{project['id']:03d}" / "e001"
         / "render_plan.json").read_text(encoding="utf-8"))
    for cat in ("shot_image", "frames"):
        drawn = [i for i in plan["items"]
                 if i["category"] == cat
                 and i["status"] in ("done", "reused")]
        assert drawn, f"{cat} 无生成条目"
        assert all(i.get("qc", {}).get("passed") for i in drawn), \
            f"{cat} 缺质检结果"


def test_camera_scales_are_varied(app):
    """镜头景别不再近景为主:每场开场全/远景,相邻景别变化。"""
    project = _preproduce(app, title="景别测试")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    storyboard, _ = app.projects.latest_document(
        episode["id"], "storyboard")
    shots = storyboard["shots"]
    scales = [((s.get("five_dimensions") or {})
               .get("camera_design") or {}).get("shot_scale", "")
              for s in shots]
    scales = [x for x in scales if x]
    assert len(set(scales)) >= 3, f"景别过于单一: {scales}"
    close = sum(1 for x in scales if x in ("近景", "特写", "大特写"))
    assert close < len(scales), "全部是近景/特写"
    wide = sum(1 for x in scales if x in ("全景", "远景"))
    assert wide >= 1, f"没有任何全景/远景定场: {scales}"


def test_species_flows_into_prompts(app):
    """形态字段进入设定与提示词:默认人类,动物角色按设定执行。"""
    project = _preproduce(app, title="形态测试")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    name = script["characters"][0]["name"]
    design = app.director._character_design(project["id"], name)
    assert design.get("species") == "人类"
    import json as _json
    plan = _json.loads(
        (app.workspace.artifacts_dir / f"p{project['id']:03d}" / "e001"
         / "render_plan.json").read_text(encoding="utf-8"))
    portrait = next(i for i in plan["items"] if i["id"] == f"char:{name}")
    assert "形态:人类" in portrait["prompt"]


def test_frames_are_chained_within_scene(app):
    """帧链:同场景内 上一镜尾帧 = 下一镜首帧;拼接处画面连贯。"""
    project = _preproduce(app, title="帧链测试")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    storyboard, _ = app.projects.latest_document(
        episode["id"], "storyboard")
    by_scene = {}
    for shot in storyboard["shots"]:
        by_scene.setdefault(shot["scene_no"], []).append(shot)
    chained = 0
    for scene_shots in by_scene.values():
        for prev, cur in zip(scene_shots, scene_shots[1:]):
            prev_last = app.assets.latest(
                project["id"], "last_frame",
                f"e001_shot{prev['shot_no']:03d}")
            cur_first = app.assets.latest(
                project["id"], "first_frame",
                f"e001_shot{cur['shot_no']:03d}")
            assert Path(prev_last["uri"]).read_bytes() ==                 Path(cur_first["uri"]).read_bytes(),                 f"镜头{prev['shot_no']}尾帧 != 镜头{cur['shot_no']}首帧"
            chained += 1
    assert chained >= 2, "没有可验证的帧链对"


def test_redo_placeholders_only_redraws_mock_items(app):
    """一键补真:只重画清单里的占位条目,其余不动。"""
    import json as _json
    project = _preproduce(app, title="补真测试")
    plan_path = (app.workspace.artifacts_dir
                 / f"p{project['id']:03d}" / "e001" / "render_plan.json")
    plan = _json.loads(plan_path.read_text(encoding="utf-8"))
    target_item = next(i for i in plan["items"]
                       if i["category"] == "scene_art")
    for item in plan["items"]:
        item["real"] = item["id"] == target_item["id"] and False or True
    target_item["real"] = False
    plan_path.write_text(_json.dumps(plan, ensure_ascii=False),
                         encoding="utf-8")
    name = target_item["name"]
    before = app.assets.latest(project["id"], "scene_art", name)
    others_before = app.assets.latest(
        project["id"], "character_sheet",
        next(i["name"] for i in plan["items"]
             if i["category"] == "character_sheet") + ":turnaround")
    summary = app.director.redo_placeholders("补真测试", 1)
    assert summary["status"] == "done" and summary["redone"] == 1
    after = app.assets.latest(project["id"], "scene_art", name)
    assert after["version"] == before["version"] + 1
    others_after = app.assets.latest(
        project["id"], "character_sheet", others_before["name"])
    assert others_after["version"] == others_before["version"]


def test_regen_frames_only(app):
    """只重做首尾帧:沿用关键图与帧链,作废旧视频。"""
    project = _preproduce(app, title="帧重做测试")
    name = "e001_shot002"
    img_before = app.assets.latest(project["id"], "image", name)
    frame_before = app.assets.latest(project["id"], "first_frame", name)
    app.director.regen_image("帧重做测试", 1,
                             {"kind": "frames", "shot_no": 2})
    assert app.assets.latest(project["id"], "image",
                             name)["version"] == img_before["version"]
    assert app.assets.latest(
        project["id"], "first_frame",
        name)["version"] == frame_before["version"] + 1
    # 帧链仍连贯:首帧 = 上一镜(同场)尾帧
    prev_last = app.assets.latest(project["id"], "last_frame",
                                  "e001_shot001")
    cur_first = app.assets.latest(project["id"], "first_frame", name)
    from pathlib import Path as _P
    assert _P(prev_last["uri"]).read_bytes() == \
        _P(cur_first["uri"]).read_bytes()


def test_regen_frames_with_prompt_override(app):
    """首尾帧可改提示词单独重画:清单标记已改词,版本递增。"""
    import json as _json
    project = _preproduce(app, title="帧改词测试")
    name = "e001_shot002"
    before = app.assets.latest(project["id"], "first_frame", name)
    app.director.regen_image(
        "帧改词测试", 1, {"kind": "frames", "shot_no": 2},
        prompt_override="尾帧定格在角色回头的瞬间,逆光剪影")
    after = app.assets.latest(project["id"], "first_frame", name)
    assert after["version"] == before["version"] + 1
    plan = _json.loads(
        (app.workspace.artifacts_dir / f"p{project['id']:03d}" / "e001"
         / "render_plan.json").read_text(encoding="utf-8"))
    item = next(i for i in plan["items"] if i["id"] == "frames:2")
    assert item["custom_prompt"] is True
    assert "逆光剪影" in item["prompt"]


def test_openai_api_uses_reference_images(tmp_path, monkeypatch):
    """API 出图有参考图时走 images/edits 多图输入,与 CLI 用参考图一致。"""
    from aifos.production import api_providers
    from aifos.production.api_providers import OpenAIImageProvider

    anchor = tmp_path / "anchor.png"
    anchor.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
    portrait = tmp_path / "portrait.png"
    portrait.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 8)

    captured = {}

    def fake_multipart(name, url, headers, fields, files, timeout):
        captured["url"] = url
        captured["files"] = [f[1].name for f in files]
        captured["prompt"] = fields["prompt"]
        return {"data": [{"b64_json": "aGk="}]}

    def fake_json(*a, **k):
        captured["fell_back_to_generations"] = True
        return {"data": [{"b64_json": "aGk="}]}

    monkeypatch.setattr(api_providers, "_multipart_post", fake_multipart)
    monkeypatch.setattr(api_providers, "_request_json", fake_json)
    provider = OpenAIImageProvider("image_api", {
        "type": "image_api", "enabled": True,
        "capabilities": ["image"], "api_key": "sk-x",
        "model": "gpt-image-2"})
    result = provider.generate("image", {
        "character_sheet": "makeup", "art_name": "小鹿",
        "prompt": "妆容设定", "aspect": "9:16",
        "character_refs": [str(portrait)],
        "style_ref": str(anchor)}, tmp_path)
    assert "images/edits" in captured["url"]
    # 人工锁定人物图优先于风格图，不能因接口上限被挤掉
    assert captured["files"][0] == "portrait.png"
    assert "anchor.png" in captured["files"]
    assert "禁止漂移" in captured["prompt"]
    assert "fell_back_to_generations" not in captured
    assert result.provider == "image_api"


def test_openai_api_no_reference_falls_back_to_generations(tmp_path,
                                                           monkeypatch):
    """没有可用参考图时回退纯文本 generations,不崩。"""
    from aifos.production import api_providers
    from aifos.production.api_providers import OpenAIImageProvider

    used = {}

    def fake_gen(*a, **k):
        used["gen"] = True
        return {"data": [{"b64_json": "aGk="}]}

    def fake_edit(*a, **k):
        used["edit"] = True
        return {"data": [{"b64_json": "aGk="}]}

    monkeypatch.setattr(api_providers, "_request_json", fake_gen)
    monkeypatch.setattr(api_providers, "_multipart_post", fake_edit)
    provider = OpenAIImageProvider("image_api", {
        "type": "image_api", "enabled": True,
        "capabilities": ["image"], "api_key": "sk-x"})
    provider.generate("image", {
        "portrait": True, "art_name": "小鹿", "prompt": "立绘",
        "aspect": "9:16"}, tmp_path)
    assert used.get("gen") and "edit" not in used


def test_single_and_batch_qc_and_redo(app):
    """单张质检 / 批量质检 / 批量重画未过 三件套。"""
    import json as _json
    project = _preproduce(app, title="质检三件套")
    plan_path = (app.workspace.artifacts_dir
                 / f"p{project['id']:03d}" / "e001" / "render_plan.json")
    plan_before = _json.loads(plan_path.read_text(encoding="utf-8"))["items"]
    initial = next(i for i in plan_before if i["category"] == "scene_art")
    with pytest.raises(AifosError, match="初始人物/场景母资产不做视觉质检"):
        app.director.qc_item("质检三件套", 1, initial["id"])
    # 正式镜头单张质检:mock 默认通过
    item = next(i for i in plan_before
                if i["category"] == "shot_image")
    report = app.director.qc_item("质检三件套", 1, item["id"])
    assert report["passed"] is True
    after = next(i for i in _json.loads(
        plan_path.read_text(encoding="utf-8"))["items"]
        if i["id"] == item["id"])
    assert after["qc"]["passed"] is True
    # 批量质检:全部核对
    summary = app.director.qc_all("质检三件套", 1)
    assert summary["status"] == "done" and summary["checked"] > 0
    # 人为标记两张未过 → 批量重画未过
    plan = _json.loads(plan_path.read_text(encoding="utf-8"))
    fail_ids = [i["id"] for i in plan["items"]
                if i["category"] == "shot_image"][:2]
    vers = {}
    for i in plan["items"]:
        if i["id"] in fail_ids:
            i["qc"] = {"passed": False, "issues": ["测试标记未过"],
                       "attempts": 1}
            t = app.director._plan_item_target(i["id"])
            vers[i["id"]] = t
    plan_path.write_text(_json.dumps(plan, ensure_ascii=False),
                         encoding="utf-8")
    updates = []
    redo = app.director.redo_items(
        "质检三件套", 1, only_failed=True,
        progress=lambda **fields: updates.append(fields))
    assert redo["status"] == "done" and redo["redone"] == 2
    assert redo["checked"] == 2
    assert {u["phase"] for u in updates} >= {
        "queued", "redrawing", "checking", "done"}
    final_plan = _json.loads(plan_path.read_text(encoding="utf-8"))
    redrawn = [i for i in final_plan["items"] if i["id"] in fail_ids]
    assert all(i["revision"]["source"] == "batch_qc" for i in redrawn)
    assert all(i["revision"]["prompt_modified"] for i in redrawn)
    assert all("测试标记未过" in i["prompt"] for i in redrawn)
    assert all(i["reference_inputs"]["attached"] for i in redrawn)
    assert all(i["reference_inputs"]["count"] >= 1 for i in redrawn)


def test_codex_qc_instruction_and_parse(tmp_path, monkeypatch):
    """Codex 图像质检:构造读图指令,从 stdout 解析判定 JSON。"""
    from aifos.adapters import codex_image
    instruction, targets, data = codex_image.build_instruction(
        "image_qc", {"image_uri": "/tmp/f.png",
                     "characters": ["小鹿"], "count": 1,
                     "designs": "小鹿(发型:银白短发)",
                     "location": "地铁站", "forbid": ["悬挂衣物"]}, tmp_path)
    assert "/tmp/f.png" in instruction and "小鹿" in instruction
    assert '"pass"' in instruction
    assert targets == [] and data["qc"] is True

    stdout = '思考中…\n{"pass": false, "issues": ["尾帧换了个人"]}\n完成'

    class FakePopen:
        def __init__(self, *a, **k):
            self.args = a[0] if a else []
            self.returncode = 0

        def communicate(self, timeout=None):
            return stdout, ""

    monkeypatch.setattr(codex_image.shutil, "which",
                        lambda _c: "/usr/bin/codex")
    monkeypatch.setattr(codex_image.subprocess, "Popen", FakePopen)
    reply = codex_image.run({
        "capability": "image_qc",
        "payload": {"image_uri": "/tmp/f.png", "characters": ["小鹿"]},
        "out_dir": str(tmp_path)}, "codex", 30, [])
    assert reply["ok"] is True
    assert reply["data"]["pass"] is False
    assert reply["data"]["issues"] == ["尾帧换了个人"]


def test_frames_qc_checks_both_frames(app, monkeypatch):
    """首尾帧质检:首帧或尾帧任一不符即整组不合格。"""
    project = _preproduce(app, title="首尾帧质检")
    calls = {"n": 0}
    real_call = app.director.router.call

    def qc_router(capability, payload, out_dir, cancel=None):
        if capability == "image_qc":
            calls["n"] += 1
            # 第 2 次(尾帧)判不合格
            from aifos.production.base import ProviderResult
            return ProviderResult(
                provider="codex", cost=0.1,
                data={"pass": calls["n"] != 2,
                      "issues": [] if calls["n"] != 2 else ["尾帧人物不符"]})
        return real_call(capability, payload, out_dir, cancel=cancel)

    app.director.router.call = qc_router
    report = app.director.qc_item("首尾帧质检", 1, "frames:2")
    assert calls["n"] == 2                 # 首帧 + 尾帧都检了
    assert report["passed"] is False
    assert any("尾帧" in x for x in report["issues"])
