"""把正式 AIFOS V3.2 控制台同步为可独立试玩的单文件演示页。

正式 CSS、应用 JS 和顶栏来自 ``aifos/web/static``；Demo 运行时与种子数据
保留在 ``docs/index.html``。同步区域均使用稳定标记，结构漂移时直接失败，避免
静默产出半同步页面。
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aifos.app import App
from aifos.standard_center import DEFAULT_STANDARD
from aifos.web.server import _episode_payload


DOC = ROOT / "docs" / "index.html"
CSS = ROOT / "aifos" / "web" / "static" / "style.css"
APP = ROOT / "aifos" / "web" / "static" / "app.js"
SHELL = ROOT / "aifos" / "web" / "static" / "index.html"

STYLE_START = "/* AIFOS_DEMO_STYLE_START */"
STYLE_END = "/* AIFOS_DEMO_STYLE_END */"
SHELL_START = "<!-- AIFOS_DEMO_SHELL_START -->"
SHELL_END = "<!-- AIFOS_DEMO_SHELL_END -->"
SEED_START = "<!-- AIFOS_DEMO_SEED_START -->"
SEED_END = "<!-- AIFOS_DEMO_SEED_END -->"
APP_START = "<!-- AIFOS_DEMO_APP_START -->"
APP_END = "<!-- AIFOS_DEMO_APP_END -->"

DEMO_CSS = """

