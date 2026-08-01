"""Generation-quality rules are wired to real prompts without batch blocking."""

import sqlite3

import pytest

from aifos.app import App
from aifos.errors import AifosError
from aifos.lessons import DOMAIN_IMAGE, DOMAIN_VIDEO, adopt_distilled_rules
from aifos.production.base import ProviderResult
from aifos.workflow import frame_content_qc_accepted


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _ctx(app, title="质量接线"):
    project, _ = app.projects.get_or_create_project(title)
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    return {
        "project": dict(project),
        "episode": dict(episode),
        "out_root": app.workspace.artifacts_dir / f"p{project['id']:03d}" / "e001",
    }


def test_rules_use_separate_image_video_domains_and_combined_cap(app):
    ctx = _ctx(app)
    adopt_distilled_rules(
        app.assets, ctx["project"]["id"],
        [{"rule": "图片域只保留明确单一机位。"}], domain=DOMAIN_IMAGE)
    adopt_distilled_rules(
        app.assets, ctx["project"]["id"],
        [{"rule": "视频域只保持道具持有人连续。"}], domain=DOMAIN_VIDEO)

    image_lines = app.director._generation_rule_lines(
        ctx, {"camera": "固定机位"}, {"camera": "固定机位"},
        modality="image")
    video_lines = app.director._generation_rule_lines(
        ctx,
        {"description": "手先接触杯柄，再平稳拿起杯子"},
        {
            "action": "手先接触杯柄，再平稳拿起杯子",
            "physical_contract": {"rules": ["杯子受重力且持有人连续"]},
            "start_state": {"杯子": {"position": "桌面"}},
            "end_state": {"杯子": {"position": "手中"}},
            "dialogue": "",
        },
        modality="video")

    assert len(image_lines) <= 5 and len(video_lines) <= 5
    assert any("图片域" in line for line in image_lines)
    assert not any("视频域" in line for line in image_lines)
    assert any("视频域" in line for line in video_lines)
    assert not any("图片域" in line for line in video_lines)

    assert app.director._generation_rule_lines(
        ctx, {}, {}, modality="image") == ()


def test_current_shot_critical_rules_cannot_be_evicted_by_history(app):
    ctx = _ctx(app, "关键规则优先")
    adopt_distilled_rules(
        app.assets, ctx["project"]["id"],
        [{"rule": f"历史视频规则{index}"} for index in range(1, 6)],
        domain=DOMAIN_VIDEO)
    shot = {
        "characters": ["甲"],
        "complex_action": True,
        "camera": "中景缓推",
        "description": "甲伸手拿起杯子",
    }
    payload = {
        "characters": ["甲"], "visible_figure_count": 1,
        "camera": "中景缓推", "action": "甲伸手拿起杯子",
        "start_state": {"甲": {"prop": "杯子在桌上"}},
        "end_state": {"甲": {"prop": "杯子在手中"}},
        "physical_contract": {"rules": ["手先接触杯柄，杯子再离桌"]},
        "spatial_blocking": {"constraint": "甲在桌前"},
        "dialogue": "不要把字符串对白当成字典",
    }

    lines = app.director._generation_rule_lines(
        ctx, shot, payload, modality="video")

    assert len(lines) == 5
    joined = "\n".join(lines)
    assert "起始、过渡、结束" in joined
    assert "道具位置、朝向、持有人和受力连续" in joined
    assert "空间图站位" in joined
    assert "人数严格等于镜头合同" in joined
    assert "一个明确景别" in joined
    assert "历史视频规则" not in joined


def test_video_preflight_does_not_scan_connectors_in_full_compact_prompt(app):
    shot = {
        "shot_no": 8,
        "description": "甲平稳抬起右手",
        "characters": ["甲"],
        "single_action": True,
    }
    payload = {
        "shot_no": 8,
        "action": "甲平稳抬起右手",
        "prompt_compact": "起点保持站立，然后准确到达终点状态。",
        "characters": ["甲"],
        "visible_figure_count": 1,
        "physical_contract": {"rules": ["手臂关节自然"]},
        "start_state": {"甲": {"pose": "垂手"}},
        "end_state": {"甲": {"pose": "抬手"}},
    }

    issues = app.director._generation_preflight_issues(
        shot, payload, modality="video")

    assert not any(
        issue.code == "motion.single_action_has_multiple_phases"
        for issue in issues)


