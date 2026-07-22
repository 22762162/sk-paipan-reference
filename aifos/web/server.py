"""AIFOS Web 服务:http.server 实现的 JSON API + 静态页面 + 产物文件服务。

启动:python3 -m aifos serve [--host 127.0.0.1] [--port 8619]

API:
  GET  /api/overview            全局看板(项目/剧集/成本/额度/任务)
  GET  /api/episode/<id>        单集详情(阶段/剧本/分镜/质检/产物索引)
  GET  /api/assets?project=T    项目资产列表
  GET  /api/logs?limit=N        最近日志
  GET  /api/jobs  /api/jobs/<id>后台制作任务
  GET  /api/history             持久生产历史(跨重启)
  GET  /api/history/<id>        单次生产详情与阶段记录
  GET  /api/standards           当前制作标准 + 版本历史
  GET  /api/standards/export    导出不含密钥的制作标准包
  POST /api/produce             {"sentence": "开始制作《万妖图录》第15集"}
  POST /api/standards/save|activate|reset|import
静态:
  GET  /                        控制台单页应用
  GET  /artifacts/<path>        workspace/artifacts 下的产物(防目录穿越)
"""

import copy
import ipaddress
import json
import shutil
import subprocess
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__
from ..app import App
from ..updater import (check_and_update, current_build, repo_root,
                       restart_process, start_auto_updater)
from ..errors import AifosError
from ..quality_policy import normalize_quality, normalize_quality_policy
from ..smart_input import resolve_produce_target
from ..standard_center import StandardConflictError, StandardValidationError

STATIC_DIR = Path(__file__).parent / "static"

# 当前代码版本(git 短哈希):前端据此发现服务已自动更新并自动刷新页面
BUILD = current_build()

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".jsonl": "application/x-ndjson; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}


def _private_lan_addresses():
    """返回可供同一局域网手机访问的本机 IPv4 地址。"""
    addresses = set()
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        addresses.update(info[4][0] for info in infos)
    except OSError:
        pass
    # UDP connect 不发送数据，但能可靠取到当前默认网卡地址。
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    result = []
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.version == 4 and ip.is_private and not ip.is_loopback:
            result.append(address)
    return sorted(set(result))


def access_payload(bound_host, port, workspace=None):
    """构造桌面与手机端都能理解的访问/安装信息。"""
    lan_enabled = bound_host in ("0.0.0.0", "::", "")
    hostname = socket.gethostname().split(".")[0]
    lan_urls = ([f"http://{address}:{port}/"
                 for address in _private_lan_addresses()]
                if lan_enabled else [])
    hostname_url = (f"http://{hostname}.local:{port}/"
                    if lan_enabled and hostname else None)
    # 外网地址(cloudflared 隧道):`aifos tunnel` 起隧道后写入 workspace,
    # 网页据此显示二维码,手机扫码即得最新地址(免去手动粘贴/猜网址)
    public = None
    if workspace is not None:
        from ..app import Workspace
        from .. import tunnel
        public = tunnel.read_public_url(Workspace(workspace).root)
    return {
        "lan_enabled": lan_enabled,
        "local_url": f"http://127.0.0.1:{port}/",
        "lan_urls": lan_urls,
        "hostname_url": hostname_url,
        "same_wifi_required": True,
        "public_url": public["url"] if public else None,
        "public": public,
        "install": {
            "ios": "用 Safari 打开后点分享，再选‘添加到主屏幕’",
            "android": "用 Chrome 打开后点菜单，再选‘安装应用’或‘添加到主屏幕’",
        },
    }


class JobRegistry:
    """produce 后台任务:制作可能耗时(真实产线更久),Web 端异步执行。"""

    def __init__(self, workspace):
        self.workspace = workspace
        self._jobs = {}
        self._lock = threading.Lock()
        self._seq = 0
        app = App(self.workspace)
        try:
            app.history.bootstrap()
        finally:
            app.close()

    def _create_history(self, title, number, action, force=False,
                        request=None):
        app = App(self.workspace)
        try:
            return app.history.create_run(
                title, number, action=action, force=force,
                request=request, source="web")
        finally:
            app.close()

    def start(self, title, number, premise="", style="", force=False,
              script=None, review=False, kind=None, action="produce"):
        run_id = self._create_history(
            title, number, action, force=force,
            request={"premise": premise, "style": style,
                     "review": bool(review), "kind": kind,
                     "script_supplied": script is not None})
        with self._lock:
            self._seq += 1
            job_id = f"j{self._seq}"
            self._jobs[job_id] = {
                "id": job_id, "status": "running",
                "title": title, "episode": number, "force": force,
                "started_at": time.time(), "run_id": run_id,
            }

        def task(app):
            return app.director.produce(
                title, number, premise=premise, style=style, force=force,
                script=script, pause_for_confirm=review, kind=kind,
                run_id=run_id)

        self._run(job_id, task)
        return job_id

    def start_task(self, title, number, task, action="adjustment",
                   request=None, tracked=False):
        """通用后台任务(打磨重写/重画)。

        普通任务签名为 ``task(app, run_id)``；tracked=True 时额外传入
        ``report(**fields)``，让长批次把逐项进度写进 job，前端无需猜测。
        """
        run_id = self._create_history(
            title, number, action, request=request)
        with self._lock:
            self._seq += 1
            job_id = f"j{self._seq}"
            self._jobs[job_id] = {
                "id": job_id, "status": "running",
                "title": title, "episode": number,
                "started_at": time.time(), "run_id": run_id,
            }
        def report(**fields):
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                progress = dict(job.get("progress") or {})
                progress.update(fields)
                progress["updated_at"] = time.time()
                job["progress"] = progress

        runner = ((lambda app: task(app, run_id, report))
                  if tracked else (lambda app: task(app, run_id)))
        self._run(job_id, runner)
        return job_id

    def _run(self, job_id, task):
        def run():
            app = App(self.workspace)
            try:
                summary = task(app)
                self._jobs[job_id].update(
                    status="done", summary=summary, finished_at=time.time())
                app.history.finish_run(
                    self._jobs[job_id]["run_id"], summary=summary)
            except Exception as exc:  # 后台任务兜底,错误进任务状态
                self._jobs[job_id].update(
                    status="failed", error=str(exc), finished_at=time.time())
                app.history.finish_run(
                    self._jobs[job_id]["run_id"], error=str(exc))
            finally:
                app.close()

        threading.Thread(target=run, daemon=True).start()

    def get(self, job_id):
        return self._jobs.get(job_id)

    def list(self):
        return sorted(self._jobs.values(),
                      key=lambda j: j["started_at"], reverse=True)

    def running_for(self, title, number):
        with self._lock:
            return [job for job in self._jobs.values()
                    if job["status"] == "running"
                    and job["title"] == title
                    and int(job["episode"]) == int(number)]


