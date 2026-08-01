"""制作标准与单集生产生命周期的集成契约。"""

import copy

from aifos.app import App


def _episode(app, title, number):
    project = app.projects.get_project(title)
    return app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=?",
        (project["id"], number))


def test_episode_snapshot_survives_confirm_and_force_rebinds(tmp_path):
    """确认/续产不漂移；只有 force 才把既有集绑定到当前生效标准。"""
    app = App(tmp_path / "ws")
    try:
        original = app.standards.active()
        paused = app.director.produce(
            "标准快照测试", 1, premise="直播前后台危机",
            pause_for_confirm=True)
        assert paused["status"] == "awaiting_script"
        paused = app.director.produce(
            "标准快照测试", 1, pause_for_confirm=True)  # 剧本确认
        assert paused["status"] == "awaiting_cast"
        project = app.projects.get_project("标准快照测试")
        episode = _episode(app, "标准快照测试", 1)
        script, _ = app.projects.latest_document(episode["id"], "script")
        for character in script["characters"]:
            app.director.select_character_candidate(
                "标准快照测试", 1, character["name"], 1)
        paused = app.director.produce(
            "标准快照测试", 1, pause_for_confirm=True)
        assert paused["status"] == "awaiting_confirm"
        assert paused["production_standard"]["version_id"] == original["version_id"]

        bound, bound_document_version = app.projects.latest_document(
            episode["id"], "production_standard")
        storyboard, _ = app.projects.latest_document(
            episode["id"], "storyboard")
        assert bound_document_version == 1
        assert bound["fingerprint"] == original["fingerprint"]
        assert storyboard["standard_fingerprint"] == original["fingerprint"]

        changed_content = copy.deepcopy(original["content"])
        changed_content["name"] = "高密度短台词标准"
        changed_content["rules"]["dialogue"]["max_chars_per_shot"] = 20
        changed = app.standards.save(
            changed_content, change_note="单镜台词改为最多 20 字")
        assert changed["active"] is True
        assert changed["fingerprint"] != original["fingerprint"]

        # 用户确认以及普通断点续产均恢复本集首次快照，不读取刚切换的厂标。
        confirmed = app.director.produce("标准快照测试", 1)
        assert confirmed["status"] == "done"
        assert confirmed["production_standard"]["version_id"] == original[
            "version_id"]
        still_bound, still_bound_version = app.projects.latest_document(
            episode["id"], "production_standard")
        assert still_bound_version == 1
        assert still_bound["fingerprint"] == original["fingerprint"]

        resumed = app.director.produce("标准快照测试", 1)
        assert resumed["production_standard"]["version_id"] == original[
            "version_id"]

        # force 表示整集重制，并显式重新绑定当前生效标准。
        forced = app.director.produce("标准快照测试", 1, force=True)
        # 全新重制不复用旧人物图，先停在新一轮定角；锁定新候选后续跑。
        assert forced["status"] == "awaiting_cast"
        rebound_script, _ = app.projects.latest_document(
            episode["id"], "script")
        for character in rebound_script["characters"]:
            app.director.select_character_candidate(
                "标准快照测试", 1, character["name"], 1)
        forced = app.director.produce("标准快照测试", 1)
        assert forced["status"] == "done"
        assert forced["production_standard"]["version_id"] == changed[
            "version_id"]
        rebound, rebound_document_version = app.projects.latest_document(
            episode["id"], "production_standard")
        assert rebound_document_version == 2
        assert rebound["fingerprint"] == changed["fingerprint"]
        regenerated_storyboard, _ = app.projects.latest_document(
            episode["id"], "storyboard")
        assert regenerated_storyboard["standard_fingerprint"] == changed[
            "fingerprint"]
        assert regenerated_storyboard["profile"]["max_dialogue_chars"] == 20
    finally:
        app.close()