def test_video_preparation_failure_isolates_only_bad_shot(app, monkeypatch):
    ctx = _ctx(app, "视频逐镜隔离")
    ctx.update({"frames": [], "videos": []})
    shots = [{"shot_no": 1, "duration": 3},
             {"shot_no": 2, "duration": 3}]
    monkeypatch.setattr(app.director, "_active_shots", lambda _ctx: shots)
    monkeypatch.setattr(
        app.director, "_existing_asset_uri",
        lambda *_args, **_kwargs: "")

    def prepare(_ctx, shot, _frames):
        if shot["shot_no"] == 1:
            raise AifosError("镜头1物理合同缺失")
        return {"shot": shot, "payload": {"shot_no": 2}}

    monkeypatch.setattr(app.director, "_prepare_video_call", prepare)
    seen = []

    def run(_ctx, tasks):
        seen.extend(tasks)
        return {2: {"shot_no": 2, "uri": "/tmp/shot2.mp4"}}

    monkeypatch.setattr(app.director, "_run_videos_parallel", run)

    result = app.director._stage_videos(ctx)

    assert [task["shot"]["shot_no"] for task in seen] == [2]
    assert ctx["videos"] == [{"shot_no": 2, "uri": "/tmp/shot2.mp4"}]
    assert result["contract_incomplete"] is True
    assert result["contract_incomplete_shots"] == [1]
    assert ctx["video_contract_repair_required"] is True


def test_video_provider_technical_failure_retries_once(app, monkeypatch):
    ctx = _ctx(app, "视频技术重试")
    ctx["out_root"].mkdir(parents=True, exist_ok=True)
    calls = []

    def call(_capability, _payload, _out_dir, _cancel=None):
        calls.append(1)
        if len(calls) == 1:
            from aifos.errors import ProviderError
            raise ProviderError("临时限流")
        return ProviderResult(
            provider="seedance", cost=1.0, uri="/tmp/ok.mp4", data={})

    monkeypatch.setattr(app.director.router, "call", call)
    task = {"shot": {"shot_no": 3}, "payload": {"prompt": "单一动作"}}

    result = app.director._call_video_with_technical_retry(
        ctx, task, direct_router=True)

    assert len(calls) == 2
    assert result.data["technical_retry_count"] == 1


def test_video_content_issue_never_enters_technical_retry(app, monkeypatch):
    ctx = _ctx(app, "视频内容不返工")
    calls = []

    def call(*_args, **_kwargs):
        calls.append(1)
        raise AifosError("人物表情不够理想")

    monkeypatch.setattr(app.director.router, "call", call)
    task = {"shot": {"shot_no": 4}, "payload": {"prompt": "单一动作"}}

    with pytest.raises(AifosError, match="表情"):
        app.director._call_video_with_technical_retry(
            ctx, task, direct_router=True)

    assert len(calls) == 1


def test_frame_qc_waiver_is_auditable_without_faking_visual_pass():
    frame = {
        "first": "/tmp/first.png",
        "last": "/tmp/last.png",
        "qc_passed": False,
        "content_qc_enabled": False,
        "content_qc_waived": True,
    }

    assert frame_content_qc_accepted(frame) is True
    assert frame["qc_passed"] is False
    assert frame_content_qc_accepted({"qc_passed": False}) is False


def test_contract_safe_copy_converts_sqlite_rows_before_provider_payload(app):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("CREATE TABLE asset (id INTEGER, name TEXT)")
        connection.execute("INSERT INTO asset VALUES (7, '空间图')")
        row = connection.execute("SELECT * FROM asset").fetchone()

        copied = app.director._contract_safe_copy({"row": row})

        assert copied == {"row": {"id": 7, "name": "空间图"}}
    finally:
        connection.close()


def test_cast_selection_wait_path_returns_true_not_undefined(app, monkeypatch):
    ctx = _ctx(app, "候选等待路径")
    ctx.update({
        "script": {
            "characters": [{"name": "主角", "role": "protagonist"}],
            "scenes": [{"scene_no": 1, "location": "房间"}],
        },
        "story_analysis": {},
    })
    monkeypatch.setattr(
        "aifos.director.character_production_readiness_error",
        lambda *_args: "")
    monkeypatch.setattr(app.director, "_active_scenes",
                        lambda _ctx: ctx["script"]["scenes"])
    monkeypatch.setattr(app.director, "_skipped_scenes", lambda _ctx: set())
    monkeypatch.setattr(app.director, "_ensure_character_designs",
                        lambda *_args: {})
    monkeypatch.setattr(app.director, "_anchor_character",
                        lambda *_args, **_kwargs: "主角")
    character_selection = {
        "total": 1,
        "characters": [{"name": "主角", "candidate_count": 4}],
    }
    prop_selection = {"total": 0, "props": []}
    monkeypatch.setattr(app.director, "_ensure_character_candidates",
                        lambda *_args: character_selection)
    monkeypatch.setattr(app.director, "_complete_prop_designs",
                        lambda *_args: [])
    monkeypatch.setattr(app.director, "_ensure_prop_candidates",
                        lambda *_args: prop_selection)
    monkeypatch.setattr(app.director, "_combine_asset_selection",
                        lambda *_args: {
                            "required": True,
                            "characters": character_selection["characters"],
                            "asset_locked": 0,
                        })

    result = app.director._stage_cast(ctx)

    assert result["awaiting_selection"] is True
