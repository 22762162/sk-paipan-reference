"""成品包导出:一集的全部成品打成 zip,自行下载分发。

内容:成片/镜头视频、封面、标题文案+话题标签、镜头画面、五维分镜、
连续性圣经、生产门禁、图文检查板、内容复核与实际运行的交付检查脚本。
"""

import io
import json
import zipfile
from pathlib import Path

from .errors import AifosError
from .script_import import script_to_text


def _latest_assets(app, project_id, kind, prefix=None):
    sql = "SELECT * FROM assets WHERE project_id=? AND kind=?"
    params = [project_id, kind]
    if prefix:
        sql += " AND name LIKE ?"
        params.append(prefix + "%")
    latest = {}
    for row in app.db.query(sql + " ORDER BY name, version", tuple(params)):
        latest[row["name"]] = row
    return latest.values()


def build_export_zip(app, project_title, episode_number):
    """返回 (zip_bytes, 文件名)。"""
    project = app.projects.get_project(project_title)
    if project is None:
        raise AifosError(f"项目不存在: {project_title}")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=?",
        (project["id"], episode_number))
    if episode is None:
        raise AifosError(f"剧集不存在: 第{episode_number}集")
    prefix = f"e{episode_number:03d}"
    out_root = (app.workspace.artifacts_dir
                / f"p{project['id']:03d}" / f"e{episode_number:03d}")

    buf = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as bundle:
        def add_file(uri, arcname):
            nonlocal added
            if not uri or uri.startswith(("http://", "https://")):
                return
            path = Path(uri)
            if path.exists():
                bundle.write(path, arcname)
                added += 1

        def add_text(arcname, text):
            nonlocal added
            bundle.writestr(arcname, text)
            added += 1

        # 剧本与发布文案
        script, _ = app.projects.latest_document(episode["id"], "script")
        if script:
            add_text("剧本.txt", script_to_text(script))
        for kind, filename in (
                ("production_standard", "制作合同/本集制作标准.json"),
                ("continuity", "制作合同/连续性圣经.json"),
                ("storyboard", "制作合同/五维分镜.json"),
                ("text_assets", "制作合同/文字资产白名单.json"),
                ("preflight", "制作合同/生产门禁.json"),
                ("content_review", "质检/逐段内容复核.json")):
            document, _ = app.projects.latest_document(episode["id"], kind)
            if document is not None:
                add_text(filename, json.dumps(
                    document, ensure_ascii=False, indent=2))
        kit_path = out_root / "publish" / "publish.json"
        if kit_path.exists():
            kit = json.loads(kit_path.read_text(encoding="utf-8"))
            add_text("发布信息.json",
                     json.dumps(kit, ensure_ascii=False, indent=2))
            add_text("标题与话题.txt", "\n".join(
                ["【候选标题】", *kit.get("titles", []), "",
                 "【话题标签】", " ".join(kit.get("hashtags", [])), "",
                 "【建议】", kit.get("publish_hint", "")]))

        # 媒体成品(取每个资产的最新版本)
        groups = [
            ("video", "视频"), ("voice", "配音"), ("image", "画面"),
            ("first_frame", "首尾帧"), ("last_frame", "首尾帧"),
            ("clip", "拆条"),
        ]
        for kind, folder in groups:
            for row in _latest_assets(app, project["id"], kind, prefix):
                uri = row["uri"]
                add_file(uri, f"{folder}/{Path(uri).name}")
        cover = next(iter(_latest_assets(
            app, project["id"], "cover", prefix)), None)
        if cover:
            add_file(cover["uri"], f"封面{Path(cover['uri']).suffix}")
        final = next(iter(_latest_assets(
            app, project["id"], "edit", prefix)), None)
        if final:
            add_file(final["uri"], f"成片{Path(final['uri']).suffix}")
        draft = out_root / "edit" / "draft_content.json"
        add_file(str(draft), "剪映草稿.json")
        for source, target in (
                (out_root / "review_board.html", "质检/图文检查板.html"),
                (out_root / "qc_report.json", "质检/三层质检报告.json"),
                (out_root / "delivery_check.json", "质检/交付复核结果.json"),
                (out_root / "check-delivery.py", "质检/check-delivery.py")):
            add_file(str(source), target)

        # 人物与场景设定图(项目级)
        for kind, folder in (("character_art", "人物设定"),
                             ("scene_art", "场景设定")):
            for row in _latest_assets(app, project["id"], kind):
                safe = "".join(c if c.isalnum() else "_"
                               for c in row["name"])[:40]
                add_file(row["uri"],
                         f"{folder}/{safe}{Path(row['uri']).suffix}")

    if added == 0:
        raise AifosError("本集尚无可导出的成品")
    filename = f"{project_title}_第{episode_number}集_成品包.zip"
    return buf.getvalue(), filename