.demo-note {
  background: var(--surface-2); border-bottom: 1px solid var(--hairline);
  color: var(--ink-2); font-size: 12.5px; padding: 7px 20px;
}
.demo-note b { color: var(--accent); }
.demo-note a { margin-left: 10px; }
"""

MIME = {
    ".svg": "image/svg+xml", ".json": "application/json",
    ".html": "text/html", ".mp4": "video/mp4", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".wav": "audio/wav",
}
TEXT_ARTIFACTS = {".svg", ".json", ".html", ".txt", ".md"}


def _canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _default_fingerprint():
    return hashlib.sha256(
        _canonical_json(DEFAULT_STANDARD).encode("utf-8")).hexdigest()


def _replace_between(text, start, end, content):
    """替换唯一标记区；缺失或重复均报错。"""
    if text.count(start) != 1 or text.count(end) != 1:
        raise RuntimeError(
            f"同步标记异常: {start!r}={text.count(start)}, "
            f"{end!r}={text.count(end)}")
    left = text.index(start) + len(start)
    right = text.index(end, left)
    if right < left:
        raise RuntimeError(f"同步标记顺序错误: {start!r} / {end!r}")
    return text[:left] + "\n" + content.rstrip() + "\n" + text[right:]


def _extract_once(text, start, end):
    if text.count(start) != 1:
        raise RuntimeError(f"正式页面结构异常，找不到唯一 {start!r}")
    left = text.index(start)
    right = text.index(end, left) + len(end)
    return text[left:right]


def _data_uri(path, workspace):
    path = Path(str(path).split("?", 1)[0])
    if str(path).startswith("/artifacts/"):
        path = workspace / "artifacts" / str(path)[len("/artifacts/"):]
    if not path.is_absolute() or not path.exists():
        return None
    try:
        path.relative_to(workspace / "artifacts")
    except ValueError:
        return None
    data = path.read_bytes()
    if path.suffix.lower() in TEXT_ARTIFACTS:
        replacements = {
            ("file://" + str(workspace)).encode(): b"/demo-workspace",
            str(workspace).encode(): b"/demo-workspace",
            ("file://" + str(workspace.parent)).encode(): b"/demo-root",
            str(workspace.parent).encode(): b"/demo-root",
        }
        for source, target in replacements.items():
            data = data.replace(source, target)
    mime = MIME.get(path.suffix.lower(), "application/octet-stream")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _embed(value, workspace):
    if isinstance(value, dict):
        return {key: _embed(item, workspace) for key, item in value.items()}
    if isinstance(value, list):
        return [_embed(item, workspace) for item in value]
    if isinstance(value, str):
        embedded = _data_uri(value, workspace)
        if embedded:
            return embedded
        return value.replace(str(workspace), "/demo-workspace").replace(
            str(workspace.parent), "/demo-root")
    return value


def _stable_times(value):
    if isinstance(value, dict):
        return {
            key: (1784558725.0 if isinstance(key, str)
                  and key.endswith("_at") else _stable_times(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_stable_times(item) for item in value]
    return value


def _standard_seed():
    snapshot = {
        "version_id": 1,
        "profile_key": DEFAULT_STANDARD["profile_key"],
        "version": 1,
        "name": DEFAULT_STANDARD["name"],
        "description": DEFAULT_STANDARD["description"],
        "content": copy.deepcopy(DEFAULT_STANDARD),
        "change_note": "初始化 SK 漫剧五维分镜 V5 标准",
        "fingerprint": _default_fingerprint(),
        "created_at": 1784558725.0,
        "active": True,
    }
    return {"active_version_id": 1, "history": [snapshot]}


def _fresh_seed():
    with tempfile.TemporaryDirectory(prefix="aifos-docs-seed-") as temp:
        workspace = Path(temp) / "workspace"
        app = App(workspace)
        try:
            app.director.produce(
                "小澜同学", 1, premise="新歌翻唱", kind="idol")
            app.director.produce("万妖图录", 15, kind="drama")
            projects = [dict(row) for row in app.projects.list_projects()]
            episodes = [dict(row) for row in app.db.query(
                "SELECT * FROM episodes ORDER BY id")]
            payloads = {
                str(row["id"]): _embed(
                    _episode_payload(app, row["id"]), workspace)
                for row in episodes
            }
            for payload in payloads.values():
                if payload.get("qc_report"):
                    payload["qc_report"]["review_board"] = (
                        payload["artifacts"].get("review_board"))
            logs = [dict(row) for row in app.db.query(
                "SELECT level, source, message FROM logs "
                "ORDER BY id DESC LIMIT 30")]
            result = _stable_times({
                "projects": projects, "episodes": episodes,
                "payloads": payloads, "fullResults": {}, "logs": logs,
                "standards": _standard_seed(),
            })
            serialized = json.dumps(result, ensure_ascii=False)
            forbidden = [str(workspace), str(workspace.parent),
                         "file:///private/var", "aifos-docs-seed-"]
            leaked = [item for item in forbidden if item in serialized]
            if leaked:
                raise RuntimeError(
                    "演示种子仍包含临时路径: " + ", ".join(leaked))
            return result
        finally:
            app.close()


def _current_seed(document):
    prefix = "window.__SEED__ = "
    if document.count(prefix) != 1:
        raise RuntimeError("演示种子赋值缺失或重复")
    left = document.index(prefix) + len(prefix)
    right = document.index(";\n", left)
    try:
        return json.loads(document[left:right])
    except json.JSONDecodeError as exc:
        raise RuntimeError("现有演示种子不是合法 JSON") from exc


def _formal_header():
    shell = SHELL.read_text(encoding="utf-8")
    return _extract_once(shell, '<header id="topbar">', "</header>")


def _validate_existing_regions(document):
    pairs = [
        (STYLE_START, STYLE_END), (SHELL_START, SHELL_END),
        (SEED_START, SEED_END), (APP_START, APP_END),
    ]
    marked = any(marker in document for pair in pairs for marker in pair)
    if not marked:
        # 只允许尚未升级的 V3.1 旧模板没有标记。
        if "AIFOS V3.2" in document:
            raise RuntimeError("V3.2 演示页缺少稳定同步标记")
        return
    for start, end in pairs:
        if document.count(start) != 1 or document.count(end) != 1:
            raise RuntimeError(
                f"同步标记异常: {start!r}={document.count(start)}, "
                f"{end!r}={document.count(end)}")
        if document.index(start) >= document.index(end):
            raise RuntimeError(f"同步标记顺序错误: {start!r} / {end!r}")


def _prefix(css):
    header = _formal_header()
    note = (
        '<div class="demo-note"><b>V3.2 可试玩演示</b> · '
        '先到「制作标准」查看或调整 SK 五维分镜、人物连续性、节奏、声音与质检门禁；'
        '保存后再开新集，系统会给该集绑定当时的标准版本与指纹。'
        '默认由 Seedance2 在视频内同步生成人声与口型，交付无字幕母版；'
        '豆包 TTS 只作为无声视频兼容模式的备选。'
        '数据只存在本浏览器<a href="javascript:void(0)" id="demo-reset">重置演示数据</a></div>')
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIFOS V3.2 · AI 漫剧生产工作台演示</title>
<style>
{STYLE_START}
{css.rstrip()}
{STYLE_END}
</style>
</head>
<body>
{SHELL_START}
{header}
{note}
<main id="app"><div class="loading">加载中…</div></main>
{SHELL_END}
'''


def _seed_script(seed):
    seed_json = json.dumps(
        seed, ensure_ascii=False, separators=(",", ":"))
    standard_json = json.dumps(
        DEFAULT_STANDARD, ensure_ascii=False, separators=(",", ":"))
    return f'''{SEED_START}
<script>
window.__SEED__ = {seed_json};
window.__DEFAULT_STANDARD__ = {standard_json};
window.__DEFAULT_STANDARD_FINGERPRINT__ = {_default_fingerprint()!r};
</script>
{SEED_END}'''


def _ensure_app_region(document):
    app_source = APP.read_text(encoding="utf-8").rstrip()
    if APP_START in document or APP_END in document:
        return _replace_between(
            document, APP_START, APP_END,
            f"<script>\n{app_source}\n</script>")
    marker = "/* AIFOS 控制台单页应用:"
    if document.count(marker) != 1:
        raise RuntimeError("找不到唯一的正式应用脚本标记")
    script_start = document.rfind("<script>", 0, document.index(marker))
    script_end = document.index("</script>", document.index(marker)) + len("</script>")
    return (document[:script_start] + APP_START + "\n<script>\n" +
            app_source + "\n</script>\n" + APP_END + document[script_end:])


def main(refresh_seed=False):
    document = DOC.read_text(encoding="utf-8")
    _validate_existing_regions(document)
    seed = _fresh_seed() if refresh_seed else _current_seed(document)
    seed.setdefault("standards", _standard_seed())

    # 首次升级时用第一个种子脚本作为稳定边界重建 HTML shell；之后依靠标记。
    first_seed_script = document.index("<script>\nwindow.__SEED__ = ")
    bootstrap_start = document.index("<script>", first_seed_script + 8)
    bootstrap_and_app = document[bootstrap_start:]
    css = CSS.read_text(encoding="utf-8").rstrip() + DEMO_CSS
    document = _prefix(css) + _seed_script(seed) + "\n" + bootstrap_and_app
    document = _ensure_app_region(document)

    # 页面闭合只补一次；浏览器虽能容错，完整结构更利于静态检查。
    stripped = document.rstrip()
    if not stripped.endswith("</html>"):
        document = stripped + "\n</body>\n</html>\n"
    else:
        document = stripped + "\n"
    DOC.write_text(document, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--refresh-seed", action="store_true",
        help="用当前离线 Mock 产线重建无临时路径的 V3.2 演示素材")
    main(parser.parse_args().refresh_seed)
