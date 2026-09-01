"""项目/本集创作规则的版本存储、CAS 与 Web 归属边界。"""

import http.client
import json
import threading

import pytest

from aifos.app import App
from aifos.errors import AifosError
from aifos.project_center import DocumentConflictError
from aifos.web.server import CREATIVE_RULE_PACK_SCHEMA, serve


def _pack(scope, *rules, suppressions=None):
    return {
        "schema": CREATIVE_RULE_PACK_SCHEMA,
        "scope": scope,
        "rules": list(rules),
        "suppressions": list(suppressions or []),
    }


def _json_request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = None
    headers = {}
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response.status, json.loads(raw)


@pytest.fixture()
def rule_app(tmp_path):
    app = App(tmp_path / "ws")
    yield app
    app.close()


@pytest.fixture()
def rule_server(tmp_path):
    workspace = tmp_path / "ws"
    app = App(workspace)
    first, _ = app.projects.get_or_create_project("甲剧")
    second, _ = app.projects.get_or_create_project("乙剧")
    first_episode, _ = app.projects.get_or_create_episode(first["id"], 1)
    second_episode, _ = app.projects.get_or_create_episode(second["id"], 1)
    ids = {
        "first_project": first["id"],
        "second_project": second["id"],
        "first_episode": first_episode["id"],
        "second_episode": second_episode["id"],
    }
    app.close()
    httpd = serve(workspace, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {"port": httpd.server_address[1], "workspace": workspace, **ids}
    httpd.shutdown()
    httpd.server_close()


def test_project_documents_are_versioned_and_project_bound(rule_app):
    first, _ = rule_app.projects.get_or_create_project("甲剧")
    second, _ = rule_app.projects.get_or_create_project("乙剧")
    v1 = rule_app.projects.save_project_document_cas(
        first["id"], "project_rules", {"rules": ["甲"]}, 0)
    v2 = rule_app.projects.save_project_document_cas(
        first["id"], "project_rules", {"rules": ["甲", "乙"]}, 1)
    assert (v1, v2) == (1, 2)
    assert rule_app.projects.latest_project_document(
        first["id"], "project_rules") == ({"rules": ["甲", "乙"]}, 2)
    assert rule_app.projects.latest_project_document(
        second["id"], "project_rules") == (None, 0)

    with pytest.raises(DocumentConflictError) as stale:
        rule_app.projects.save_project_document_cas(
            first["id"], "project_rules", {"rules": []}, 1)
    assert stale.value.actual_version == 2
    with pytest.raises(AifosError, match="项目不存在"):
        rule_app.projects.save_project_document(999999, "project_rules", {})
    deleted = rule_app.history.delete_project("甲剧")
    assert deleted["deleted_project"] == "甲剧"
    assert rule_app.db.query_one(
        "SELECT 1 FROM project_documents WHERE project_id=?",
        (first["id"],)) is None


def test_episode_rule_cas_reuses_documents_table(rule_app):
    project, _ = rule_app.projects.get_or_create_project("甲剧")
    episode, _ = rule_app.projects.get_or_create_episode(project["id"], 1)
    version = rule_app.projects.save_document_cas(
        episode["id"], "episode_rules", {"rules": ["仅本集"]}, 0)
    assert version == 1
    row = rule_app.db.query_one(
        "SELECT kind, version FROM documents WHERE episode_id=?",
        (episode["id"],))
    assert dict(row) == {"kind": "episode_rules", "version": 1}
    assert rule_app.db.query_one(
        "SELECT 1 FROM project_documents WHERE project_id=?",
        (project["id"],)) is None


def test_project_rules_api_returns_default_pack_and_enforces_cas(rule_server):
    port = rule_server["port"]
    project_id = rule_server["first_project"]
    status, initial = _json_request(
        port, "GET", f"/api/project/{project_id}/rules")
    assert status == 200
    assert initial["version"] == 0
    assert initial["scope"] == "project_series"
    assert initial["rules"] == []

    content = _pack("project_series", {
        "key": "visual.rain",
        "category": "creative",
        "text": "全剧雨景保持冷青色",
        "enabled": True,
    })
    status, saved = _json_request(
        port, "POST", f"/api/project/{project_id}/rules",
        {"project_id": project_id, "expected_version": 0,
         "content": content})
    assert status == 200
    assert saved["version"] == 1
    assert saved["content"] == content

    status, conflict = _json_request(
        port, "POST", f"/api/project/{project_id}/rules",
        {"expected_version": 0, "content": content})
    assert status == 409
    assert conflict["expected_version"] == 0
    assert conflict["actual_version"] == 1


@pytest.mark.parametrize("rule", [
    {"key": "quality_gate.people", "category": "creative",
     "text": "人数不阻断", "enabled": False},
    {"key": "provider.seedance.input_contract", "category": "provider",
     "text": "覆盖供应商输入约束", "action": "override"},
    {"id": "frames", "category": "quality_gate",
     "text": "首尾帧仅警告", "severity": "warning"},
])
def test_rules_api_rejects_disabling_or_overriding_technical_gates(
        rule_server, rule):
    port = rule_server["port"]
    project_id = rule_server["first_project"]
    status, result = _json_request(
        port, "POST", f"/api/project/{project_id}/rules", {
            "expected_version": 0,
            "content": _pack("project_series", rule),
        })
    assert status == 400
    assert "技术硬门" in result["error"]
    status, current = _json_request(
        port, "GET", f"/api/project/{project_id}/rules")
    assert status == 200 and current["version"] == 0


def test_rules_api_rejects_suppressing_mandatory_gate(rule_server):
    port = rule_server["port"]
    project_id = rule_server["first_project"]
    status, result = _json_request(
        port, "POST", f"/api/project/{project_id}/rules", {
            "expected_version": 0,
            "content": _pack(
                "project_series", suppressions=["quality_gate.continuity"]),
        })
    assert status == 400
    assert "不能抑制系统技术硬门" in result["error"]


@pytest.mark.parametrize("selector", [
    [],
    "shot 2",
    2,
    None,
    {"shot_nos": {"bad": 2}},
    {"unknown_dimension": ["image"]},
    {"shot_nos": [0]},
    {"modalities": [True]},
])
def test_rules_api_rejects_malformed_applicability_without_saving(
        rule_server, selector):
    port = rule_server["port"]
    project_id = rule_server["first_project"]
    status, result = _json_request(
        port, "POST", f"/api/project/{project_id}/rules", {
            "expected_version": 0,
            "content": _pack("project_series", {
                "key": "visual.local_only",
                "text": "只在指定镜头生效",
                "applicability": selector,
            }),
        })

    assert status == 400
    assert "applicability" in result["error"]
    status, current = _json_request(
        port, "GET", f"/api/project/{project_id}/rules")
    assert status == 200
    assert current["version"] == 0
    assert current["rules"] == []


def test_rules_api_rejects_conflicting_or_malformed_suppression_scope(
        rule_server):
    port = rule_server["port"]
    project_id = rule_server["first_project"]
    bad_rows = [
        {"key": "visual.local_only", "applicability": ["shot 2"]},
        {"key": "visual.local_only", "applicability": {"shot_no": [2]},
         "applies_to": {"shot_no": [3]}},
    ]
    for suppression in bad_rows:
        status, result = _json_request(
            port, "POST", f"/api/project/{project_id}/rules", {
                "expected_version": 0,
                "content": _pack(
                    "project_series", suppressions=[suppression]),
            })
        assert status == 400
        assert "suppressions.0" in result["error"]


def test_rules_api_keeps_legal_project_episode_and_shot_scopes(rule_server):
    port = rule_server["port"]
    project_id = rule_server["first_project"]
    episode_id = rule_server["first_episode"]
    project_pack = _pack("project_series", {
        "key": "visual.series_palette",
        "text": "全剧冷青色",
    })
    episode_pack = _pack("episode_temporary", {
        "key": "visual.local_accent",
        "text": "本集第三镜暖红色",
        "applicability": {
            "shot_nos": [3],
            "stages": ["keyframes"],
            "modalities": ["image"],
        },
    })

    assert _json_request(
        port, "POST", f"/api/project/{project_id}/rules", {
            "expected_version": 0, "content": project_pack,
        })[0] == 200
    assert _json_request(
        port, "POST", f"/api/episode/{episode_id}/rules", {
            "project_id": project_id,
            "expected_version": 0,
            "content": episode_pack,
        })[0] == 200

    status, matching = _json_request(
        port, "GET", f"/api/episode/{episode_id}/rule-stack"
        "?shot_no=3&stage=keyframes&modality=image")
    assert status == 200
    assert matching["effective_rules"]["visual.series_palette"] == "全剧冷青色"
    assert matching["effective_rules"]["visual.local_accent"] == "本集第三镜暖红色"

    status, another_shot = _json_request(
        port, "GET", f"/api/episode/{episode_id}/rule-stack"
        "?shot_no=4&stage=keyframes&modality=image")
    assert status == 200
    assert another_shot["effective_rules"]["visual.series_palette"] == "全剧冷青色"
    assert "visual.local_accent" not in another_shot["effective_rules"]


def test_project_rules_api_rejects_episode_bound_suppression(rule_server):
    port = rule_server["port"]
    project_id = rule_server["first_project"]
    episode_id = rule_server["first_episode"]
    status, result = _json_request(
        port, "POST", f"/api/project/{project_id}/rules", {
            "expected_version": 0,
            "content": _pack("project_series", suppressions=[{
                "key": "visual.default_city",
                "project_id": project_id,
                "episode_id": episode_id,
            }]),
        })
    assert status == 409
    assert "content.suppressions.0.episode_id" in result["error"]


def test_episode_rules_are_derived_from_episode_project_and_never_cross_series(
        rule_server):
    port = rule_server["port"]
    project_id = rule_server["first_project"]
    wrong_project = rule_server["second_project"]
    episode_id = rule_server["first_episode"]
    content = _pack("episode_temporary", {
        "key": "shot.ending.mood",
        "category": "creative",
        "text": "本集结尾留白",
        "enabled": True,
    })

    status, mismatch = _json_request(
        port, "POST", f"/api/episode/{episode_id}/rules", {
            "project_id": wrong_project,
            "episode_id": episode_id,
            "expected_version": 0,
            "content": content,
        })
    assert status == 409 and "所属项目不一致" in mismatch["error"]

    nested_mismatch = dict(content)
    nested_mismatch["project_id"] = wrong_project
    status, mismatch = _json_request(
        port, "POST", f"/api/episode/{episode_id}/rules", {
            "project_id": project_id,
            "episode_id": episode_id,
            "expected_version": 0,
            "content": nested_mismatch,
        })
    assert status == 409 and "content.project_id" in mismatch["error"]

    status, saved = _json_request(
        port, "POST", f"/api/episode/{episode_id}/rules", {
            "project_id": project_id,
            "episode_id": episode_id,
            "expected_version": 0,
            "content": content,
        })
    assert status == 200
    assert saved["project_id"] == project_id
    assert saved["episode_id"] == episode_id
    assert saved["version"] == 1

    status, mismatch = _json_request(
        port, "GET",
        f"/api/episode/{episode_id}/rules?project_id={wrong_project}")
    assert status == 409 and "所属项目不一致" in mismatch["error"]
    status, loaded = _json_request(
        port, "GET", f"/api/episode/{episode_id}/rules")
    assert status == 200
    assert loaded["content"] == content

    app = App(rule_server["workspace"])
    try:
        row = app.db.query_one(
            "SELECT kind, version FROM documents WHERE episode_id=?",
            (episode_id,))
        assert dict(row) == {"kind": "episode_rules", "version": 1}
        assert app.projects.latest_document(
            rule_server["second_episode"], "episode_rules") == (None, 0)
    finally:
        app.close()


def test_rule_stack_reuses_director_resolver_and_filters_by_shot(rule_server):
    port = rule_server["port"]
    project_id = rule_server["first_project"]
    wrong_project = rule_server["second_project"]
    episode_id = rule_server["first_episode"]
    project_pack = _pack("project_series", {
        "key": "visual.palette",
        "category": "creative",
        "text": "冷青色",
        "enabled": True,
    })
    episode_pack = _pack("episode_temporary", {
        "key": "visual.palette",
        "category": "creative",
        "value": "暖红色",
        "enabled": True,
        "applicability": {"shot_nos": [3]},
    }, {
        "key": "camera.motion",
        "category": "creative",
        "text": "缓慢推进",
        "enabled": True,
        "applicability": {"modalities": ["image"]},
    })
    assert _json_request(
        port, "POST", f"/api/project/{project_id}/rules", {
            "expected_version": 0, "content": project_pack,
        })[0] == 200
    assert _json_request(
        port, "POST", f"/api/episode/{episode_id}/rules", {
            "project_id": project_id,
            "expected_version": 0,
            "content": episode_pack,
        })[0] == 200

    status, shot_three = _json_request(
        port, "GET",
        f"/api/episode/{episode_id}/rule-stack?shot_no=3&modality=image")
    assert status == 200
    assert shot_three["effective_rules"]["visual.palette"] == "暖红色"
    assert shot_three["effective_rules"]["camera.motion"] == "缓慢推进"
    assert shot_three["effective_rules"]["technical.no_burned_subtitles"] is True
    assert shot_three["project_rule_version"] == 1
    assert shot_three["episode_rule_version"] == 1
    assert shot_three["suppressed"] == shot_three["overridden"]
    assert shot_three["fingerprint"].startswith("sha256:")

    status, shot_four = _json_request(
        port, "GET",
        f"/api/episode/{episode_id}/rule-stack?shot_no=4&modality=video")
    assert status == 200
    assert shot_four["effective_rules"]["visual.palette"] == "冷青色"
    assert "camera.motion" not in shot_four["effective_rules"]

    status, missing = _json_request(
        port, "GET", f"/api/episode/{episode_id}/rule-stack")
    assert status == 400 and "shot_no" in missing["error"]
    status, mismatch = _json_request(
        port, "GET", f"/api/episode/{episode_id}/rule-stack"
        f"?shot_no=3&project_id={wrong_project}")
    assert status == 409 and "所属项目不一致" in mismatch["error"]
