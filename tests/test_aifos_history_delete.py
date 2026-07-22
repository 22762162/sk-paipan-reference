"""生产历史删除：保留项目/文件，资产是否软删由用户明确选择。"""

from aifos.app import App


def _image(app, project_id, kind, name, filename, meta=None):
    path = app.workspace.artifacts_dir / "delete-tests" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    row = app.assets.register(
        project_id, kind, name, uri=str(path), meta=meta or {})
    return row, path


def test_delete_work_keeps_asset_center_images_by_default(tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("保留资产作品")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        app.projects.save_document(episode["id"], "script", {"scenes": []})
        run_id = app.history.create_run("保留资产作品", 1)
        app.db.execute(
            "INSERT INTO tasks(episode_id, run_id, stage, name, status, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (episode["id"], run_id, "script", "剧本", "done", 1, 1))
        asset, path = _image(
            app, project["id"], "image", "e001_shot001", "keep.png",
            {"shot_no": 1, "image_quality": "medium"})

        result = app.history.delete_work(run_id, delete_assets=False)

        assert result["episode_deleted"] is True
        assert result["runs_deleted"] == 1
        assert result["assets_soft_deleted"] == 0
        assert app.projects.get_episode(episode["id"]) is None
        assert app.history.get(run_id) is None
        assert app.assets.get(asset["id"])["uri"] == str(path)
        assert app.assets.latest(
            project["id"], "image", "e001_shot001") is not None
        assert path.exists()
        assert app.projects.get_project("保留资产作品") is not None
    finally:
        app.close()


def test_delete_episode_work_supports_overview_items_without_history(tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("总览直接删除")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        app.projects.save_document(episode["id"], "script", {"scenes": []})
        asset, path = _image(
            app, project["id"], "image", "e001_shot001", "overview.png")

        result = app.history.delete_episode_work(
            episode["id"], delete_assets=False)

        assert result["episode_deleted"] is True
        assert result["runs_deleted"] == 0
        assert result["documents_deleted"] == 1
        assert app.projects.get_episode(episode["id"]) is None
        assert app.assets.get(asset["id"])["uri"] == str(path)
        assert path.exists()
        assert app.projects.get_project("总览直接删除") is not None
    finally:
        app.close()


def test_delete_one_episode_only_soft_deletes_its_scoped_images(tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("多集共享资产")
        first, _ = app.projects.get_or_create_episode(project["id"], 1)
        second, _ = app.projects.get_or_create_episode(project["id"], 2)
        run_id = app.history.create_run("多集共享资产", 1)
        shared, shared_path = _image(
            app, project["id"], "character_art", "林昭", "shared.png")
        first_image, first_path = _image(
            app, project["id"], "image", "e001_shot001", "first.png",
            {"shot_no": 1})
        second_image, _ = _image(
            app, project["id"], "image", "e002_shot001", "second.png",
            {"shot_no": 1})

        result = app.history.delete_work(run_id, delete_assets=True)

        assert result["assets_soft_deleted"] == 1
        assert app.projects.get_episode(first["id"]) is None
        assert app.projects.get_episode(second["id"]) is not None
        assert app.assets.latest(
            project["id"], first_image["kind"], first_image["name"]) is None
        tombstone = app.assets.latest(
            project["id"], first_image["kind"], first_image["name"],
            include_deleted=True)
        assert app.assets.meta(tombstone)["deleted_episode_number"] == 1
        assert app.assets.latest(
            project["id"], shared["kind"], shared["name"])["id"] == shared["id"]
        assert app.assets.latest(
            project["id"], second_image["kind"], second_image["name"])["id"] == second_image["id"]
        assert first_path.exists() and shared_path.exists()
    finally:
        app.close()


def test_delete_single_episode_can_soft_delete_all_visible_project_images(tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("单集全删图片")
        app.projects.get_or_create_episode(project["id"], 1)
        run_id = app.history.create_run("单集全删图片", 1)
        portrait, portrait_path = _image(
            app, project["id"], "character_art", "林昭", "portrait.png")
        reference, reference_path = _image(
            app, project["id"], "reference", "造型参考", "reference.png")

        result = app.history.delete_work(run_id, delete_assets=True)

        assert result["assets_soft_deleted"] == 2
        assert app.assets.latest(
            project["id"], portrait["kind"], portrait["name"]) is None
        assert app.assets.latest(
            project["id"], reference["kind"], reference["name"]) is None
        assert portrait_path.exists() and reference_path.exists()
    finally:
        app.close()