def _versioned(url, row):
    """重画同名文件后靠版本参数破除浏览器缓存。"""
    if url and url.startswith("/artifacts/"):
        return f"{url}?v={row['version']}"
    return url


def _artifact_url(app, uri):
    """文件系统路径 → /artifacts/ 相对 URL;远程 URL 原样;其余 None。"""
    if not uri:
        return None
    if uri.startswith("http://") or uri.startswith("https://"):
        return uri
    try:
        rel = Path(uri).resolve().relative_to(
            app.workspace.artifacts_dir.resolve())
    except ValueError:
        return None
    return "/artifacts/" + rel.as_posix()


def _image_asset_catalog(app, project_id):
    """资产中心当前可见图片；携带稳定 asset_id 供删除和视频选图。"""
    kinds = {
        "character_art", "character_sheet", "scene_art",
        "image", "first_frame", "last_frame", "cover", "reference",
    }
    labels = {
        "character_art": "人物立绘", "character_sheet": "人物设定",
        "scene_art": "场景概念图", "image": "镜头关键图",
        "first_frame": "首帧", "last_frame": "尾帧",
        "cover": "封面", "reference": "上传参考图",
    }
    items = []
    for row in app.assets.active_list(project_id):
        if row["kind"] not in kinds:
            continue
        url = _versioned(_artifact_url(app, row["uri"]), row)
        if not url:
            continue
        meta = json.loads(row["meta"] or "{}")
        quality = meta.get("image_quality", "medium")
        items.append({
            "asset_id": row["id"], "kind": row["kind"],
            "name": row["name"], "version": row["version"],
            "label": f"{labels[row['kind']]} · {row['name']}",
            "url": url, "quality": quality,
            "usable_for_video": quality != "low",
            "meta": meta,
        })
    items.sort(key=lambda item: (item["kind"], item["name"]))
    return items


def _collect_artifacts(app, project_id, ep_num):
    """从资产表重建单集产物索引(按镜头/台词编号)。"""
    prefix = f"e{ep_num:03d}"
    rows = [row for row in app.assets.active_list(project_id)
            if row["name"].startswith(prefix)]
    shot_re = re.compile(rf"^{prefix}_shot(\d+)$")
    line_re = re.compile(rf"^{prefix}_line(\d+)$")
    clip_re = re.compile(rf"^{prefix}_scene(\d+)$")
    out = {"images": {}, "first": {}, "last": {}, "videos": {},
           "video_audio": {}, "video_providers": {}, "voices": {},
           "cover": None, "final": None,
           "titles": [], "clips": [], "review_board": None}
    kind_map = {"image": "images", "first_frame": "first",
                "last_frame": "last", "video": "videos"}
    for row in rows:
        kind, name = row["kind"], row["name"]
        url = _versioned(_artifact_url(app, row["uri"]), row)
        shot = shot_re.match(name)
        if shot and kind in kind_map:
            shot_no = int(shot.group(1))
            out[kind_map[kind]][shot_no] = url
            if kind == "video":
                meta = json.loads(row["meta"] or "{}")
                out["video_audio"][shot_no] = bool(
                    meta.get("audio_in_video"))
                out["video_providers"][shot_no] = meta.get("provider", "")
            continue
        line = line_re.match(name)
        if line and kind == "voice":
            out["voices"][int(line.group(1))] = url
            continue
        if kind == "cover" and name == prefix:
            out["cover"] = url
        elif kind == "edit" and name == f"{prefix}_final":
            out["final"] = url
        elif kind == "review_board" and name == prefix:
            out["review_board"] = url
        elif kind == "title" and name == prefix:
            out["titles"] = json.loads(row["meta"]).get("candidates", [])
        elif kind == "clip":
            clip = clip_re.match(name)
            if clip:
                out["clips"].append(
                    {"scene_no": int(clip.group(1)), "url": url})
    out["clips"].sort(key=lambda c: c["scene_no"])
    # 人物立绘与场景概念图(项目级资产,跨集复用)
    def latest_rows(kind):
        return app.assets.active_list(project_id, kind=kind)

    designs = {
        row["name"]: json.loads(row["meta"] or "{}").get("design")
        for row in latest_rows("character")}
    out["cast_art"] = [
        {"asset_id": row["id"], "kind": row["kind"], "name": row["name"],
         "url": _versioned(_artifact_url(app, row["uri"]), row),
         "role": json.loads(row["meta"]).get("role", ""),
         "design": designs.get(row["name"])}
        for row in latest_rows("character_art")]
    out["scene_art"] = [
        {"asset_id": row["id"], "kind": row["kind"], "name": row["name"],
         "url": _versioned(_artifact_url(app, row["uri"]), row)}
        for row in latest_rows("scene_art")]
    # 人物资产套件(四视图/特写/特征/妆容/服装/服装细节)按角色分组
    sheets = {}
    for row in latest_rows("character_sheet"):
        meta = json.loads(row["meta"] or "{}")
        sheets.setdefault(meta.get("character", ""), []).append({
            "asset_id": row["id"], "kind": row["kind"],
            "name": row["name"], "sheet": meta.get("sheet", ""),
            "label": meta.get("label", ""),
            "url": _versioned(_artifact_url(app, row["uri"]), row)})
    out["character_sheets"] = sheets
    out["references"] = [
        {"asset_id": row["id"], "kind": row["kind"], "name": row["name"],
         "attach_to": json.loads(row["meta"] or "{}").get("attach_to", ""),
         "note": json.loads(row["meta"] or "{}").get("note", ""),
         "url": _versioned(_artifact_url(app, row["uri"]), row)}
        for row in latest_rows("reference")]
    out["image_assets"] = _image_asset_catalog(app, project_id)
    return out


