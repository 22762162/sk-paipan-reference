"""AIFOS Web 服务:http.server 实现的 JSON API + 静态页面 + 产物文件服务。

启动:python3 -m aifos serve [--host 127.0.0.1] [--port 8619]

API:
  GET  /api/overview            全局看板(项目/剧集/成本/额度/任务)
  GET  /api/episode/<id>        单集详情(阶段/剧本/分镜/质检/产物索引)
  GET  /api/assets?project=T    项目资产列表
  GET  /api/logs?limit=N        最近日志
  GET  /api/jobs  /api/jobs/<id>后台制作任务
  POST /api/produce             {"sentence": "开始制作《万妖图录》第15集"}
静态:
  GET  /                        控制台单页应用
  GET  /artifacts/<path>        workspace/artifacts 下的产物(防目录穿越)
"""

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__
from ..app import App
from ..errors import AifosError
from ..smart_input import resolve_produce_target

STATIC_DIR = Path(__file__).parent / "static"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".jsonl": "application/x-ndjson; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}


class JobRegistry:
    """produce 后台任务:制作可能耗时(真实产线更久),Web 端异步执行。"""

    def __init__(self, workspace):
        self.workspace = workspace
        self._jobs = {}
        self._lock = threading.Lock()
        self._seq = 0

    def start(self, title, number, premise="", style="", force=False,
              script=None, review=False, kind=None):
        with self._lock:
            self._seq += 1
            job_id = f"j{self._seq}"
            self._jobs[job_id] = {
                "id": job_id, "status": "running",
                "title": title, "episode": number, "force": force,
                "started_at": time.time(),
            }

        def task(app):
            return app.director.produce(
                title, number, premise=premise, style=style, force=force,
                script=script, pause_for_confirm=review, kind=kind)

        self._run(job_id, task)
        return job_id

    def start_task(self, title, number, task):
        """通用后台任务(打磨重写/重画)。task(app) → summary。"""
        with self._lock:
            self._seq += 1
            job_id = f"j{self._seq}"
            self._jobs[job_id] = {
                "id": job_id, "status": "running",
                "title": title, "episode": number,
                "started_at": time.time(),
            }
        self._run(job_id, task)
        return job_id

    def _run(self, job_id, task):
        def run():
            app = App(self.workspace)
            try:
                summary = task(app)
                self._jobs[job_id].update(
                    status="done", summary=summary, finished_at=time.time())
            except Exception as exc:  # 后台任务兜底,错误进任务状态
                self._jobs[job_id].update(
                    status="failed", error=str(exc), finished_at=time.time())
            finally:
                app.close()

        threading.Thread(target=run, daemon=True).start()

    def get(self, job_id):
        return self._jobs.get(job_id)

    def list(self):
        return sorted(self._jobs.values(),
                      key=lambda j: j["started_at"], reverse=True)


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


def _collect_artifacts(app, project_id, ep_num):
    """从资产表重建单集产物索引(按镜头/台词编号)。"""
    prefix = f"e{ep_num:03d}"
    rows = app.db.query(
        "SELECT * FROM assets WHERE project_id=? AND name LIKE ?",
        (project_id, prefix + "%"))
    shot_re = re.compile(rf"^{prefix}_shot(\d+)$")
    line_re = re.compile(rf"^{prefix}_line(\d+)$")
    clip_re = re.compile(rf"^{prefix}_scene(\d+)$")
    out = {"images": {}, "first": {}, "last": {}, "videos": {},
           "voices": {}, "cover": None, "final": None,
           "titles": [], "clips": []}
    kind_map = {"image": "images", "first_frame": "first",
                "last_frame": "last", "video": "videos"}
    for row in rows:
        kind, name = row["kind"], row["name"]
        url = _versioned(_artifact_url(app, row["uri"]), row)
        shot = shot_re.match(name)
        if shot and kind in kind_map:
            out[kind_map[kind]][int(shot.group(1))] = url
            continue
        line = line_re.match(name)
        if line and kind == "voice":
            out["voices"][int(line.group(1))] = url
            continue
        if kind == "cover" and name == prefix:
            out["cover"] = url
        elif kind == "edit" and name == f"{prefix}_final":
            out["final"] = url
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
        rows_ = app.db.query(
            "SELECT * FROM assets WHERE project_id=? AND kind=? "
            "ORDER BY name, version", (project_id, kind))
        latest = {}
        for row in rows_:
            latest[row["name"]] = row
        return latest.values()

    out["cast_art"] = [
        {"name": row["name"],
         "url": _versioned(_artifact_url(app, row["uri"]), row),
         "role": json.loads(row["meta"]).get("role", "")}
        for row in latest_rows("character_art")]
    out["scene_art"] = [
        {"name": row["name"],
         "url": _versioned(_artifact_url(app, row["uri"]), row)}
        for row in latest_rows("scene_art")]
    return out