def _episode_payload(app, episode_id):
    episode = app.projects.get_episode(episode_id)
    if episode is None:
        return None
    project = app.db.query_one(
        "SELECT * FROM projects WHERE id=?", (episode["project_id"],))
    script, script_v = app.projects.latest_document(episode_id, "script")
    storyboard, sb_v = app.projects.latest_document(episode_id, "storyboard")
    continuity, continuity_v = app.projects.latest_document(
        episode_id, "continuity")
    blocking, blocking_v = app.projects.latest_document(
        episode_id, "blocking")
    text_assets, text_assets_v = app.projects.latest_document(
        episode_id, "text_assets")
    preflight, preflight_v = app.projects.latest_document(
        episode_id, "preflight")
    content_review, content_review_v = app.projects.latest_document(
        episode_id, "content_review")
    production_standard, production_standard_v = app.projects.latest_document(
        episode_id, "production_standard")
    quality_policy, quality_policy_v = app.projects.latest_document(
        episode_id, "quality_policy")
    video_references, video_references_v = app.projects.latest_document(
        episode_id, "video_references")
    cast_selection = app.director.character_selection_status(
        project["id"], (script or {}).get("characters", []))
    for character in cast_selection.get("characters", []):
        if character.get("identity_uri"):
            character["identity_url"] = _artifact_url(
                app, character["identity_uri"])
        for candidate in character.get("candidates", []):
            candidate["url"] = _artifact_url(app, candidate.get("uri", ""))
    tasks = [dict(t) for t in app.db.query(
        "SELECT id, stage, name, status, provider, cost, error, created_at, "
        "updated_at FROM tasks WHERE episode_id=? ORDER BY id",
        (episode_id,))]
    out_dir = (app.workspace.artifacts_dir / f"p{project['id']:03d}"
               / f"e{episode['number']:03d}")
    qc_report = None
    qc_path = out_dir / "qc_report.json"
    if qc_path.exists():
        qc_report = json.loads(qc_path.read_text(encoding="utf-8"))
    render_plan = None
    plan_path = out_dir / "render_plan.json"
    if plan_path.exists():
        try:
            render_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except ValueError:
            render_plan = None
    if render_plan is not None:
        render_plan = copy.deepcopy(render_plan)
        for item in render_plan.get("items", []):
            for ref in (item.get("reference_inputs") or {}).get("items", []):
                ref["url"] = _artifact_url(app, ref.get("uri", ""))
    if blocking is not None:
        blocking = copy.deepcopy(blocking)
        for scene in blocking.get("scenes", []):
            scene["svg_url"] = _artifact_url(
                app, scene.get("svg_uri", ""))
    return {
        "build": BUILD,
        "episode": dict(episode),
        "project": dict(project),
        "tasks": tasks,
        "script": script,
        "script_version": script_v,
        "storyboard": storyboard,
        "storyboard_version": sb_v,
        "continuity": continuity,
        "continuity_version": continuity_v,
        "blocking": blocking,
        "blocking_version": blocking_v,
        "text_assets": text_assets,
        "text_assets_version": text_assets_v,
        "preflight": preflight,
        "preflight_version": preflight_v,
        "content_review": content_review,
        "content_review_version": content_review_v,
        "production_profile": (storyboard or {}).get("profile", {}),
        "production_standard": production_standard,
        "production_standard_version": production_standard_v,
        "quality_policy": normalize_quality_policy(quality_policy),
        "quality_policy_version": quality_policy_v,
        "video_references": video_references or {
            "schema": "aifos.video-references/v1", "shots": {}},
        "video_references_version": video_references_v,
        "cast_selection": cast_selection,
        "qc_report": qc_report,
        "render_plan": render_plan,
        "artifacts": _collect_artifacts(
            app, project["id"], episode["number"]),
    }


def _overview_payload(app, jobs):
    episodes = [dict(r) for r in app.db.query(
        "SELECT e.id, e.number, e.title, e.status, e.qc_score, e.cost, "
        "e.updated_at, p.title AS project "
        "FROM episodes e JOIN projects p ON p.id=e.project_id "
        "ORDER BY e.updated_at DESC")]
    done = [e for e in episodes if e["status"] == "done"]
    scored = [e["qc_score"] for e in episodes if e["qc_score"] is not None]
    active_standard = app.standards.active()
    return {
        "version": __version__,
        "build": BUILD,
        "production_standard": {
            key: active_standard.get(key) for key in (
                "version_id", "profile_key", "version", "name",
                "fingerprint", "created_at")
        },
        "projects": [dict(r) for r in app.projects.list_projects()],
        "episodes": episodes,
        "stats": {
            "episodes": len(episodes),
            "done": len(done),
            "total_cost": round(sum(e["cost"] for e in episodes), 2),
            "avg_qc": round(sum(scored) / len(scored), 1) if scored else None,
            "budget": app.config.get("budget", "per_episode", default=0),
        },
        "cost_by_stage": [dict(r) for r in app.system.cost_by_stage()],
        "cost_by_provider": [dict(r) for r in app.system.cost_by_provider()],
        "quota": [dict(r) for r in app.system.quota_status()],
        "asset_stats": {
            p["title"]: [dict(r) for r in app.assets.stats(p["id"])]
            for p in app.projects.list_projects()
        },
        "jobs": jobs.list(),
    }


def make_handler(workspace, jobs):
    workspace = Path(workspace)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # 静默访问日志(平台日志走系统中心)
            pass

        # ---- 响应助手 ----
        def _json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type",
                             "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status, message):
            self._json({"error": message}, status=status)

        def _qr_svg(self, data):
            """把任意文本(通常是外网地址)渲染成 QR 二维码 SVG。"""
            from .. import qrcode
            try:
                matrix = qrcode.encode(data, "M")
            except ValueError as exc:
                return self._error(400, str(exc))
            svg = qrcode.render_svg(matrix).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(svg)))
            self.send_header("Cache-Control", "public, max-age=60")
            self.end_headers()
            self.wfile.write(svg)

        def _file(self, path, no_cache=False, cache_seconds=None):
            path = Path(path)
            if not path.is_file():
                return self._error(404, "文件不存在")
            body = path.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                MIME.get(path.suffix.lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            if no_cache:
                # 界面文件禁缓存:git pull 更新后刷新即生效,
                # 避免浏览器缓存旧版界面导致"更新了却看不到新功能"
                self.send_header("Cache-Control", "no-cache")
            elif cache_seconds:
                self.send_header("Cache-Control",
                                 f"public, max-age={int(cache_seconds)}")
            self.end_headers()
            self.wfile.write(body)

        def _with_app(self, fn):
            app = App(workspace)
            try:
                return fn(app)
            finally:
                app.close()

        # ---- 路由 ----
        def do_GET(self):
            parsed = urlparse(self.path)
            route = unquote(parsed.path)
            query = parse_qs(parsed.query)
            try:
                if route in ("/", "/index.html"):
                    return self._file(STATIC_DIR / "index.html",
                                      no_cache=True)
                if route == "/manifest.webmanifest":
                    return self._file(STATIC_DIR / "manifest.webmanifest",
                                      no_cache=True)
                if route == "/sw.js":
                    return self._file(STATIC_DIR / "sw.js", no_cache=True)
                if route.startswith("/static/"):
                    return self._static(STATIC_DIR, route[len("/static/"):])
                if route.startswith("/artifacts/"):
                    return self._artifact(route[len("/artifacts/"):],
                                          query)
                if route == "/api/access":
                    host, port = self.server.server_address[:2]
                    return self._json(
                        access_payload(host, port, workspace=workspace))
                if route == "/qr.svg":
                    data = query.get("data", [""])[0]
                    if not data:
                        return self._error(400, "缺少 data 参数")
                    return self._qr_svg(data)
                if route == "/api/overview":
                    return self._json(self._with_app(
                        lambda app: _overview_payload(app, jobs)))
                match = re.match(r"^/api/episode/(\d+)$", route)
                if match:
                    payload = self._with_app(
                        lambda app: _episode_payload(
                            app, int(match.group(1))))
                    if payload is None:
                        return self._error(404, "剧集不存在")
                    return self._json(payload)
                if route == "/api/assets":
                    return self._assets(query)
                if route == "/api/logs":
                    limit = int(query.get("limit", ["50"])[0])
                    return self._json(self._with_app(
                        lambda app: [dict(r)
                                     for r in app.logger.tail(limit)]))
                if route == "/api/history":
                    limit = int(query.get("limit", ["200"])[0])
                    status = query.get("status", [None])[0]
                    action = query.get("action", [None])[0]
                    search = query.get("q", [""])[0]
                    return self._json(self._with_app(
                        lambda app: app.history.list(
                            limit=limit, status=status, action=action,
                            query=search)))
                match = re.match(r"^/api/history/(\d+)$", route)
                if match:
                    payload = self._with_app(
                        lambda app: app.history.get(int(match.group(1))))
                    if payload is None:
                        return self._error(404, "历史记录不存在")
                    return self._json(payload)
                if route == "/api/jobs":
                    return self._json(jobs.list())
                if route == "/api/standards":
                    return self._standards()
                if route == "/api/standards/export":
                    version_id = query.get("version_id", [None])[0]
                    return self._standards_export(
                        int(version_id) if version_id else None)
                match = re.match(r"^/api/jobs/(\w+)$", route)
                if match:
                    job = jobs.get(match.group(1))
                    if job is None:
                        return self._error(404, "任务不存在")
                    return self._json(job)
                match = re.match(r"^/api/export/(\d+)$", route)
                if match:
                    return self._export(int(match.group(1)))
                if route == "/api/settings":
                    from ..settings import settings_payload
                    return self._json(self._with_app(settings_payload))
                if route == "/api/doctor":
                    from ..doctor import run_doctor
                    ping = query.get("ping", ["0"])[0] == "1"
                    return self._json(self._with_app(
                        lambda app: run_doctor(app, do_ping=ping)))
                return self._error(404, "未知路径")
            except BrokenPipeError:
                pass
            except Exception as exc:
                self._error(500, str(exc))

        def do_POST(self):
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/produce":
                    return self._produce()
                if parsed.path == "/api/confirm":
                    return self._confirm()
                if parsed.path == "/api/character/select":
                    return self._character_select()
                if parsed.path == "/api/revise":
                    return self._revise()
                if parsed.path == "/api/regen_image":
                    return self._regen_image()
                if parsed.path == "/api/upload":
                    return self._upload()
                if parsed.path == "/api/settings":
                    return self._settings_update()
                if parsed.path == "/api/settings/test":
                    return self._settings_test()
                if parsed.path == "/api/settings/detect":
                    return self._settings_detect()
                if parsed.path == "/api/update":
                    return self._update_now()
                if parsed.path == "/api/redo_mock":
                    return self._redo_mock()
                if parsed.path == "/api/qc_item":
                    return self._qc_item()
                if parsed.path == "/api/qc_all":
                    return self._qc_all()
                if parsed.path == "/api/redo_items":
                    return self._redo_items()
                if parsed.path == "/api/restyle":
                    return self._restyle()
                if parsed.path == "/api/reference/upload":
                    return self._reference_upload()
                if parsed.path == "/api/reference/delete":
                    return self._reference_delete()
                if parsed.path == "/api/asset/delete":
                    return self._asset_delete()
                if parsed.path == "/api/video/references":
                    return self._video_references()
                if parsed.path == "/api/project/style":
                    return self._project_style()
                if parsed.path == "/api/project/rename":
                    return self._project_rename()
                if parsed.path == "/api/stop":
                    return self._stop()
                if parsed.path == "/api/standards/save":
                    return self._standards_save()
                if parsed.path == "/api/standards/activate":
                    return self._standards_activate()
                if parsed.path == "/api/standards/reset":
                    return self._standards_reset()
                if parsed.path == "/api/standards/import":
                    return self._standards_import()
                return self._error(404, "未知路径")
            except BrokenPipeError:
                pass
            except Exception as exc:
                self._error(500, str(exc))

        # ---- 各端点 ----
        def _static(self, root, rel):
            target = (root / rel).resolve()
            if not str(target).startswith(str(root.resolve()) + "/"):
                return self._error(404, "非法路径")
            return self._file(target, no_cache=True)

        THUMB_EXTS = (".png", ".jpg", ".jpeg", ".webp")

        def _thumbnail(self, root, target, width):
            """列表用缩略图:按需生成并缓存(macOS 自带 sips,备选
            ffmpeg;都没有时回退原图)。点开大图仍加载原图。"""
            if target.suffix.lower() not in self.THUMB_EXTS:
                return target
            width = max(64, min(width, 960))
            try:
                rel = target.relative_to(root)
            except ValueError:
                return target
            thumb = root / ".thumbs" / f"w{width}" / rel
            try:
                if thumb.exists() and                         thumb.stat().st_mtime >= target.stat().st_mtime:
                    return thumb
                thumb.parent.mkdir(parents=True, exist_ok=True)
                for command in (
                        ["sips", "-Z", str(width), str(target),
                         "--out", str(thumb)],
                        ["ffmpeg", "-y", "-loglevel", "error",
                         "-i", str(target),
                         "-vf", f"scale={width}:-2", str(thumb)]):
                    if shutil.which(command[0]) is None:
                        continue
                    proc = subprocess.run(command, capture_output=True,
                                          timeout=30)
                    if proc.returncode == 0 and thumb.exists():
                        return thumb
            except (OSError, subprocess.TimeoutExpired):
                pass
            return target

        def _artifact(self, rel, query=None):
            app = App(workspace)
            try:
                root = app.workspace.artifacts_dir.resolve()
            finally:
                app.close()
            target = (root / rel).resolve()
            if not str(target).startswith(str(root) + "/"):
                return self._error(404, "非法路径")
            width = 0
            try:
                width = int((query or {}).get("w", ["0"])[0])
            except (TypeError, ValueError):
                width = 0
            if width and target.is_file():
                target = self._thumbnail(root, target, width)
            # 产物带 ?v= 版本参数,可放心长缓存;重画后版本号变化自动失效
            return self._file(target, cache_seconds=86400)

        def _assets(self, query):
            title = query.get("project", [""])[0]
            kind = query.get("kind", [None])[0]

            def fetch(app):
                project = app.projects.get_project(title)
                if project is None:
                    return None
                rows = app.assets.list(project["id"], kind=kind)
                items = []
                for row in rows:
                    item = dict(row)
                    item["meta"] = json.loads(item["meta"])
                    item["url"] = _artifact_url(app, item["uri"])
                    items.append(item)
                return items

            items = self._with_app(fetch)
            if items is None:
                return self._error(404, f"项目不存在: {title}")
            return self._json(items)

        def _standards(self):
            def fetch(app):
                active = app.standards.active()
                return {
                    "active": active,
                    "history": app.standards.history(
                        active.get("profile_key")),
                    "capabilities": {
                        "versioned": True,
                        "episode_snapshot": True,
                        "import_export": True,
                        "locked_paths": [
                            "rules.production.video_model",
                            "rules.production.resolution",
                            "rules.production.voice",
                            "rules.production.lip_sync",
                            "rules.production.burn_subtitles",
                            "rules.production.fast_vip_real_face_conflict",
                        ],
                    },
                }
            return self._json(self._with_app(fetch))

        def _standards_export(self, version_id=None):
            return self._json(self._with_app(
                lambda app: app.standards.export_bundle(version_id)))

        def _standard_error(self, exc, status=400):
            issues = getattr(exc, "issues", None) or []
            return self._json({
                "error": "制作标准校验失败",
                "message": str(exc),
                "issues": issues,
            }, status=status)

        def _standards_save(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            if not isinstance(body.get("content"), dict):
                return self._error(400, "缺少 content 制作标准")
            try:
                snapshot = self._with_app(lambda app: app.standards.save(
                    body["content"],
                    change_note=(body.get("change_note") or "").strip(),
                    activate=bool(body.get("activate", True)),
                    expected_active_id=body.get("expected_active_id")))
            except StandardValidationError as exc:
                return self._standard_error(exc)
            except StandardConflictError as exc:
                return self._json({
                    "error": "制作标准已被其他操作更新",
                    "message": str(exc),
                    "expected_active_id": exc.expected_active_id,
                    "actual_active_id": exc.actual_active_id,
                }, status=409)
            return self._json({"standard": snapshot}, status=201)

        def _standards_activate(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            try:
                version_id = int(body.get("version_id"))
            except (TypeError, ValueError):
                return self._error(400, "缺少合法 version_id")
            try:
                snapshot = self._with_app(
                    lambda app: app.standards.activate(version_id))
            except (StandardValidationError, ValueError, KeyError) as exc:
                return self._standard_error(exc)
            return self._json({"standard": snapshot})

        def _standards_reset(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            try:
                snapshot = self._with_app(lambda app: app.standards.reset(
                    change_note=(body.get("change_note")
                                 or "恢复 SK 五维漫剧 V5 官方标准").strip()))
            except StandardValidationError as exc:
                return self._standard_error(exc)
            return self._json({"standard": snapshot}, status=201)

        def _standards_import(self):
            if int(self.headers.get("Content-Length", "0")) > 2 * 1024 * 1024:
                return self._error(400, "制作标准包不能超过 2MB")
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            bundle = body.get("bundle")
            if not isinstance(bundle, dict):
                return self._error(400, "缺少 bundle 标准包")
            try:
                snapshot = self._with_app(
                    lambda app: app.standards.import_bundle(
                        bundle,
                        change_note=(body.get("change_note")
                                     or "导入制作标准").strip(),
                        activate=bool(body.get("activate", True))))
            except StandardValidationError as exc:
                return self._standard_error(exc)
            return self._json({"standard": snapshot}, status=201)

        def _export(self, episode_id):
            """成品包 zip 下载。"""
            from urllib.parse import quote as _quote

            from ..export_kit import build_export_zip

            def task(app):
                episode = app.projects.get_episode(episode_id)
                if episode is None:
                    return None
                project = app.db.query_one(
                    "SELECT * FROM projects WHERE id=?",
                    (episode["project_id"],))
                return build_export_zip(
                    app, project["title"], episode["number"])

            try:
                result = self._with_app(task)
            except Exception as exc:
                return self._error(400, str(exc))
            if result is None:
                return self._error(404, "剧集不存在")
            data, filename = result
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{_quote(filename)}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _produce(self):
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(
                    self.rfile.read(length).decode("utf-8")) if length else {}
            except ValueError:
                return self._error(400, "请求体不是合法 JSON")
            title = body.get("title")
            number = body.get("episode")
            sentence = body.get("sentence", "")
            note = ""
            if sentence or not (title and number):
                try:
                    title, number, note = self._with_app(
                        lambda app: resolve_produce_target(
                            app, sentence, title=title,
                            number=int(number) if number else None))
                except AifosError as exc:
                    return self._error(400, str(exc))
            script = None
            if body.get("script_text"):
                from ..script_import import ScriptImportError, parse_any
                try:
                    script = parse_any(
                        body["script_text"], title, int(number))
                except ScriptImportError as exc:
                    return self._error(400, str(exc))
            # Web 端默认走「预生产 → 确认 → 自动生产」流程
            job_id = jobs.start(
                title, int(number),
                premise=body.get("premise", ""),
                style=body.get("style", ""),
                force=bool(body.get("force")),
                script=script,
                review=bool(body.get("review", True)),
                kind=body.get("kind")
                if body.get("kind") in ("drama", "idol") else None,
                action=("force_rebuild" if body.get("force") else
                        "script_import" if script is not None else
                        "produce"))
            return self._json(
                {"job_id": job_id, "title": title,
                 "episode": int(number), "note": note}, status=202)

        def _confirm(self):
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(
                    self.rfile.read(length).decode("utf-8")) if length else {}
            except ValueError:
                return self._error(400, "请求体不是合法 JSON")
            episode_id = body.get("episode_id")
            if not episode_id:
                return self._error(400, "缺少 episode_id")

            def lookup(app):
                episode = app.projects.get_episode(int(episode_id))
                if episode is None:
                    return None
                project = app.db.query_one(
                    "SELECT * FROM projects WHERE id=?",
                    (episode["project_id"],))
                return (project["title"], episode["number"],
                        episode["status"], project["id"], episode["id"])

            found = self._with_app(lookup)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number, status, project_id, found_episode_id = found
            video_quality = body.get("video_quality")
            if video_quality is not None:
                try:
                    video_quality = normalize_quality(
                        video_quality, allow_auto=True,
                        field="video_quality")
                    self._with_app(
                        lambda app: app.director.update_quality_policy(
                            found_episode_id,
                            video_default=video_quality))
                except AifosError as exc:
                    return self._error(400, str(exc))
            if status == "awaiting_cast":
                selection = self._with_app(
                    lambda app: app.director.character_selection_status(
                        project_id,
                        (app.projects.latest_document(
                            found_episode_id, "script")[0] or {}).get(
                                "characters", [])))
                if not selection.get("passed"):
                    return self._error(
                        409, "请先为每名角色选定1张最终立绘，再继续生产")
            # 剧本确认 → 继续预生产(画完人物/分镜再停一次);
            # 开拍确认 → 自动完成视频/配音/剪辑/质检
            job_id = jobs.start(
                title, number,
                review=(status in ("awaiting_script", "awaiting_cast")),
                action=("confirm_script" if status == "awaiting_script"
                        else "confirm_cast" if status == "awaiting_cast"
                        else "confirm_preflight"))
            return self._json(
                {"job_id": job_id, "phase": status}, status=202)

        def _character_select(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            name = str(body.get("character") or "").strip()
            index = body.get("candidate_index")
            if not name or index is None:
                return self._error(400, "缺少 character/candidate_index")
            title, number = found
            try:
                result = self._with_app(
                    lambda app: app.director.select_character_candidate(
                        title, number, name, int(index)))
            except (AifosError, TypeError, ValueError) as exc:
                return self._error(400, str(exc))
            return self._json(result)

        def _episode_ref(self, body):
            episode_id = body.get("episode_id")
            if not episode_id:
                return None

            def lookup(app):
                episode = app.projects.get_episode(int(episode_id))
                if episode is None:
                    return None
                project = app.db.query_one(
                    "SELECT * FROM projects WHERE id=?",
                    (episode["project_id"],))
                return project["title"], episode["number"]

            return self._with_app(lookup)

        def _read_body(self):
            length = int(self.headers.get("Content-Length", "0"))
            try:
                return json.loads(
                    self.rfile.read(length).decode("utf-8")) if length else {}
            except ValueError:
                return None

        def _revise(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            feedback = (body.get("feedback") or "").strip()
            if not feedback:
                return self._error(400, "请填写修改意见")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            job_id = jobs.start_task(
                title, number,
                lambda app, run_id: app.director.revise_script(
                    title, number, feedback, run_id=run_id),
                action="revise_script", request={"feedback": feedback})
            return self._json({"job_id": job_id}, status=202)

        def _regen_image(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            target = body.get("target") or {}
            if target.get("kind") not in ("character_art", "scene_art",
                                          "shot", "character_sheet",
                                          "frames"):
                return self._error(400, "target.kind 需为 character_art/"
                                        "scene_art/shot/character_sheet/"
                                        "frames")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            feedback = (body.get("feedback") or "").strip()
            prompt = (body.get("prompt") or "").strip()
            quality = body.get("quality")
            if quality is not None:
                try:
                    quality = normalize_quality(
                        quality, allow_auto=True, field="image_quality")
                except AifosError as exc:
                    return self._error(400, str(exc))
            job_id = jobs.start_task(
                title, number,
                lambda app, run_id: app.director.regen_image(
                    title, number, target, feedback=feedback,
                    prompt_override=prompt, quality_override=quality),
                action="regen_image",
                request={"target": target, "feedback": feedback,
                         "prompt": prompt, "quality": quality})
            return self._json({"job_id": job_id}, status=202)

        def _settings_update(self):
            """设置中心保存 Provider、能力路由或整套图片策略。"""
            from ..settings import set_image_strategy, set_routing, \
                settings_payload, update_provider
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")

            def task(app):
                if body.get("provider"):
                    update_provider(app.workspace.config_path,
                                    body["provider"],
                                    body.get("fields") or {})
                elif body.get("image_strategy"):
                    set_image_strategy(app.workspace.config_path,
                                       body["image_strategy"])
                elif body.get("defaults"):
                    from ..settings import set_defaults
                    set_defaults(app.workspace.config_path,
                                 body["defaults"])
                elif body.get("capability"):
                    chain = body.get("chain") or []
                    if isinstance(chain, str):
                        chain = [c.strip() for c in chain.split(",")
                                 if c.strip()]
                    set_routing(app.workspace.config_path,
                                body["capability"], chain)
                else:
                    raise AifosError("缺少 provider 或 capability")

            try:
                self._with_app(task)
            except AifosError as exc:
                return self._error(400, str(exc))
            # 重新加载,回传保存后的完整视图
            return self._json(self._with_app(settings_payload))

        def _settings_test(self):
            from ..settings import test_provider
            body = self._read_body()
            if body is None or not body.get("provider"):
                return self._error(400, "缺少 provider")
            try:
                report = self._with_app(
                    lambda app: test_provider(app, body["provider"]))
            except AifosError as exc:
                return self._error(400, str(exc))
            return self._json(report)

        def _settings_detect(self):
            """自动检测本机 CLI 并接线,返回检测结果 + 最新设置视图。"""
            from ..doctor import apply_detected, detect_clis
            from ..settings import settings_payload

            def task(app):
                found = detect_clis()
                applied = apply_detected(app.workspace.config_path, found)
                return found, applied

            found, applied = self._with_app(task)
            view = self._with_app(settings_payload)
            view["detected"] = found
            view["applied"] = [{"provider": p, "path": path}
                               for p, path in applied]
            return self._json(view)

        def _stop(self):
            """停止生成:置 cancelling,流水线在下一次产线调用前安全停下,
            落回最近的可调整检查点(剧本确认/开拍确认)。"""
            body = self._read_body()
            if body is None or not body.get("episode_id"):
                return self._error(400, "缺少 episode_id")
            stable = {"done", "failed", "qc_failed", "created",
                      "awaiting_script", "awaiting_confirm"}

            def inspect(app):
                episode = app.projects.get_episode(int(body["episode_id"]))
                if episode is None:
                    raise AifosError("剧集不存在")
                project = app.db.query_one(
                    "SELECT * FROM projects WHERE id=?",
                    (episode["project_id"],))
                # 有正在运行的制作任务时,无论状态都允许停止
                job_running = any(
                    j["status"] == "running"
                    and j.get("title") == project["title"]
                    and j.get("episode") == episode["number"]
                    for j in jobs.list())
                if episode["status"] in stable and not job_running:
                    raise AifosError("当前没有正在进行的生成")
                project = app.db.query_one(
                    "SELECT * FROM projects WHERE id=?",
                    (episode["project_id"],))
                return dict(episode), project["title"]

            try:
                episode, title = self._with_app(inspect)
            except AifosError as exc:
                return self._error(400, str(exc))
            active = jobs.running_for(title, episode["number"])
            if not active:
                landing = self._with_app(
                    lambda app: app.history.recover_episode(
                        episode["id"], "停止时未发现活动进程，已清理失联任务"))
                return self._json({"status": landing,
                                   "previous": episode["status"],
                                   "recovered": True})

            def cancel(app):
                app.projects.set_episode_status(episode["id"], "cancelling")
                for job in active:
                    app.history.mark_cancelling(job.get("run_id"))

            self._with_app(cancel)
            return self._json({"status": "cancelling",
                               "previous": episode["status"],
                               "run_ids": [j.get("run_id") for j in active]})

        def _project_style(self):
            """画风确认:{project, style}。剧本确认页在开画前设定,
            之后所有人物/场景/分镜出图提示都会带上该画风。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            title = (body.get("project") or "").strip()
            style = (body.get("style") or "").strip()
            if not title:
                return self._error(400, "缺少 project")
            if not style:
                return self._error(400, "画风不能为空")

            def task(app):
                if app.projects.get_project(title) is None:
                    raise AifosError(f"项目不存在: {title}")
                project = app.projects.update_project(title, style=style)
                app.logger.info("director", f"画风已确认: {style}")
                return dict(project)

            try:
                project = self._with_app(task)
            except AifosError as exc:
                return self._error(400, str(exc))
            return self._json(project)

        def _project_rename(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")

            def task(app):
                title = body.get("title")
                if not title and body.get("project_id"):
                    row = app.db.query_one(
                        "SELECT * FROM projects WHERE id=?",
                        (int(body["project_id"]),))
                    if row is None:
                        raise AifosError("项目不存在")
                    title = row["title"]
                return dict(app.projects.rename_project(
                    title, body.get("new_title", "")))

            try:
                project = self._with_app(task)
            except AifosError as exc:
                return self._error(400, str(exc))
            return self._json(project)

        def _update_now(self):
            """手动触发自更新:更新成功后 1 秒重启服务(自动恢复)。"""
            busy = any(j["status"] == "running" for j in jobs.list())
            if busy:
                return self._error(409, "有生产任务在跑,空闲后会自动更新")
            status, detail = check_and_update(repo_root())
            if status == "updated":
                def later():
                    time.sleep(1)
                    restart_process()
                threading.Thread(target=later, daemon=True).start()
            return self._json({"status": status, "detail": detail})

        def _qc_item(self):
            """单张质检:{episode_id|project+episode, item_id}(同步返回结果)。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            item_id = (body.get("item_id") or "").strip()
            if not item_id:
                return self._error(400, "缺少 item_id")
            title, number = found
            try:
                report = self._with_app(
                    lambda app: app.director.qc_item(title, number, item_id))
            except AifosError as exc:
                return self._error(400, str(exc))
            return self._json(report)

        def _qc_all(self):
            """批量质检:后台逐张核对(可暂停)。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            job_id = jobs.start_task(
                title, number,
                lambda app, run_id: app.director.qc_all(title, number),
                action="qc_all")
            return self._json({"job_id": job_id}, status=202)

        def _redo_items(self):
            """批量重画:{item_ids:[...]} 或 {only_failed:true}。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            item_ids = body.get("item_ids") or []
            only_failed = bool(body.get("only_failed"))
            quality = body.get("quality")
            if quality is not None:
                try:
                    quality = normalize_quality(
                        quality, allow_auto=True, field="image_quality")
                except AifosError as exc:
                    return self._error(400, str(exc))
            job_id = jobs.start_task(
                title, number,
                lambda app, run_id, report: app.director.redo_items(
                    title, number, item_ids=item_ids,
                    only_failed=only_failed, quality_override=quality,
                    progress=report),
                action="redo_items", tracked=True,
                request={"item_ids": item_ids,
                         "only_failed": only_failed, "quality": quality})
            return self._json({"job_id": job_id}, status=202)

        def _redo_mock(self):
            """一键补真:{episode_id|project+episode} → 只重画占位图。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            job_id = jobs.start_task(
                title, number,
                lambda app, run_id: app.director.redo_placeholders(
                    title, number),
                action="redo_placeholders")
            return self._json({"job_id": job_id}, status=202)

        def _restyle(self):
            """一键换画风:{episode_id|project+episode, style?}。
            后台按新画风重做全部形象(立绘/套件/场景),可暂停续做。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            style = (body.get("style") or "").strip()
            job_id = jobs.start_task(
                title, number,
                lambda app, run_id: app.director.restyle_project(
                    title, number, style=style),
                action="restyle", request={"style": style})
            return self._json({"job_id": job_id}, status=202)

        def _reference_upload(self):
            """参考图上传:{project|episode_id, name, attach_to, note,
            filename, data_base64};出图时自动注入提示。"""
            import base64
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            title = (body.get("project") or "").strip()
            if not title:
                found = self._episode_ref(body)
                if found is None:
                    return self._error(400, "缺少 project(或有效 episode_id)")
                title = found[0]
            try:
                data = base64.b64decode(body.get("data_base64", ""))
            except Exception:
                return self._error(400, "data_base64 解码失败")
            if not data:
                return self._error(400, "文件为空")
            if len(data) > 50 * 1024 * 1024:
                return self._error(400, "参考图超过 50MB")
            ext = Path(body.get("filename", "")).suffix.lower() or ".png"
            try:
                result = self._with_app(
                    lambda app: app.director.add_reference(
                        title, body.get("name", ""), data, ext,
                        attach_to=(body.get("attach_to") or "").strip(),
                        note=(body.get("note") or "").strip()))
            except Exception as exc:
                return self._error(400, str(exc))
            return self._json(result)

        def _reference_delete(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            try:
                result = self._with_app(
                    lambda app: app.director.delete_reference(
                        (body.get("project") or "").strip(),
                        (body.get("name") or "").strip()))
            except Exception as exc:
                return self._error(400, str(exc))
            return self._json(result)

        def _asset_delete(self):
            """资产中心删图：软删除当前版本，历史文件保持可恢复。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            title = (body.get("project") or "").strip()
            if not title or body.get("asset_id") is None:
                return self._error(400, "缺少 project/asset_id")
            try:
                result = self._with_app(
                    lambda app: app.director.delete_image_asset(
                        title, int(body["asset_id"])))
            except Exception as exc:
                return self._error(400, str(exc))
            return self._json(result)

        def _video_references(self):
            """按镜头保存从资产中心选择的 Seedance 多图参考。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            if body.get("episode_id") is None or body.get("shot_no") is None:
                return self._error(400, "缺少 episode_id/shot_no")
            try:
                result = self._with_app(
                    lambda app: app.director.set_video_references(
                        int(body["episode_id"]), int(body["shot_no"]),
                        body.get("asset_ids") or []))
            except Exception as exc:
                return self._error(400, str(exc))
            return self._json(result)

        def _upload(self):
            """人工修改素材上传:{episode_id, target, filename, data_base64}。
            target.kind: character_art / scene_art / shot / shot_video。"""
            import base64
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            target = body.get("target") or {}
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            try:
                data = base64.b64decode(body.get("data_base64", ""))
            except Exception:
                return self._error(400, "data_base64 解码失败")
            if not data:
                return self._error(400, "文件为空")
            if len(data) > 200 * 1024 * 1024:
                return self._error(400, "文件超过 200MB")
            ext = Path(body.get("filename", "")).suffix.lower() or ".png"

            def task(app):
                if target.get("kind") == "shot_video":
                    return app.director.import_video(
                        title, number, int(target["shot_no"]), data, ext)
                return app.director.import_image(
                    title, number, target, data, ext)

            try:
                result = self._with_app(task)
            except Exception as exc:
                return self._error(400, str(exc))
            return self._json(result)

    return Handler


def serve(workspace, host="127.0.0.1", port=8619):
    """构建并返回 HTTP 服务器(调用方负责 serve_forever)。"""
    jobs = JobRegistry(workspace)
    handler = make_handler(workspace, jobs)
    httpd = ThreadingHTTPServer((host, port), handler)
    # 零操作自动更新:空闲时拉取新版并自愈重启(可用
    # defaults.auto_update=false 关闭;非 git 安装自动跳过)
    app = App(workspace)
    try:
        auto = app.config.get("defaults", "auto_update", default=True)
    finally:
        app.close()
    if auto:
        def on_log(message):
            worker = App(workspace)
            try:
                worker.logger.info("updater", message)
            finally:
                worker.close()
        start_auto_updater(
            jobs_idle=lambda: all(
                j["status"] != "running" for j in jobs.list()),
            on_log=on_log)
    return httpd