def _episode_payload(app, episode_id):
    episode = app.projects.get_episode(episode_id)
    if episode is None:
        return None
    project = app.db.query_one(
        "SELECT * FROM projects WHERE id=?", (episode["project_id"],))
    script, script_v = app.projects.latest_document(episode_id, "script")
    storyboard, sb_v = app.projects.latest_document(episode_id, "storyboard")
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
    return {
        "episode": dict(episode),
        "project": dict(project),
        "tasks": tasks,
        "script": script,
        "script_version": script_v,
        "storyboard": storyboard,
        "storyboard_version": sb_v,
        "qc_report": qc_report,
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
    return {
        "version": __version__,
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

        def _file(self, path):
            path = Path(path)
            if not path.is_file():
                return self._error(404, "文件不存在")
            body = path.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                MIME.get(path.suffix.lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
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
                    return self._file(STATIC_DIR / "index.html")
                if route.startswith("/static/"):
                    return self._static(STATIC_DIR, route[len("/static/"):])
                if route.startswith("/artifacts/"):
                    return self._artifact(route[len("/artifacts/"):])
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
                if route == "/api/jobs":
                    return self._json(jobs.list())
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
                if parsed.path == "/api/project/rename":
                    return self._project_rename()
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
            return self._file(target)

        def _artifact(self, rel):
            app = App(workspace)
            try:
                root = app.workspace.artifacts_dir.resolve()
            finally:
                app.close()
            target = (root / rel).resolve()
            if not str(target).startswith(str(root) + "/"):
                return self._error(404, "非法路径")
            return self._file(target)

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
                if body.get("kind") in ("drama", "idol") else None)
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
                return project["title"], episode["number"]

            found = self._with_app(lookup)
            if found is None:
                return self._error(404, "剧集不存在")
            job_id = jobs.start(found[0], found[1], review=False)
            return self._json({"job_id": job_id}, status=202)

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
                lambda app: app.director.revise_script(
                    title, number, feedback))
            return self._json({"job_id": job_id}, status=202)

        def _regen_image(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            target = body.get("target") or {}
            if target.get("kind") not in ("character_art", "scene_art",
                                          "shot"):
                return self._error(400, "target.kind 需为 "
                                        "character_art/scene_art/shot")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            feedback = (body.get("feedback") or "").strip()
            job_id = jobs.start_task(
                title, number,
                lambda app: app.director.regen_image(
                    title, number, target, feedback=feedback))
            return self._json({"job_id": job_id}, status=202)

        def _settings_update(self):
            """设置中心保存:{provider, fields} 或 {capability, chain}。"""
            from ..settings import set_routing, settings_payload, \
                update_provider
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")

            def task(app):
                if body.get("provider"):
                    update_provider(app.workspace.config_path,
                                    body["provider"],
                                    body.get("fields") or {})
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
    return ThreadingHTTPServer((host, port), handler)
