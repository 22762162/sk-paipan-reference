"""AIFOS Web 服务:http.server 实现的 JSON API + 静态页面 + 产物文件服务。

启动:python3 -m aifos serve [--host 127.0.0.1] [--port 8619]

API:
  GET  /api/overview            全局看板(项目/剧集/成本/额度/任务)
  GET  /api/episode/<id>        单集详情(阶段/剧本/分镜/质检/产物索引)
  GET  /api/episode/<id>/status 单集轻量变更摘要(用于手机端轮询)
  GET  /api/episode/<id>/prompts 逐镜最高规则提示词与 PASS/WARN/BLOCK
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

import base64
import binascii
import copy
import hashlib
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
from ..asset_center import IMAGE_KINDS
from ..lessons import project_lessons, set_lesson_approval
from ..updater import (check_and_update, current_build, repo_root,
                       restart_process, start_auto_updater)
from ..errors import AifosError
from ..quality_policy import normalize_quality, normalize_quality_policy
from ..prompt_review import build_episode_prompt_review
from ..scene_render import build_scene_render_contract
from ..smart_input import resolve_produce_target
from ..standard_center import StandardConflictError, StandardValidationError
from ..story_context import attach_shot_story_context

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


# 整集生产:并行 worker 正在改整份 render_plan,此时改单张镜头不安全,
# 必须让用户显式暂停。其余单点任务(单图重画、单图质检)不属于这一类——
# 它们只该互相排队,不该互相拒绝,更不该互相打断。
PRODUCTION_ACTIONS = frozenset({
    "produce", "force_rebuild", "script_import", "series_next",
    "confirm_script", "image_acceleration_resume",
})


class JobRegistry:
    """produce 后台任务:制作可能耗时(真实产线更久),Web 端异步执行。"""

    PRODUCTION_ACTIONS = PRODUCTION_ACTIONS

    def __init__(self, workspace):
        self.workspace = workspace
        self._jobs = {}
        # 单集串行队列:同一集的单点任务共享 render_plan.json 和相邻镜头
        # 边界,不能并行;但也不该互相拒绝——否则用户改完一张必须盯着等它
        # 跑完才能提交下一张。按提交顺序排队,逐个执行。
        self._queues = {}
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
              script=None, review=False, kind=None, action="produce",
              unique=False, style_pack_id="", auto_select_assets=False,
              fresh_assets=False):
        """启动生产；unique=True 时同一集重复提交复用正在运行的任务。

        检查、创建历史和登记 job 必须处在同一把锁内，否则两个浏览器标签
        同时点「确认」仍可能各自通过检查，重复消耗 Seedance/生图额度。
        """
        with self._lock:
            if unique:
                existing = next((
                    job for job in self._jobs.values()
                    if job["status"] == "running"
                    and job["title"] == title
                    and int(job["episode"]) == int(number)), None)
                if existing is not None:
                    return existing["id"]
            run_id = self._create_history(
                title, number, action, force=force,
                request={"premise": premise, "style": style,
                         "style_pack_id": style_pack_id,
                         "review": bool(review), "kind": kind,
                         "script_supplied": script is not None,
                         "auto_select_assets": bool(auto_select_assets),
                         "fresh_assets": bool(fresh_assets)})
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
                run_id=run_id, style_pack_id=style_pack_id,
                auto_select_assets=auto_select_assets,
                fresh_assets=fresh_assets)

        self._run(job_id, task)
        return job_id

    def start_task(self, title, number, task, action="adjustment",
                   request=None, tracked=False, unique=False, queue=False):
        """通用后台任务(打磨重写/重画)。

        普通任务签名为 ``task(app, run_id)``；tracked=True 时额外传入
        ``report(**fields)``，让长批次把逐项进度写进 job，前端无需猜测。
        """
        with self._lock:
            if unique:
                existing = next((
                    job for job in self._jobs.values()
                    if job["status"] == "running"
                    and job["title"] == title
                    and int(job["episode"]) == int(number)), None)
                if existing is not None:
                    return existing["id"]
            run_id = self._create_history(
                title, number, action, request=request)
            self._seq += 1
            job_id = f"j{self._seq}"
            self._jobs[job_id] = {
                "id": job_id, "status": "queued" if queue else "running",
                "title": title, "episode": number, "action": action,
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

        callback = ((lambda app: task(app, run_id, report))
                    if tracked else (lambda app: task(app, run_id)))

        def accounted_runner(app):
            project = app.projects.get_project(title)
            episode = (app.db.query_one(
                "SELECT * FROM episodes WHERE project_id=? AND number=?",
                (project["id"], int(number)))
                if project is not None else None)
            before_episode_cost = float(
                episode["cost"] or 0) if episode is not None else 0.0
            before_task_cost = float((app.db.query_one(
                "SELECT COALESCE(SUM(cost), 0) AS total FROM tasks "
                "WHERE episode_id=?", (episode["id"],))
                if episode is not None else {"total": 0})["total"] or 0)
            summary = None
            error = ""
            try:
                summary = callback(app)
                return summary
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                if episode is not None:
                    current = app.projects.get_episode(episode["id"])
                    after_episode_cost = float(
                        current["cost"] or 0) if current is not None else 0.0
                    after_task_cost = float(app.db.query_one(
                        "SELECT COALESCE(SUM(cost), 0) AS total FROM tasks "
                        "WHERE episode_id=?", (episode["id"],))["total"] or 0)
                    unassigned = round(max(
                        0.0,
                        (after_episode_cost - before_episode_cost)
                        - (after_task_cost - before_task_cost),
                    ), 4)
                    if unassigned > 0:
                        stage, name = {
                            "regenerate_cast": ("cast", "人物/道具候选重做"),
                            "revise_script": ("script", "剧本打磨重写"),
                            "regen_image": ("images", "图片定向修改"),
                            "reanalyze_story": ("script", "制作圣经重新分析"),
                            "qc_all": ("qc", "图片批量复检"),
                            "recheck_current_storyboard": (
                                "qc", "当前分镜合同批量复检"),
                            "redo_items": ("images", "图片批量重画"),
                            "redo_video": ("videos", "视频定向修改"),
                            "redo_placeholders": ("images", "占位图片补真"),
                            "restyle": ("cast", "全剧视觉风格重做"),
                        }.get(action, ("adjustment", "制作调整"))
                        providers = ",".join(sorted(
                            str(value) for value in
                            getattr(app.director, "_task_providers", set())
                            if str(value))) or "调整任务（Provider 未回传）"
                        summary_dict = (
                            summary if isinstance(summary, dict) else {})
                        result_status = str(
                            summary_dict.get("status") or "")
                        task_status = (
                            "failed" if error else
                            "stopped" if result_status == "paused" else
                            "done")
                        ts = time.time()
                        app.db.execute(
                            "INSERT INTO tasks("
                            "episode_id, run_id, stage, name, status, "
                            "provider, cost, result, error, created_at, "
                            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                episode["id"], run_id, stage, name,
                                task_status, providers, unassigned,
                                json.dumps(
                                    summary_dict, ensure_ascii=False,
                                    default=str)[:4000],
                                error[:1000], ts, ts,
                            ))

        if queue:
            self._submit_serial(job_id, title, number, accounted_runner)
        else:
            self._run(job_id, accounted_runner)
        return job_id

    def _episode_busy(self, title, number):
        """调用方须持锁。本集是否已有任务在跑。"""
        return any(
            job["status"] == "running" and job["title"] == title
            and int(job["episode"]) == int(number)
            for job in self._jobs.values())

    def _renumber_queue(self, key):
        """调用方须持锁。刷新排队位次,界面据此显示"前面还有几张"。"""
        for index, (queued_id, _) in enumerate(self._queues.get(key) or []):
            job = self._jobs.get(queued_id)
            if job is not None:
                job["queue_position"] = index + 1

    def _submit_serial(self, job_id, title, number, runner):
        """本集空闲就立刻跑,否则排队,等前一个任务结束后自动接上。"""
        key = (title, int(number))
        with self._lock:
            if self._episode_busy(title, number):
                self._queues.setdefault(key, []).append((job_id, runner))
                self._jobs[job_id]["status"] = "queued"
                self._renumber_queue(key)
                return
            self._jobs[job_id]["status"] = "running"
            self._jobs[job_id]["started_at"] = time.time()
        self._run(job_id, runner)

    def _drain_serial(self, title, number):
        """前一个任务结束后启动队首;已失效的条目顺延跳过。"""
        key = (title, int(number))
        while True:
            with self._lock:
                pending = self._queues.get(key)
                if not pending:
                    self._queues.pop(key, None)
                    return
                if self._episode_busy(title, number):
                    return
                job_id, runner = pending.pop(0)
                if not pending:
                    self._queues.pop(key, None)
                self._renumber_queue(key)
                job = self._jobs.get(job_id)
                if job is None:
                    continue
                job["status"] = "running"
                job["started_at"] = time.time()
                job.pop("queue_position", None)
            self._run(job_id, runner)
            return

    def queued_for(self, title, number):
        with self._lock:
            return [self._jobs[queued_id]
                    for queued_id, _ in (
                        self._queues.get((title, int(number))) or [])
                    if queued_id in self._jobs]

    def _run(self, job_id, task):
        def run():
            app = App(self.workspace)
            follow_up = None
            try:
                summary = task(app)
                self._jobs[job_id].update(
                    status="done", summary=summary, finished_at=time.time())
                app.history.finish_run(
                    self._jobs[job_id]["run_id"], summary=summary)
                if isinstance(summary, dict) and summary.get("status") == "done":
                    project = app.projects.get_project(summary.get("project"))
                    episode = (app.db.query_one(
                        "SELECT id FROM episodes WHERE project_id=? AND number=?",
                        (project["id"], int(summary["episode"])))
                        if project is not None else None)
                    if episode is not None:
                        try:
                            follow_up = app.series.maybe_auto_advance(
                                episode["id"])
                        except Exception as exc:
                            # 串行队列是当前成功运行的后续动作；推进失败只提示，
                            # 不能把已经交付成功的本集和历史记录反写成失败。
                            self._jobs[job_id]["series_advance_error"] = str(exc)
            except Exception as exc:  # 后台任务兜底,错误进任务状态
                self._jobs[job_id].update(
                    status="failed", error=str(exc), finished_at=time.time())
                app.history.finish_run(
                    self._jobs[job_id]["run_id"], error=str(exc))
            finally:
                app.close()
                # 无论成败都要放行队首,否则一次失败就把整条队列卡死。
                job = self._jobs.get(job_id) or {}
                if job.get("title") is not None:
                    try:
                        self._drain_serial(job["title"], job["episode"])
                    except Exception:
                        pass
            if follow_up and not follow_up.get("done"):
                try:
                    next_job = self.start_series_step(follow_up)
                    self._jobs[job_id]["series_next"] = {
                        "episode_id": follow_up["episode_id"],
                        "episode": follow_up["number"],
                        "job_id": next_job,
                    }
                except Exception as exc:
                    # 当前集已经成功，自动准备下一集失败不应篡改当前结果。
                    self._jobs[job_id]["series_advance_error"] = str(exc)

        threading.Thread(target=run, daemon=True).start()

    def start_series_step(self, step):
        """逐集运行编剧总闸门；解析稿不得直接跳到人物/出图。"""
        if step.get("done"):
            return None
        source_script = step.get("source_script")
        if (step.get("mode") == "script" and source_script is None
                and not step.get("requires_writer_adaptation")):
            # Compatibility for a legacy batch that already stored a formal
            # script and intentionally waits at the review checkpoint.
            return None
        return self.start(
            step["title"], int(step["number"]),
            premise=step.get("premise", ""), review=True,
            script=source_script,
            action="series_next")

    def get(self, job_id):
        return self._jobs.get(job_id)

    def list(self):
        return sorted(self._jobs.values(),
                      key=lambda j: j["started_at"], reverse=True)

    def production_running_for(self, title, number):
        """只统计整集生产类任务;单图重画/质检不算"本集正在生产"。"""
        return [job for job in self.running_for(title, number)
                if str(job.get("action") or "") in self.PRODUCTION_ACTIONS]

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


_DIAGNOSTIC_FIELDS = (
    "image_error", "prompt_diagnosis", "prompt_audit",
    "reference_diagnosis", "reference_audit",
    "frame_audit",
    "targeted_prompt_patch", "reference_adjustments",
    "diagnosis_complete",
)
_DIAGNOSTIC_FULL_PROMPT_KEYS = {
    "prompt", "prompt_full", "full_prompt", "original_prompt",
    "generation_prompt", "prompt_used", "revised_prompt", "final_prompt",
    "prompt_before", "prompt_after",
}


def _safe_diagnostic_text(value, limit=800):
    """诊断展示文本脱敏：不把服务器绝对路径或超长生成输入发给浏览器。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    try:
        if Path(text).is_absolute():
            return "[本地路径已隐藏]"
    except (OSError, ValueError):
        pass
    # 兼容错误文本中夹带的 macOS/Linux 绝对路径；远程 URL 不是诊断卡
    # 需要展示的内容，也一并收敛为不可点击的安全占位。
    text = re.sub(
        r"(?<![\w/])/(?:[^/\s,;，；]+/)+[^/\s,;，；]+",
        "[本地路径已隐藏]", text)
    text = re.sub(
        r"\b[A-Za-z]:\\(?:[^\\\s,;，；]+\\)*[^\\\s,;，；]+",
        "[本地路径已隐藏]", text)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _safe_diagnostic_value(value, *, depth=0):
    """仅保留可解释的结构化诊断，不透传 prompt、URI 或任意文件路径。"""
    if depth > 5:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _safe_diagnostic_text(value)
    if isinstance(value, (list, tuple)):
        return [
            safe for item in list(value)[:24]
            if (safe := _safe_diagnostic_value(
                item, depth=depth + 1)) not in (None, "", [], {})
        ]
    if not isinstance(value, dict):
        return _safe_diagnostic_text(value)
    output = {}
    for raw_key, raw_value in list(value.items())[:48]:
        key = str(raw_key)
        lowered = key.lower()
        prompt_key_is_safe = any(token in lowered for token in (
            "diagnosis", "audit", "hash", "changed", "patch",
            "adjustment", "instruction", "delta", "status",
        ))
        if (lowered in _DIAGNOSTIC_FULL_PROMPT_KEYS
                or ("prompt" in lowered and not prompt_key_is_safe)
                or any(token in lowered for token in (
                    "absolute_path", "local_path", "file_path",
                    "output_uri", "source_uri", "image_uri",
                    "video_uri", "reference_uri"))):
            continue
        safe = _safe_diagnostic_value(raw_value, depth=depth + 1)
        if safe not in (None, "", [], {}):
            output[key] = safe
    return output


def _generation_diagnostic_payload(item, qc=None):
    """统一图片/视频问题项的安全诊断契约，兼容新旧字段命名。"""
    item = item if isinstance(item, dict) else {}
    qc = qc if isinstance(qc, dict) else {}
    raw_input = (
        qc.get("input_diagnosis")
        or item.get("input_diagnosis")
        or {})
    raw_input = raw_input if isinstance(raw_input, dict) else {}
    input_diagnosis = {}
    for key in _DIAGNOSTIC_FIELDS:
        for source in (raw_input, qc, item):
            if key in source:
                input_diagnosis[key] = source[key]
                break

    def first_value(*keys, default=None):
        for source in (qc, item, raw_input):
            for key in keys:
                if key in source and source[key] is not None:
                    return source[key]
        return default

    decision = first_value("retry_decision", default=None)
    if not decision:
        decision = first_value("decision", default={})
    blocked_reason = first_value("retry_blocked_reason", default="")
    if not blocked_reason and isinstance(decision, dict):
        blocked_reason = (
            decision.get("retry_blocked_reason")
            or decision.get("blocked_reason")
            or "")
    return {
        "input_diagnosis": _safe_diagnostic_value(input_diagnosis) or {},
        "applied_changes": _safe_diagnostic_value(
            first_value("applied_changes", default=[])) or [],
        "attempt_history": _safe_diagnostic_value(
            first_value("attempt_history", default=[])) or [],
        "retry_decision": _safe_diagnostic_value(decision) or {},
        "retry_blocked_reason": _safe_diagnostic_text(blocked_reason),
        "codex_escalation": _safe_diagnostic_value(
            first_value("codex_escalation", default={})) or {},
    }


def _image_asset_catalog(app, project_id):
    """资产中心当前可见图片，补齐分类、作品、时间与提示词溯源。"""
    labels = {
        "character_candidate": "人物候选",
        "character_art": "人物立绘", "character_sheet": "人物设定",
        "prop_candidate": "道具候选", "prop_identity": "核心道具母资产",
        "scene_art": "场景概念图", "image": "镜头关键图",
        "first_frame": "首帧", "last_frame": "尾帧",
        "cover": "封面", "reference": "上传参考图",
        "spatial_blocking": "空间调度图",
        "inner_persona": "内心Q版母资产",
    }
    category_labels = {
        "character": "人物", "scene": "场景", "costume": "服装",
        "shot": "镜头", "frame": "首尾帧", "cover": "封面",
        "reference": "参考图", "inner_persona": "内心Q版",
        "prop": "道具", "style": "画风",
    }
    # 资产工坊自建资产按它自己声明的类型归类,不跟着 reference 一起
    # 堆在「参考图」里,否则自建人物形象会和上传素材混成一列。
    studio_categories = {
        "character": "character", "scene": "scene",
        "prop": "prop", "style": "style",
    }
    project = app.db.query_one(
        "SELECT id, title FROM projects WHERE id=?", (project_id,))
    if project is None:
        return []

    episodes = [dict(row) for row in app.db.query(
        "SELECT id, number, title, updated_at FROM episodes "
        "WHERE project_id=? ORDER BY updated_at DESC, number DESC",
        (project_id,))]
    episode_by_number = {int(row["number"]): row for row in episodes}
    prompts_by_episode = {}
    project_prompts = {}
    for episode in episodes:
        plan_path = (app.workspace.artifacts_dir
                     / f"p{project_id:03d}"
                     / f"e{int(episode['number']):03d}"
                     / "render_plan.json")
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            plan = {"items": []}
        for plan_item in plan.get("items", []):
            item_id = str(plan_item.get("id") or "")
            prompt = str(plan_item.get("prompt") or "").strip()
            if not item_id or not prompt:
                continue
            prompts_by_episode.setdefault(
                (int(episode["number"]), item_id), prompt)
            project_prompts.setdefault(item_id, prompt)

    active_rows = app.assets.active_list(project_id)
    selected_candidate_ids = {
        str(app.assets.meta(row).get("candidate_asset_id"))
        for row in active_rows if row["kind"] in {
            "character_identity", "prop_identity"}
        and app.assets.meta(row).get("candidate_asset_id")
    }
    stored_prompts = {}
    for row in active_rows:
        if row["kind"] != "prompt":
            continue
        meta = app.assets.meta(row)
        prompt = str(meta.get("prompt") or "").strip()
        if prompt:
            stored_prompts[row["name"]] = prompt

    def category_for(kind, meta):
        if kind in {
                "character_candidate", "character_art", "inner_persona"}:
            return "character"
        if kind in {"prop_candidate", "prop_identity"}:
            return "prop"
        if kind == "character_sheet":
            return ("costume" if meta.get("sheet") in {
                "costume", "costume_detail"} else "character")
        if kind == "scene_art":
            return "scene"
        if kind == "image":
            return "shot"
        if kind == "spatial_blocking":
            return "shot"
        if kind in {"first_frame", "last_frame"}:
            return "frame"
        return kind

    def board_group_for(kind):
        """资产画布的一级泳道:先区分是否会直接进入后续生产。"""
        if kind in {"character_art", "scene_art", "image", "first_frame",
                    "last_frame", "cover", "spatial_blocking",
                    "inner_persona", "prop_identity"}:
            return "production"
        if kind == "character_sheet":
            return "character_support"
        if kind in {"character_candidate", "prop_candidate"}:
            return "candidate"
        if kind == "reference":
            return "reference"
        return "other"

    board_group_labels = {
        "production": "主生产资产",
        "character_support": "人物辅助设定",
        "candidate": "候选与历史",
        "studio": "自建资产库",
        "reference": "上传参考图",
        "other": "其他资产",
    }
    studio_role_labels = {
        "identity": "人物身份参考", "wardrobe": "服装/道具参考",
        "scene": "场景空间参考", "composition": "构图/动作参考",
        "style": "画风参考", "manual": "手动参考图",
        "inner_persona": "内心Q版参考",
    }

    def usage_for(kind, row_id, selected=False):
        if kind == "character_candidate":
            return "已定版候选" if selected else "候选图·未定版不入镜头"
        if kind == "prop_candidate":
            return "已定版道具候选" if selected else "道具候选·未定版不入镜头"
        return {
            "character_art": "身份锚点·自动使用",
            "scene_art": "场景锚点·自动使用",
            "character_sheet": "辅助参考·按镜头调用",
            "image": "镜头关键图·可入视频",
            "first_frame": "首帧·视频必需",
            "last_frame": "尾帧·视频必需",
            "cover": "封面资产",
            "reference": "上传参考·按关联调用",
            "spatial_blocking": "多人/变机位镜头·Seedance 必传",
            "inner_persona": "必要内心戏自动使用·不计现场真人",
            "prop_identity": "核心道具锚点·相关镜头自动使用",
        }.get(kind, "项目资产")

    def prompt_key(row, meta, episode_number):
        kind, name = row["kind"], row["name"]
        if kind == "character_candidate":
            character = meta.get("character") or name.rsplit(":", 1)[0]
            index = int(meta.get("candidate_index") or name.rsplit(":", 1)[-1])
            return f"candidate:{character}:{index}"
        if kind == "prop_candidate":
            prop = meta.get("prop") or name.rsplit(":", 1)[0]
            index = int(
                meta.get("candidate_index") or name.rsplit(":", 1)[-1])
            return f"prop_candidate:{prop}:{index}"
        if kind == "character_art":
            index = meta.get("candidate_index")
            return (f"candidate:{name}:{int(index)}" if index
                    else f"char:{name}")
        if kind == "character_sheet":
            character = meta.get("character") or name.split(":", 1)[0]
            sheet = meta.get("sheet") or name.split(":", 1)[-1]
            return f"sheet:{character}:{sheet}"
        if kind == "scene_art":
            return f"scene:{name}"
        if kind == "image":
            return f"shot:{int(meta.get('shot_no') or name.rsplit('shot', 1)[-1])}"
        if kind in {"first_frame", "last_frame"}:
            return f"frames:{int(meta.get('shot_no') or name.rsplit('shot', 1)[-1])}"
        return ""

    items = []
    for row in active_rows:
        if row["kind"] not in IMAGE_KINDS:
            continue
        row_meta = app.assets.meta(row)
        if row["kind"] == "character_candidate":
            try:
                legacy_index = int(
                    row_meta.get("candidate_index") or 0)
            except (TypeError, ValueError):
                legacy_index = 0
            if legacy_index > 4:
                # 旧版第5张仍留在版本历史和文件中，但不再属于当前四选一。
                continue
        url = _versioned(_artifact_url(app, row["uri"]), row)
        if not url:
            continue
        meta = row_meta
        episode_number = meta.get(
            "source_episode_number", meta.get("episode_number"))
        match = re.match(r"^e(\d{3})(?:_|$)", row["name"])
        if episode_number is None and match:
            episode_number = int(match.group(1))
        try:
            episode_number = int(episode_number) if episode_number else None
        except (TypeError, ValueError):
            episode_number = None
        episode = episode_by_number.get(episode_number)
        direct_prompt = str(
            meta.get("prompt") or meta.get("seedance_prompt") or "").strip()
        key = ""
        try:
            key = prompt_key(row, meta, episode_number)
        except (TypeError, ValueError):
            key = ""
        prompt = (direct_prompt or stored_prompts.get(row["name"]) or
                  prompts_by_episode.get((episode_number, key)) or
                  project_prompts.get(key) or "")
        prompt_status = "recorded"
        if not prompt:
            if meta.get("uploaded") or row["kind"] == "reference":
                prompt_status = "not_applicable"
                prompt = "人工上传图片，无生成提示词"
            else:
                prompt_status = "legacy_missing"
                prompt = "早期资产未留存完整提示词"
        category = category_for(row["kind"], meta)
        quality = meta.get("image_quality", "medium")
        board_group = board_group_for(row["kind"])
        selected = str(row["id"]) in selected_candidate_ids
        studio = bool(meta.get("studio"))
        studio_type = str(meta.get("studio_asset_type") or "")
        studio_role = str(meta.get("reference_role") or "")
        label = f"{labels[row['kind']]} · {row['name']}"
        usage_label = usage_for(row["kind"], row["id"], selected)
        if studio:
            board_group = "studio"
            category = studio_categories.get(studio_type, category)
            label = (f"{meta.get('studio_asset_label') or '自建资产'}"
                     f" · {row['name']}")
            attached = str(meta.get("attach_to") or "")
            usage_label = (
                studio_role_labels.get(studio_role, "自建资产")
                + "·" + (f"关联{attached}" if attached
                         else ("全项目通用" if studio_role == "style"
                               else "仅手动选择")))
        items.append({
            "asset_id": row["id"], "kind": row["kind"],
            "name": row["name"], "version": row["version"],
            "label": label,
            "studio": studio,
            "studio_asset_type": studio_type,
            "studio_asset_label": str(meta.get("studio_asset_label") or ""),
            "reference_role": studio_role,
            "role_label": studio_role_labels.get(studio_role, ""),
            "attach_to": str(meta.get("attach_to") or ""),
            "real": bool(meta.get("real", True)),
            "url": url, "quality": quality,
            "usable_for_video": quality != "low",
            "category": category,
            "category_label": category_labels.get(category, category),
            "board_group": board_group,
            "board_group_label": board_group_labels[board_group],
            "selected": selected,
            "usage_label": usage_label,
            "generated_at": row["created_at"],
            "source_project": project["title"],
            "source_episode": episode_number,
            "source_episode_title": (episode or {}).get("title", ""),
            "prompt": prompt,
            "prompt_status": prompt_status,
            "meta": meta,
        })
    items.sort(key=lambda item: (
        item["category"], -float(item["generated_at"]), item["name"]))
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
    out["prop_art"] = [
        {"asset_id": row["id"], "kind": row["kind"], "name": row["name"],
         "url": _versioned(_artifact_url(app, row["uri"]), row),
         "meta": json.loads(row["meta"] or "{}")}
        for row in latest_rows("prop_identity")]
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
         "reference_role": json.loads(row["meta"] or "{}").get(
             "reference_role", ""),
         "url": _versioned(_artifact_url(app, row["uri"]), row)}
        for row in latest_rows("reference")]
    out["inner_personas"] = [
        {"asset_id": row["id"], "kind": row["kind"], "name": row["name"],
         "url": _versioned(_artifact_url(app, row["uri"]), row),
         "meta": json.loads(row["meta"] or "{}")}
        for row in latest_rows("inner_persona")]
    out["image_assets"] = _image_asset_catalog(app, project_id)
    return out


def _current_render_plan_items(render_plan):
    """返回当前有效清单，并屏蔽已废止的母资产历史 QC。"""
    current = []
    for item in (render_plan or {}).get("items") or []:
        if item.get("category") == "character_candidate":
            index = item.get("candidate_index")
            if not index:
                parts = str(item.get("id") or "").rsplit(":", 1)
                index = parts[-1] if len(parts) == 2 else 0
            try:
                if int(index or 0) > 4:
                    continue
            except (TypeError, ValueError):
                pass
        visible = dict(item)
        if visible.get("category") in {
                "character_candidate", "character_art",
                "character_sheet", "scene_art"}:
            # 初始母资产不做视觉质检。旧计划里的失败 QC 只属于历史
            # 运行记录，不得继续污染当前生产清单和失败徽标。
            visible.pop("qc", None)
        current.append(visible)
    return current


def _production_progress(app, episode, render_plan):
    """把图片清单换算成可核验的实时进度。

    render_plan 的 ``done`` 在资产登记前会短暂先落盘，历史异常中也可能
    永久残留；因此完成数只认资产中心里仍有效且文件真实存在的正式资产，
    不能仅凭清单状态把一张不存在的图展示为已完成。
    """
    project_id = int(episode["project_id"])
    episode_id = int(episode["id"])
    episode_number = int(episode["number"])
    plan_items = _current_render_plan_items(render_plan)
    running_task = app.db.query_one(
        "SELECT stage, name, status, updated_at FROM tasks "
        "WHERE episode_id=? AND status='running' "
        "ORDER BY id DESC LIMIT 1", (episode_id,))
    running_run = app.db.query_one(
        "SELECT id, action FROM production_runs WHERE episode_id=? "
        "AND status IN ('running', 'cancelling') "
        "ORDER BY id DESC LIMIT 1", (episode_id,))
    live_run = running_task is not None or running_run is not None
    # 「整集生产中」与「有任务在跑」必须分开:单张重画也会建一条 run,
    # 若混为一谈,提交第二张时前端会以为在生产而先发暂停,把第一张正在
    # 重画的任务直接打断。
    production_running = (
        running_run is not None
        and str(running_run["action"] or "") in PRODUCTION_ACTIONS)
    status_keys = (
        "done", "reused", "pending", "generating", "retrying",
        "awaiting_human", "failed",
    )
    category_labels = {
        "character_candidate": "人物候选",
        "prop_candidate": "道具候选",
        "character_art": "人物立绘",
        "character_sheet": "人物设定图",
        "scene_art": "场景图",
        "shot_image": "关键帧",
        "frames": "首尾帧",
    }
    assets = {
        (row["kind"], row["name"]): row
        for row in app.assets.active_list(project_id)
    }

    def valid_uri(uri):
        uri = str(uri or "").strip()
        return bool(uri) and (
            uri.startswith(("http://", "https://")) or Path(uri).is_file())

    def has_asset(kind, name):
        row = assets.get((kind, name))
        return row is not None and valid_uri(row["uri"])

    def item_has_formal_asset(item):
        """按 render_plan 项定位其唯一正式资产；失败稿 output_uri 不算。"""
        category = str(item.get("category") or "")
        shot_no = item.get("shot_no")
        try:
            shot_no = int(shot_no) if shot_no is not None else None
        except (TypeError, ValueError):
            shot_no = None
        shot_name = (
            f"e{int(episode_number):03d}_shot{shot_no:03d}"
            if shot_no is not None else "")
        if category == "shot_image":
            return bool(shot_name) and has_asset("image", shot_name)
        if category == "frames":
            return bool(shot_name) and (
                has_asset("first_frame", shot_name)
                and has_asset("last_frame", shot_name))
        if category == "character_art":
            name = str(item.get("name") or item.get("id", "").split(
                ":", 1)[-1])
            return has_asset("character_art", name)
        if category == "scene_art":
            name = str(item.get("name") or item.get("id", "").split(
                ":", 1)[-1])
            return has_asset("scene_art", name)
        if category == "character_sheet":
            name = str(item.get("name") or "")
            sheet = str(item.get("sheet") or "")
            if not (name and sheet):
                parts = str(item.get("id") or "").split(":")
                if len(parts) >= 3:
                    name, sheet = ":".join(parts[1:-1]), parts[-1]
            return bool(name and sheet) and has_asset(
                "character_sheet", f"{name}:{sheet}")
        if category == "character_candidate":
            name = str(item.get("name") or "")
            index = item.get("candidate_index")
            if not (name and index):
                parts = str(item.get("id") or "").split(":")
                if len(parts) >= 3:
                    name, index = ":".join(parts[1:-1]), parts[-1]
            try:
                asset_name = f"{name}:{int(index):02d}"
            except (TypeError, ValueError):
                return False
            return bool(name) and has_asset("character_candidate", asset_name)
        if category == "prop_candidate":
            name = str(item.get("name") or "")
            index = item.get("candidate_index")
            if not (name and index):
                parts = str(item.get("id") or "").split(":")
                if len(parts) >= 3:
                    name, index = ":".join(parts[1:-1]), parts[-1]
            try:
                asset_name = f"{name}:{int(index):02d}"
            except (TypeError, ValueError):
                return False
            return bool(name) and has_asset("prop_candidate", asset_name)
        # 新分类可显式声明正式资产映射，避免默认相信 output_uri。
        return bool(
            item.get("asset_kind") and item.get("asset_name")
            and has_asset(str(item["asset_kind"]), str(item["asset_name"])))

    def item_is_downstream_usable(item, formal_asset):
        """正式文件存在后仍须通过当前镜头合同，才能交给下游。

        ``generated`` 与 ``usable`` 必须分开：前者回答“图是否已经画出”，
        后者回答“当前版本是否已通过质检并能进入首尾帧/Seedance”。
        初始人物和场景母资产没有逐图 QC，登记成功即可使用。
        """
        if not formal_asset:
            return False
        if str(item.get("category") or "") not in {"shot_image", "frames"}:
            return True
        qc = item.get("qc") or {}
        if not qc:
            return not (
                item.get("invalidated_previous_output")
                or item.get("contract_recheck"))
        if qc.get("passed") is not True or qc.get("stale"):
            return False
        required = (
            "identity_checked", "identity_match",
            "gender_checked", "gender_match",
            "count_checked", "count_match",
            "physical_logic_checked", "physical_logic_match",
            "spatial_logic_checked", "spatial_logic_match",
        )
        return all(qc.get(field) is True for field in required)

    categories = {}
    active_items = []
    issues = []
    timestamp = time.time()
    for item in plan_items:
        category = str(item.get("category") or "other")
        stats = categories.setdefault(category, {
            "category": category,
            "label": category_labels.get(category, category),
            "total": 0,
            **{key: 0 for key in status_keys},
            "usable": 0,
            "generated": 0,
            "awaiting_review": 0,
            "needs_repair": 0,
            "queued": 0,
            "not_generated": 0,
            "unverified_done": 0,
            "percent": 0,
        })
        stats["total"] += 1
        raw_status = str(item.get("status") or "pending")
        status = {
            "running": "generating",
            "retry": "retrying",
        }.get(raw_status, raw_status)
        if status not in status_keys:
            status = "pending"
        if status in ("generating", "retrying") and not live_run:
            issues.append({
                "item_id": item.get("id", ""),
                "category": category,
                "shot_no": item.get("shot_no"),
                "label": item.get("label", ""),
                "status": "stale_active",
                "issues": ["任务已经结束，此项不再生产中，已按待续产统计"],
            })
            status = "pending"
        formal_asset = item_has_formal_asset(item)
        output_exists = valid_uri(item.get("output_uri"))
        downstream_usable = item_is_downstream_usable(item, formal_asset)
        if status in ("done", "reused"):
            if formal_asset:
                stats[status] += 1
                stats["generated"] += 1
                if downstream_usable:
                    stats["usable"] += 1
                else:
                    stats["awaiting_review"] += 1
            else:
                stats["unverified_done"] += 1
                issues.append({
                    "item_id": item.get("id", ""),
                    "category": category,
                    "shot_no": item.get("shot_no"),
                    "label": item.get("label", ""),
                    "status": "unverified_done",
                    "issues": ["清单标记完成，但未找到可用的正式资产"],
                })
        else:
            stats[status] += 1
            if output_exists:
                stats["generated"] += 1

        if downstream_usable:
            pass
        elif status in ("generating", "retrying"):
            pass
        elif status in ("awaiting_human", "failed"):
            stats["needs_repair"] += 1
        elif formal_asset or output_exists:
            stats["awaiting_review"] += (
                0 if status in ("done", "reused") else 1)
        elif status == "pending" and live_run:
            stats["queued"] += 1
        else:
            stats["not_generated"] += 1

        if status in ("generating", "retrying"):
            try:
                started_at = float(item.get("started_at") or timestamp)
            except (TypeError, ValueError):
                started_at = timestamp
            elapsed = round(max(0.0, timestamp - started_at), 1)
            active_items.append({
                "item_id": item.get("id", ""),
                "category": category,
                "category_label": stats["label"],
                "shot_no": item.get("shot_no"),
                "label": item.get("label", ""),
                "status": status,
                "started_at": started_at,
                "elapsed": elapsed,
                # 超过停滞阈值(1800s,与产线停滞检测同口径)仍在"生成中"
                # 大概率是遗留认领——秒表继续走会误导用户"没卡住"。
                "stale": elapsed > 1800,
            })
        if status in ("awaiting_human", "failed"):
            qc = item.get("qc") or {}
            item_issues = list(qc.get("issues") or [])
            if not item_issues and item.get("error"):
                item_issues = [item["error"]]
            issues.append({
                "item_id": item.get("id", ""),
                "category": category,
                "shot_no": item.get("shot_no"),
                "label": item.get("label", ""),
                "status": status,
                "issues": item_issues,
            })

    overall = {
        "total": 0,
        **{key: 0 for key in status_keys},
        "usable": 0,
        "generated": 0,
        "awaiting_review": 0,
        "needs_repair": 0,
        "queued": 0,
        "not_generated": 0,
        "unverified_done": 0,
        "percent": 0,
    }
    ordered_categories = []
    for category in categories.values():
        category["percent"] = round(
            category["usable"] * 100 / category["total"], 1
        ) if category["total"] else 0
        ordered_categories.append(category)
        for key in (
                "total", *status_keys, "usable", "generated",
                "awaiting_review", "needs_repair", "queued",
                "not_generated", "unverified_done"):
            overall[key] += int(category[key])
    overall["percent"] = round(
        overall["usable"] * 100 / overall["total"], 1
    ) if overall["total"] else 0
    if running_task is not None:
        current_stage = str(running_task["stage"])
        current_stage_label = str(running_task["name"])
    elif active_items:
        current_stage = str(active_items[0]["category"])
        current_stage_label = (
            f"{active_items[0]['category_label']}生产")
    elif running_run is not None:
        current_stage = str(running_run["action"] or "production")
        current_stage_label = {
            "regen_image": "图片定向修改",
            "redo_items": "批量补画",
            "video_regen": "视频定向修改",
            "produce": "继续制作",
        }.get(current_stage, "生产任务")
    else:
        current_stage = str(episode["status"] or "")
        current_stage_label = current_stage
    def parallel_limit(key, default):
        try:
            value = int(app.config.get("defaults", key, default=default))
        except (TypeError, ValueError):
            value = default
        return max(1, min(value, 8))

    image_limit = parallel_limit("parallel_images", 3)
    # parallel_images 是每条图片通道的容量。单 Codex 通道最多 8 路；
    # A/B/C 三条独立登录通道就显示并实际提供 24 路。
    try:
        codex_ready_count = len(app.director._codex_parallel_profiles())
    except (AttributeError, TypeError, ValueError):
        codex_ready_count = 0
    if codex_ready_count:
        image_limit *= codex_ready_count
    video_limit = parallel_limit("parallel_videos", 4)
    image_active = sum(
        1 for item in active_items
        if item["category"] in {
            "character_candidate", "character_art", "character_sheet",
            "scene_art", "shot_image", "frames",
        })
    video_active = 0
    if running_task is not None and current_stage == "videos":
        shot_numbers = {
            int(item["shot_no"]) for item in plan_items
            if item.get("category") == "shot_image"
            and item.get("shot_no") is not None
        }
        missing_videos = sum(
            1 for shot_no in shot_numbers
            if not has_asset(
                "video",
                f"e{episode_number:03d}_shot{shot_no:03d}"))
        video_active = min(video_limit, missing_videos)
    overall.update({
        "running": live_run,
        "production_running": production_running,
        "current_stage": current_stage,
        "current_stage_label": current_stage_label,
        "parallelism": {
            "image": {"active": image_active, "limit": image_limit},
            "video": {"active": video_active, "limit": video_limit},
        },
    })
    return {
        "updated_at": (render_plan or {}).get("updated_at"),
        "calculated_at": timestamp,
        "overall": overall,
        "categories": ordered_categories,
        "active_items": active_items,
        "issues": issues,
    }


def _production_guidance(app, episode, storyboard, render_plan, progress):
    """根据正式资产门禁给出下一步，而不是照抄可能过期的 episode.status。

    关键帧、首尾帧、视频是严格串行的三道门。某一阶段即使已有部分产物，
    只要上游没有全部形成可读取的正式资产，就只能展示为 blocked，不能
    因为数据库曾被写成 awaiting_confirm 而误导用户进入 Seedance。
    """
    categories = {
        item["category"]: item
        for item in (progress or {}).get("categories", [])
    }
    plan_items = list((render_plan or {}).get("items") or [])
    overall = (progress or {}).get("overall") or {}
    live_run = bool(overall.get("running"))
    current_stage = str(overall.get("current_stage") or "")
    episode_number = int(episode["number"])
    project_id = int(episode["project_id"])

    storyboard_shots = list((storyboard or {}).get("shots") or [])
    storyboard_numbers = {
        int(shot["shot_no"])
        for shot in storyboard_shots
        if shot.get("shot_no") is not None
    }
    plan_numbers = {
        int(item["shot_no"])
        for item in plan_items
        if item.get("category") == "shot_image"
        and item.get("shot_no") is not None
    }
    shot_numbers = storyboard_numbers or plan_numbers
    total_shots = len(shot_numbers)
    if not total_shots:
        total_shots = max(
            int(categories.get("shot_image", {}).get("total") or 0),
            int(categories.get("frames", {}).get("total") or 0),
        )

    def valid_uri(uri):
        uri = str(uri or "").strip()
        return bool(uri) and (
            uri.startswith(("http://", "https://")) or Path(uri).is_file())

    active_assets = [
        row for row in app.assets.active_list(project_id)
        if valid_uri(row["uri"])
    ]
    asset_names = {}
    for row in active_assets:
        asset_names.setdefault(str(row["kind"]), set()).add(str(row["name"]))

    def stage_from_category(key, label):
        source = categories.get(key) or {}
        total = max(int(source.get("total") or 0), total_shots)
        usable = int(source.get("usable") or 0)
        generating = int(source.get("generating") or 0)
        retrying = int(source.get("retrying") or 0)
        awaiting_human = int(source.get("awaiting_human") or 0)
        failed = int(source.get("failed") or 0)
        return {
            "key": key,
            "label": label,
            "status": "paused",
            "total": total,
            "usable": usable,
            "generated": int(source.get("generated") or usable),
            "awaiting_review": int(source.get("awaiting_review") or 0),
            "needs_repair": int(source.get("needs_repair") or (
                awaiting_human + failed)),
            "queued": int(source.get("queued") or 0),
            "not_generated": int(source.get("not_generated") or 0),
            "pending": int(source.get("pending") or 0),
            "generating": generating,
            "retrying": retrying,
            "awaiting_human": awaiting_human,
            "failed": failed,
            "remaining": max(0, total - usable),
            "percent": round(usable * 100 / total, 1) if total else 0,
            "blocked_by": [],
            "reason": "",
        }

    keyframes = stage_from_category("shot_image", "关键帧")
    frames = stage_from_category("frames", "首尾帧")
    usable_keyframe_shots = set()
    usable_frame_shots = set()
    if shot_numbers:
        plan_by_stage_and_shot = {
            (str(item.get("category")), int(item["shot_no"])): item
            for item in plan_items
            if item.get("category") in ("shot_image", "frames")
            and item.get("shot_no") is not None
        }

        def current_contract_passed(category, shot_no):
            item = plan_by_stage_and_shot.get((category, int(shot_no)))
            if not item or item.get("status") not in ("done", "reused"):
                return False
            qc = item.get("qc") or {}
            if not qc:
                # 兼容明确关闭图片 QC 的历史项目；一旦条目标记过期或进入
                # 当前合同重检，就绝不能再靠旧资产文件存在直接放行。
                return not (
                    item.get("invalidated_previous_output")
                    or item.get("contract_recheck"))
            if qc.get("passed") is not True or qc.get("stale"):
                return False
            # 新版人物/物理/空间门禁缺一项都不算当前合同正式通过。
            required = (
                "identity_checked", "identity_match",
                "gender_checked", "gender_match",
                "count_checked", "count_match",
                "physical_logic_checked", "physical_logic_match",
                "spatial_logic_checked", "spatial_logic_match",
            )
            return all(qc.get(field) is True for field in required)

        usable_keyframe_shots = {
            shot_no for shot_no in shot_numbers
            if current_contract_passed("shot_image", shot_no)
            and f"e{episode_number:03d}_shot{shot_no:03d}"
            in asset_names.get("image", set())
        }
        usable_frame_shots = {
            shot_no for shot_no in shot_numbers
            if current_contract_passed("frames", shot_no)
            and f"e{episode_number:03d}_shot{shot_no:03d}"
            in asset_names.get("first_frame", set())
            and f"e{episode_number:03d}_shot{shot_no:03d}"
            in asset_names.get("last_frame", set())
        }
        keyframes["usable"] = len(usable_keyframe_shots)
        frames["usable"] = len(usable_frame_shots)
        for stage in (keyframes, frames):
            stage["remaining"] = max(0, stage["total"] - stage["usable"])
            stage["percent"] = round(
                stage["usable"] * 100 / stage["total"], 1
            ) if stage["total"] else 0
            accounted = (
                stage["generating"] + stage["retrying"]
                + stage["awaiting_human"] + stage["failed"])
            stage["pending"] = max(
                stage["pending"], stage["remaining"] - accounted)
            bucketed = (
                stage["usable"] + stage["awaiting_review"]
                + stage["needs_repair"] + stage["generating"]
                + stage["retrying"] + stage["queued"]
                + stage["not_generated"])
            if bucketed < stage["total"]:
                stage["not_generated"] += stage["total"] - bucketed
    continuity_keys = {
        str(shot.get("scene_no") or f"shot:{shot.get('shot_no')}")
        for shot in storyboard_shots
    }
    continuity_chains = len(continuity_keys)
    image_limit = int(
        ((overall.get("parallelism") or {}).get("image") or {}).get(
            "limit") or 1)
    frames["continuity_chains"] = continuity_chains
    frames["parallel_capacity"] = min(
        image_limit, continuity_chains) if continuity_chains else 0
    frames["note"] = (
        "当前仅1条连续链，首尾帧将按链串行"
        if continuity_chains == 1
        else (
            f"当前有{continuity_chains}条连续链，最多并行"
            f"{frames['parallel_capacity']}路"
            if continuity_chains else "尚未识别到可生产的连续链"
        )
    )

    video_prefix = f"e{episode_number:03d}_shot"
    usable_videos = sum(
        1 for name in asset_names.get("video", set())
        if name.startswith(video_prefix))
    videos = {
        "key": "videos",
        "label": "Seedance 视频",
        "status": "paused",
        "total": total_shots,
        "usable": min(usable_videos, total_shots),
        "generated": min(usable_videos, total_shots),
        "awaiting_review": 0,
        "needs_repair": 0,
        "queued": 0,
        "not_generated": max(0, total_shots - usable_videos),
        "pending": max(0, total_shots - usable_videos),
        "generating": int(
            ((overall.get("parallelism") or {}).get("video") or {}).get(
                "active") or 0),
        "retrying": 0,
        "awaiting_human": 0,
        "failed": 0,
        "remaining": max(0, total_shots - usable_videos),
        "percent": round(usable_videos * 100 / total_shots, 1)
        if total_shots else 0,
        "blocked_by": [],
        "reason": "",
    }

    keyframe_assets = {
        name for name in asset_names.get("image", set())
    }
    pending_shots = []
    issue_shots = []
    pending_item_ids = []
    issue_item_ids = []
    issue_details = []
    planned_shots = set()
    for item in plan_items:
        if item.get("category") != "shot_image":
            continue
        try:
            shot_no = int(item["shot_no"])
        except (KeyError, TypeError, ValueError):
            continue
        planned_shots.add(shot_no)
        if shot_no in usable_keyframe_shots:
            continue
        status = str(item.get("status") or "pending")
        qc = item.get("qc") or {}
        if status in ("awaiting_human", "failed") \
                or qc.get("passed") is False:
            issue_shots.append(shot_no)
            item_id = str(item.get("id") or f"shot:{shot_no}")
            issue_item_ids.append(item_id)
            hard_failure = qc.get("hard_failure") is not False
            issue_details.append({
                "item_id": item_id,
                "shot_no": shot_no,
                "hard_failure": hard_failure,
                "severity": (
                    "must_fix" if hard_failure else "review_or_accept"),
                "issues": list(qc.get("issues") or (
                    [item["error"]] if item.get("error") else [])),
                "identity_match": qc.get("identity_match"),
                "gender_match": qc.get("gender_match"),
                "count_match": qc.get("count_match"),
            })
        else:
            pending_shots.append(shot_no)
            pending_item_ids.append(str(
                item.get("id") or f"shot:{shot_no}"))
    for shot_no in sorted(shot_numbers - planned_shots):
        if shot_no not in usable_keyframe_shots:
            pending_shots.append(shot_no)
            pending_item_ids.append(f"shot:{shot_no}")
    pending_shots = sorted(set(pending_shots))
    issue_shots = sorted(set(issue_shots))
    pending_item_ids = list(dict.fromkeys(pending_item_ids))
    issue_item_ids = list(dict.fromkeys(issue_item_ids))
    issue_details.sort(key=lambda item: item["shot_no"])
    must_fix_issues = [
        item for item in issue_details if item["severity"] == "must_fix"]
    review_issues = [
        item for item in issue_details
        if item["severity"] == "review_or_accept"]

    keyframes_ready = bool(
        keyframes["total"]
        and keyframes["usable"] >= keyframes["total"])
    frames_ready = bool(
        frames["total"] and frames["usable"] >= frames["total"])
    videos_ready = bool(
        videos["total"] and videos["usable"] >= videos["total"])

    image_stage_active = (
        live_run and (
            keyframes["generating"] > 0
            or keyframes["retrying"] > 0
            or current_stage in {
                "images", "shot_image", "redo_items", "regen_image",
                "produce",
            }
        )
    )
    keyframe_count_text = (
        f"已生成 {keyframes['generated']}/{keyframes['total']}，"
        f"可用于下游 {keyframes['usable']}/{keyframes['total']}。")
    if keyframes_ready:
        keyframes["status"] = "ready"
        keyframes["reason"] = (
            f"{keyframe_count_text} 全部关键帧均已通过当前合同。")
    elif image_stage_active:
        keyframes["status"] = "active"
        keyframes["reason"] = (
            f"关键帧生产任务正在运行；{keyframe_count_text}")
    elif keyframes["pending"] > 0 or pending_shots:
        keyframes["status"] = "paused"
        keyframes["reason"] = (
            f"{keyframe_count_text} 当前没有运行任务；还有 "
            f"{len(pending_shots) or keyframes['pending']} "
            "个镜头可继续生产。")
    elif keyframes["awaiting_human"] or keyframes["failed"]:
        keyframes["status"] = "blocked"
        keyframes["reason"] = (
            f"{keyframe_count_text} 剩余关键帧均需人工处理，"
            "暂时无法自动进入下一阶段。")
    else:
        keyframes["status"] = "blocked"
        keyframes["reason"] = (
            f"{keyframe_count_text} 未找到完整的关键帧正式资产"
            "或可继续的生产项。")

    keyframe_blockers = []
    if pending_shots or keyframes["pending"]:
        keyframe_blockers.append("keyframes_pending")
    if issue_shots or keyframes["awaiting_human"] or keyframes["failed"]:
        keyframe_blockers.append("keyframes_awaiting_human")

    if not keyframes_ready:
        frames["status"] = "blocked"
        frames["blocked_by"] = keyframe_blockers or ["keyframes_incomplete"]
        frames["reason"] = "关键帧未全部通过并登记，首尾帧生产门禁未开放。"
    elif frames_ready:
        frames["status"] = "ready"
        frames["reason"] = "全部镜头的首帧和尾帧正式资产均已齐全。"
    elif (live_run and (
            frames["generating"] > 0 or frames["retrying"] > 0
            or current_stage in {"frames", "first_frame", "last_frame"})):
        frames["status"] = "active"
        frames["reason"] = "首尾帧生产任务正在运行。"
    elif frames["pending"] > 0:
        frames["status"] = "paused"
        frames["reason"] = "关键帧门禁已通过，可继续生产剩余首尾帧。"
    else:
        frames["status"] = "blocked"
        frames["reason"] = "首尾帧仍有失败或待人工处理项。"

    if not frames_ready:
        videos["status"] = "blocked"
        videos["blocked_by"] = ["frames_incomplete"]
        videos["reason"] = "首尾帧未全部齐全，Seedance 视频生产门禁未开放。"
    elif videos_ready:
        videos["status"] = "ready"
        videos["reason"] = "全部镜头视频正式资产已齐全。"
    elif (live_run and current_stage in {
            "videos", "video", "video_regen", "seedance"}):
        videos["status"] = "active"
        videos["reason"] = "Seedance 视频任务正在运行。"
    else:
        videos["status"] = "paused"
        videos["reason"] = "首尾帧门禁已通过，可开始 Seedance 视频生产。"

    blockers = []
    pending_count = len(pending_shots) or keyframes["pending"]
    issue_count = len(issue_shots) or (
        keyframes["awaiting_human"] + keyframes["failed"])
    if pending_count:
        blockers.append({
            "code": "keyframes_pending",
            "stage": "keyframes",
            "count": pending_count,
            "shot_nos": pending_shots,
            "message": f"还有 {pending_count} 个关键帧尚未生产。",
        })
    if issue_count:
        severity = (
            "mixed" if must_fix_issues and review_issues
            else "must_fix" if must_fix_issues else "review_or_accept")
        blockers.append({
            "code": "keyframes_awaiting_human",
            "stage": "keyframes",
            "count": issue_count,
            "shot_nos": issue_shots,
            "hard_failure": bool(must_fix_issues),
            "severity": severity,
            "must_fix_count": len(must_fix_issues),
            "review_or_accept_count": len(review_issues),
            "message": f"还有 {issue_count} 个关键帧二次质检未通过，需人工处理。",
        })
    if not keyframes_ready:
        current = keyframes
        phase = "keyframes"
    elif not frames_ready:
        current = frames
        phase = "frames"
    elif not videos_ready:
        current = videos
        phase = "videos"
    else:
        current = None
        phase = "review"

    if current is None:
        state = "ready"
        current_step_label = "成片复核"
        headline = "视频资产已齐全，可以进入成片复核"
        reason = "关键帧、首尾帧和视频三道正式资产门禁均已通过。"
        next_action = {
            "action": "review_videos",
            "label": "进入成片复核",
            "count": videos["usable"],
        }
    else:
        state = current["status"]
        current_step_label = {
            "keyframes": "关键帧生产",
            "frames": "首尾帧生产",
            "videos": "Seedance 视频生产",
        }[phase]
        headline = {
            "active": f"{current_step_label}正在进行",
            "paused": f"{current_step_label}已暂停",
            "blocked": f"{current_step_label}被门禁拦截",
            "ready": f"{current_step_label}已完成",
        }[state]
        reason = current["reason"]
        if phase == "keyframes" and pending_count:
            next_action = {
                "action": "resume_keyframes",
                "label": f"继续生产 {pending_count} 个非问题关键帧",
                "count": pending_count,
            }
        elif phase == "keyframes" and issue_count:
            next_action = {
                "action": "resolve_image_issues",
                "label": f"处理 {issue_count} 个问题关键帧",
                "count": issue_count,
            }
        elif phase == "frames":
            next_action = {
                "action": "resume_frames",
                "label": f"继续生产 {frames['remaining']} 组首尾帧",
                "count": frames["remaining"],
            }
        else:
            next_action = {
                "action": "start_videos",
                "label": f"开始生产 {videos['remaining']} 个视频",
                "count": videos["remaining"],
            }

    resume_pending_images = {
        "action": "resume_pending_images",
        "count": pending_count,
        "shot_nos": pending_shots,
        "item_ids": pending_item_ids,
        "enabled": bool(pending_count and not live_run),
        "label": f"继续生产 {pending_count} 个非问题关键帧",
    }
    resolve_image_issues = {
        "action": "resolve_image_issues",
        "count": issue_count,
        "shot_nos": issue_shots,
        "item_ids": issue_item_ids,
        "hard_failure": bool(must_fix_issues),
        "severity": (
            "mixed" if must_fix_issues and review_issues
            else "must_fix" if must_fix_issues
            else "review_or_accept" if review_issues else "none"),
        "must_fix_count": len(must_fix_issues),
        "review_or_accept_count": len(review_issues),
        "items": issue_details,
        "enabled": bool(issue_count),
        "label": (
            f"修正 {len(must_fix_issues)} 个必须修复、"
            f"复核 {len(review_issues)} 个可放行关键帧"
            if issue_count else "没有待处理的问题关键帧"),
    }
    return {
        "state": state,
        "phase": phase,
        "current_step": phase,
        "current_step_label": current_step_label,
        "headline": headline,
        "reason": reason,
        "next_action": next_action,
        "next_actions": {
            "resume_pending_images": resume_pending_images,
            "resolve_image_issues": resolve_image_issues,
        },
        "blockers": blockers,
        "issues": issue_details,
        "actions": {
            "pending_images": {
                **resume_pending_images,
            },
            "resolve_image_issues": {
                **resolve_image_issues,
            },
        },
        "can_start_frames": keyframes_ready,
        "can_start_videos": keyframes_ready and frames_ready,
        "can_confirm_seedance": keyframes_ready and frames_ready,
        "stages": {
            "keyframes": keyframes,
            "frames": frames,
            "videos": videos,
        },
    }


def _episode_status_payload(app, episode_id, jobs):
    """返回适合高频轮询的轻量变更摘要，不加载整集剧本和全部图片合同。"""
    episode = app.projects.get_episode(episode_id)
    if episode is None:
        return None
    project = app.db.query_one(
        "SELECT * FROM projects WHERE id=?", (episode["project_id"],))
    tasks = [dict(row) for row in app.db.query(
        "SELECT id, stage, status, provider, cost, error, updated_at "
        "FROM tasks WHERE episode_id=? ORDER BY id",
        (episode_id,))]
    document_versions = {
        row["kind"]: int(row["version"])
        for row in app.db.query(
            "SELECT kind, MAX(version) AS version FROM documents "
            "WHERE episode_id=? GROUP BY kind ORDER BY kind",
            (episode_id,))
    }
    asset_marker = dict(app.db.query_one(
        "SELECT COUNT(*) AS total, COALESCE(MAX(id), 0) AS max_id, "
        "COALESCE(SUM(version), 0) AS versions, "
        "COALESCE(SUM(reuse_count), 0) AS reuse_count, "
        "COALESCE(SUM(LENGTH(uri) + LENGTH(meta)), 0) AS content_size "
        "FROM assets WHERE project_id=?",
        (project["id"],)))
    out_dir = (app.workspace.artifacts_dir / f"p{project['id']:03d}"
               / f"e{episode['number']:03d}")
    plan_path = out_dir / "render_plan.json"
    try:
        plan_stat = plan_path.stat()
        render_plan_marker = {
            "exists": True,
            "size": plan_stat.st_size,
            "mtime_ns": plan_stat.st_mtime_ns,
        }
    except OSError:
        render_plan_marker = {"exists": False, "size": 0, "mtime_ns": 0}
    relevant_jobs = []
    for job in jobs.list():
        if (job.get("title") != project["title"]
                or int(job.get("episode") or 0) != int(episode["number"])):
            continue
        relevant_jobs.append({
            key: copy.deepcopy(job[key])
            for key in (
                "id", "status", "title", "episode", "started_at",
                "finished_at", "error", "progress",
                "series_advance_error")
            if key in job
        })
    payload = {
        "build": BUILD,
        "episode": dict(episode),
        "project": {
            key: project[key] for key in (
                "id", "title", "status", "kind", "style", "aspect",
                "style_pack_id")
        },
        "tasks": tasks,
        "jobs": relevant_jobs,
        "document_versions": document_versions,
        "asset_marker": asset_marker,
        "render_plan_marker": render_plan_marker,
    }
    signature_source = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    payload["signature"] = hashlib.sha256(signature_source).hexdigest()
    return payload


def _scene3d_payload(app, episode_id):
    """3D 空间查看器数据:blocking 三维调度 + 该场景的 720° 全景母版。

    全景是房间几何真相,blocking 是人和机位在房间里的位置——两者同一
    坐标系(全景中心 yaw0 = blocking +Z),叠在一起才是完整的空间事实。
    """
    episode = app.projects.get_episode(episode_id)
    if episode is None:
        return None
    blocking, blocking_v = app.projects.latest_document(
        episode_id, "blocking")
    if blocking is None:
        return {"episode_id": episode_id, "blocking": None,
                "panoramas": {}, "scene_models": {},
                "render_contracts": {},
                "message": "本集尚未生成空间调度"}
    project_id = episode["project_id"]
    artifacts = app.workspace.artifacts_dir.resolve()
    panoramas = {}
    scene_models = {}
    raw_scenes = (blocking.get("scenes")
                  if isinstance(blocking, dict) else [])
    scenes = raw_scenes if isinstance(raw_scenes, list) else []
    locations = []
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        location = str(scene.get("location") or "").strip()
        if not location:
            continue
        if location not in locations:
            locations.append(location)
        if location not in scene_models:
            model_row = app.assets.latest(
                project_id, "scene_model", location)
            if model_row is not None and model_row["uri"]:
                try:
                    model = json.loads(
                        Path(model_row["uri"]).read_text(encoding="utf-8"))
                    if isinstance(model, dict):
                        scene_models[location] = model
                except (OSError, ValueError, TypeError):
                    pass
        if location not in panoramas:
            row = app.assets.latest(
                project_id, "scene_art", f"{location}::view:panorama")
            if row is None or not row["uri"]:
                continue
            uri = Path(row["uri"]).resolve()
            try:
                rel = uri.relative_to(artifacts)
            except ValueError:
                continue
            panoramas[location] = {
                "url": "/artifacts/" + str(rel),
                "version": row["version"],
            }
    render_contracts = {
        location: build_scene_render_contract(
            scene_models.get(location), panoramas.get(location),
            location=location)
        for location in locations
    }
    storyboard, _sv = app.projects.latest_document(episode_id, "storyboard")
    actions = {}
    for shot in ((storyboard or {}).get("shots") or []):
        try:
            actions[str(int(shot.get("shot_no")))] = {
                "action": str(shot.get("action") or "")[:200],
                "camera": str(shot.get("camera") or "")[:120],
            }
        except (TypeError, ValueError):
            continue
    return {
        "episode_id": episode_id,
        "episode_number": episode["number"],
        "project_title": (app.db.query_one(
            "SELECT title FROM projects WHERE id=?", (project_id,))
            or {"title": ""})["title"],
        "blocking": blocking,
        "blocking_version": blocking_v,
        "panoramas": panoramas,
        "scene_models": scene_models,
        "render_contracts": render_contracts,
        "shot_actions": actions,
    }


def _episode_payload(app, episode_id, jobs=None):
    episode = app.projects.get_episode(episode_id)
    if episode is None:
        return None
    project = app.db.query_one(
        "SELECT * FROM projects WHERE id=?", (episode["project_id"],))
    script, script_v = app.projects.latest_document(episode_id, "script")
    story_analysis, story_analysis_v = app.projects.latest_document(
        episode_id, "story_analysis")
    if script is not None and story_analysis is not None:
        from ..story_analysis import (
            apply_story_analysis,
            build_story_analysis,
        )
        # 详情接口始终做一次幂等内存规范化，不覆盖用户保存的文档版本。
        # 除补齐旧字段外，这也会折叠历史版本中反复拼接的人物出图卡，
        # 让当前项目立即看到简洁视觉提示词。
        story_analysis = build_story_analysis(
            script,
            project["style"] or "",
            raw=story_analysis,
            source=story_analysis.get("source") or "legacy_upgrade",
        )
        script = apply_story_analysis(copy.deepcopy(script), story_analysis)
    storyboard, sb_v = app.projects.latest_document(episode_id, "storyboard")
    # 给每个镜头附上可人工核对的“剧本对应/时代/地点”上下文。这里是
    # API 视图层的只读增强，不改写历史分镜文档，也不会影响已锁定提示词。
    storyboard, shot_story_contexts = attach_shot_story_context(
        script, storyboard)
    continuity, continuity_v = app.projects.latest_document(
        episode_id, "continuity")
    scene_plan, _scene_plan_v = app.projects.latest_document(
        episode_id, "scene_plan")
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
    _character_asset_policy, character_asset_policy_v = \
        app.projects.latest_document(episode_id, "character_asset_policy")
    video_references, video_references_v = app.projects.latest_document(
        episode_id, "video_references")
    shot_revision_state, shot_revision_state_v = \
        app.projects.latest_document(episode_id, "shot_revision_state")
    video_qc_report, video_qc_report_v = \
        app.projects.latest_document(episode_id, "video_qc_report")
    series_source, series_source_v = app.projects.latest_document(
        episode_id, "series_source")
    cast_selection = app.director.production_asset_selection_status(
        project["id"], script or {})
    for character in cast_selection.get("characters", []):
        if character.get("identity_uri"):
            character["identity_url"] = _artifact_url(
                app, character["identity_uri"])
        for candidate in character.get("candidates", []):
            candidate["url"] = _artifact_url(app, candidate.get("uri", ""))
    for prop in cast_selection.get("props", []):
        if prop.get("identity_uri"):
            prop["identity_url"] = _artifact_url(
                app, prop["identity_uri"])
        for candidate in prop.get("candidates", []):
            candidate["url"] = _artifact_url(
                app, candidate.get("uri", ""))
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
    # 视频质检单独落盘，供生产表逐镜显示“已通过 / 自动返工 1/1 /
    # 二次失败待人工”，即使总质检报告尚未生成也能看到视频状态。
    video_qc_path = out_dir / "video_qc_report.json"
    if video_qc_path.exists():
        try:
            video_qc_report = json.loads(
                video_qc_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    if (shot_revision_state or {}).get("active"):
        # 镜头已出新版本而旧成片尚未补拍时，不能继续展示旧质检“通过”。
        qc_report = None
        video_qc_report = None
        content_review = None
    render_plan = None
    plan_path = out_dir / "render_plan.json"
    if plan_path.exists():
        try:
            render_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except ValueError:
            render_plan = None
    relations = None
    relations_path = out_dir / "relations.json"
    if relations_path.exists():
        try:
            relations = json.loads(
                relations_path.read_text(encoding="utf-8"))
        except ValueError:
            relations = None
    image_failures = []
    if render_plan is not None:
        render_plan = copy.deepcopy(render_plan)
        render_plan["items"] = _current_render_plan_items(render_plan)
        for item in render_plan.get("items", []):
            try:
                shot_no = int(item.get("shot_no"))
            except (TypeError, ValueError):
                shot_no = None
            if shot_no in shot_story_contexts:
                item["story_context"] = copy.deepcopy(
                    shot_story_contexts[shot_no])
            for ref in (item.get("reference_inputs") or {}).get("items", []):
                ref["url"] = _artifact_url(app, ref.get("uri", ""))
            output_url = _artifact_url(app, item.get("output_uri", ""))
            if output_url:
                try:
                    stamp = int(Path(item["output_uri"]).stat().st_mtime_ns)
                except (OSError, TypeError, ValueError):
                    stamp = int(render_plan.get("updated_at") or 0)
                separator = "&" if "?" in output_url else "?"
                item["output_url"] = (
                    f"{output_url}{separator}failed={stamp}"
                    if item.get("status") in ("awaiting_human", "failed")
                    else output_url)
            if (item.get("category") == "shot_image"
                    and item.get("status") in ("awaiting_human", "failed")):
                qc = item.get("qc") or {}
                issues = list(qc.get("issues") or [])
                if not issues and item.get("error"):
                    issues = [item["error"]]
                if not issues:
                    # codex 内置 image_gen 等来源的失败常常 issues 双空,
                    # 前端只能显示兜底文案"图片质检未通过"——把诊断里
                    # 已有的原因逐级捞出来,失败必须带可读原因。
                    fallback_reason = next((
                        str(value).strip() for value in (
                            (qc.get("image_error") or {}).get("summary"),
                            qc.get("retry_blocked_reason"),
                            (qc.get("codex_escalation") or {}).get("reason"),
                        ) if str(value or "").strip()), "")
                    if fallback_reason:
                        issues = [fallback_reason]
                image_failures.append({
                    "item_id": _safe_diagnostic_text(
                        item.get("id", ""), limit=160),
                    "shot_no": item.get("shot_no"),
                    "label": _safe_diagnostic_text(
                        item.get("label", ""), limit=240),
                    "status": item.get("status"),
                    "attempts": int(qc.get("attempts") or 0),
                    "consecutive_failures": int(
                        qc.get("consecutive_failures")
                        or qc.get("previous_consecutive_failures")
                        or 0),
                    "qc_provider": _safe_diagnostic_text(
                        qc.get("qc_provider", ""), limit=80),
                    "auto_repairs": int(qc.get("auto_repairs") or max(
                        0, int(qc.get("attempts") or 1) - 1)),
                    "issues": _safe_diagnostic_value(issues) or [],
                    "revision_feedback": _safe_diagnostic_text(
                        qc.get("revision_feedback", "")),
                    "failed_output_url": item.get("output_url"),
                    **_generation_diagnostic_payload(item, qc),
                })
    if isinstance(video_qc_report, dict):
        video_qc_report = copy.deepcopy(video_qc_report)
        for shot_qc in video_qc_report.get("shots", []):
            if isinstance(shot_qc, dict):
                shot_qc.update(_generation_diagnostic_payload(shot_qc))
    video_references_effective = app.director.effective_video_references(
        episode_id)
    for shot in video_references_effective.get("shots", {}).values():
        for item in shot.get("items", []):
            item["url"] = _artifact_url(app, item.get("uri", ""))
    image_acceleration = app.director.image_acceleration_options(
        project["title"], episode["number"])
    if blocking is not None:
        blocking = copy.deepcopy(blocking)
        for scene in blocking.get("scenes", []):
            scene_url = _artifact_url(
                app, scene.get("svg_uri", ""))
            scene["svg_url"] = _versioned(
                scene_url, {"version": blocking_v or 1})
            # 空间调度的 3D 视图直接用本场 720° 全景当房间——调度不再是
            # 抽象线框,而是「真实房间里的人和机位」。全景是几何唯一真相,
            # 与 blocking 同坐标系(全景水平中心 = +Z)。
            location = str(scene.get("location") or "").strip()
            if location:
                row = app.assets.latest(
                    project["id"], "scene_art",
                    f"{location}::view:panorama")
                if row is not None and row["uri"]:
                    scene["panorama_url"] = _versioned(
                        _artifact_url(app, row["uri"]),
                        {"version": row["version"] or 1})
                # 三维场景表:物体的真实位置与尺寸(由全景反解)。有了它,
                # 3D 调度视图画的是真家具而不只是贴图,人物与物件的空间
                # 关系才看得清、镜头才不会穿帮。
                model_row = app.assets.latest(
                    project["id"], "scene_model", location)
                if model_row is not None and model_row["uri"]:
                    try:
                        scene["scene_model"] = json.loads(
                            Path(model_row["uri"]).read_text(
                                encoding="utf-8"))
                    except (OSError, ValueError):
                        pass
    production_progress = _production_progress(
        app, episode, render_plan)
    production_guidance = _production_guidance(
        app, episode, storyboard, render_plan, production_progress)
    payload = {
        "build": BUILD,
        "episode": dict(episode),
        "project": dict(project),
        "tasks": tasks,
        "script": script,
        "script_version": script_v,
        "story_analysis": story_analysis,
        "story_analysis_version": story_analysis_v,
        "storyboard": storyboard,
        "storyboard_version": sb_v,
        "continuity": continuity,
        "scene_plan": scene_plan or {"skipped_scenes": []},
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
        "character_asset_policy": app.director.character_asset_policy(
            episode_id, script=script),
        "character_asset_policy_version": character_asset_policy_v,
        "video_references": video_references or {
            "schema": "aifos.video-references/v1", "shots": {}},
        "video_references_version": video_references_v,
        "video_references_effective": video_references_effective,
        "shot_revision_state": shot_revision_state,
        "shot_revision_state_version": shot_revision_state_v,
        "series_source": series_source,
        "series_source_version": series_source_v,
        "series_batch": app.series.batch_for_episode(episode_id),
        "cast_selection": cast_selection,
        "qc_report": qc_report,
        "video_qc_report": video_qc_report,
        "video_qc_report_version": video_qc_report_v,
        "render_plan": render_plan,
        "production_progress": production_progress,
        "production_guidance": production_guidance,
        "image_failures": image_failures,
        "relations": relations,
        "lessons": project_lessons(app.assets, project["id"], limit=20),
        "image_acceleration": {
            "summary": image_acceleration["summary"],
            "default_provider": image_acceleration["default_provider"],
            "default_model": image_acceleration["default_model"],
            "default_quality": image_acceleration["default_quality"],
        },
        "artifacts": _collect_artifacts(
            app, project["id"], episode["number"]),
    }
    if jobs is not None:
        status = _episode_status_payload(app, episode_id, jobs)
        payload["poll_signature"] = status["signature"]
    return payload


def _overview_payload(app, jobs):
    episodes = [dict(r) for r in app.db.query(
        "SELECT e.id, e.number, e.title, e.status, e.qc_score, e.cost, "
        "e.updated_at, p.title AS project "
        "FROM episodes e JOIN projects p ON p.id=e.project_id "
        "ORDER BY e.updated_at DESC")]
    projects = [dict(r) for r in app.projects.list_projects()]
    asset_stats = {}
    for project in projects:
        rows = [dict(row) for row in app.assets.stats(project["id"])]
        if rows:
            asset_stats[project["title"]] = rows
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
        "projects": projects,
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
        "asset_stats": asset_stats,
        "icloud_sync": app.icloud_sync.status(),
        "firefire": app.firefire.overview(),
        "jobs": jobs.list(),
        "series_batches": app.series.list_batches(),
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
                if route == "/scene3d":
                    return self._file(STATIC_DIR / "scene3d.html",
                                      no_cache=True)
                if route == "/api/scene3d":
                    try:
                        episode_id = int(query.get("episode", ["0"])[0])
                    except (TypeError, ValueError):
                        return self._error(400, "episode 必须是数字")
                    if not episode_id:
                        return self._error(400, "缺少 episode 参数")
                    payload = self._with_app(
                        lambda app: _scene3d_payload(app, episode_id))
                    if payload is None:
                        return self._error(404, "剧集不存在")
                    return self._json(payload)
                if route == "/api/overview":
                    return self._json(self._with_app(
                        lambda app: _overview_payload(app, jobs)))
                if route == "/api/codex/channel-stats":
                    from ..channel_stats import summarize
                    try:
                        hours = float(query.get("hours", ["24"])[0])
                    except (TypeError, ValueError):
                        hours = 24.0
                    return self._json(summarize(
                        Path(workspace) / "logs", hours=hours))
                if route == "/api/qc/failure-stats":
                    from ..qc_stats import summarize_qc
                    try:
                        hours = float(query.get("hours", ["168"])[0])
                    except (TypeError, ValueError):
                        hours = 168.0
                    return self._json(summarize_qc(
                        Path(workspace) / "logs", hours=hours))
                if route == "/api/director/knowledge":
                    # AI 导演知识库:直接读取运行时词典模块——所见即所用,
                    # 不存副本,词典升级这里自动同步。
                    from ..adapters.claude_script import DIRECTOR_PRINCIPLES
                    from ..camera_language import (
                        ANGLE_GEOMETRY, CAMERA_SCALE_CAPACITY,
                        COMPOSITION_GEOMETRY, MOVEMENT_GEOMETRY,
                        POSITION_GEOMETRY, SCALE_GEOMETRY,
                        TOP_DOWN_POSITION_GEOMETRY)
                    from ..lighting_language import (
                        DEEP_FOCUS_CLAUSE, GENRE_LOOKS, LIGHTING_STYLES,
                        NEGATIVE_CONTROLS, SHALLOW_DOF_CLAUSE,
                        VOLUMETRIC_CLAUSE, WARM_COOL_CLAUSE)
                    from ..realism_language import (
                        NEGATIVE_CONTROLS as REALISM_NEGATIVE,
                        NON_REALISTIC_TOKENS, REALISM_CLAUSE,
                        REALISTIC_STYLE_TOKENS)
                    from ..spatial_language import (_DISTANCE_FRAMING,
                                                    _H_ZONES)
                    return self._json({
                        "schema": "aifos.director-knowledge/v1",
                        "source_note": (
                            "以下内容直接读取自运行时词典模块"
                            "(camera_language/lighting_language/"
                            "spatial_language/realism_language),"
                            "AI导演与生成、质检共用同一份——所见即所用"),
                        "principles": list(DIRECTOR_PRINCIPLES),
                        "camera": {
                            "景别": SCALE_GEOMETRY,
                            "角度": ANGLE_GEOMETRY,
                            "机位": POSITION_GEOMETRY,
                            "机位·顶拍语境": TOP_DOWN_POSITION_GEOMETRY,
                            "运镜": MOVEMENT_GEOMETRY,
                            "构图": COMPOSITION_GEOMETRY,
                        },
                        "capacity": CAMERA_SCALE_CAPACITY,
                        "lighting": {
                            "styles": {
                                key: value for key, value in
                                LIGHTING_STYLES.items()},
                            "extras": {
                                "体积光": VOLUMETRIC_CLAUSE,
                                "冷暖对比": WARM_COOL_CLAUSE,
                                "浅景深": SHALLOW_DOF_CLAUSE,
                                "大景深": DEEP_FOCUS_CLAUSE,
                            },
                            "negative": NEGATIVE_CONTROLS,
                        },
                        "genres": {
                            key: {"label": look["label"],
                                  "tokens": list(look["tokens"]),
                                  "style": look["style"],
                                  "grammar": look["grammar"],
                                  "camera": dict(look.get("camera") or {})}
                            for key, look in GENRE_LOOKS.items()},
                        "spatial": {
                            "screen_zones": [
                                {"range": [low, high], "label": label}
                                for low, high, label in _H_ZONES],
                            "distance_framing": [
                                # float('inf') 会被序列化成 Infinity,
                                # 浏览器 JSON.parse 拒收 → 前端拿到空对象
                                {"range": [low,
                                           None if high == float("inf")
                                           else high],
                                 "label": label}
                                for low, high, label in _DISTANCE_FRAMING],
                            "clauses": [
                                "空间站位:画面分区+被摄距离(到主体胸眼"
                                "高度)+由近及远遮挡顺序+朝向视线",
                                "行动路线:起点→终点的分区位移+走近/远离"
                                "摄影机米数+姿态变化",
                                "屏幕方向:180度轴线锁,同场跨镜左右关系"
                                "不得翻转",
                                "空间冲突:声明景别与机位距离跨两档即报,"
                                "出图前拦截",
                            ],
                        },
                        "realism": {
                            "clause": REALISM_CLAUSE,
                            "negative": REALISM_NEGATIVE,
                            "enabled_styles": list(REALISTIC_STYLE_TOKENS),
                            "disabled_styles": list(NON_REALISTIC_TOKENS),
                        },
                    })
                if route == "/api/rules/appeals":
                    from ..rule_appeals import (rules_needing_fix,
                                                summarize_appeals)
                    try:
                        hours = float(query.get("hours", ["168"])[0])
                    except (TypeError, ValueError):
                        hours = 168.0
                    summary = summarize_appeals(
                        Path(workspace) / "logs", hours=hours)
                    summary["needs_rule_fix"] = rules_needing_fix(summary)
                    return self._json(summary)
                if route in ("/api/firefire", "/api/firefire/overview"):
                    return self._json(self._with_app(
                        lambda app: app.firefire.overview()))
                match = re.match(r"^/api/firefire/session/(\d+)$", route)
                if match:
                    payload = self._with_app(lambda app: app.firefire.get_session(
                        int(match.group(1)), include_evidence=True))
                    if payload is None:
                        return self._error(404, "火火学习会话不存在")
                    return self._json(payload)
                match = re.match(r"^/api/firefire/style/([\w-]+)$", route)
                if match:
                    payload = self._with_app(lambda app: app.firefire.get_style(
                        match.group(1)))
                    if payload is None:
                        return self._error(404, "火火独立风格不存在")
                    return self._json(payload)
                match = re.match(
                    r"^/api/firefire/knowledge/([\w-]+)$", route)
                if match:
                    payload = self._with_app(
                        lambda app: app.firefire.knowledge.get(
                            match.group(1)))
                    if payload is None:
                        return self._error(404, "知识条目不存在")
                    return self._json(payload)
                match = re.match(r"^/api/episode/(\d+)/status$", route)
                if match:
                    payload = self._with_app(
                        lambda app: _episode_status_payload(
                            app, int(match.group(1)), jobs))
                    if payload is None:
                        return self._error(404, "剧集不存在")
                    return self._json(payload)
                match = re.match(r"^/api/episode/(\d+)/prompts$", route)
                if match:
                    payload = self._with_app(
                        lambda app: build_episode_prompt_review(
                            app, int(match.group(1))))
                    if payload is None:
                        return self._error(404, "剧集不存在")
                    payload["build"] = BUILD
                    return self._json(payload)
                match = re.match(r"^/api/episode/(\d+)$", route)
                if match:
                    payload = self._with_app(
                        lambda app: _episode_payload(
                            app, int(match.group(1)), jobs=jobs))
                    if payload is None:
                        return self._error(404, "剧集不存在")
                    payload["live_jobs"] = copy.deepcopy(jobs.running_for(
                        payload["project"]["title"],
                        payload["episode"]["number"]))
                    return self._json(payload)
                if route == "/api/assets":
                    return self._assets(query)
                if route == "/api/image_acceleration/options":
                    return self._image_acceleration_options(query)
                if route == "/api/asset-images":
                    return self._asset_images(query)
                if route == "/api/asset-studio/options":
                    return self._asset_studio_options(query)
                if route == "/api/project/shell":
                    return self._project_shell(query)
                if route == "/api/scene/expansion":
                    return self._scene_expansion(query)
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
                if route == "/api/image-production/shards":
                    return self._image_production_shards()
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
                match = re.match(r"^/api/series/(\d+)$", route)
                if match:
                    payload = self._with_app(
                        lambda app: app.series.get_batch(int(match.group(1))))
                    if payload is None:
                        return self._error(404, "多集导入批次不存在")
                    return self._json(payload)
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
                if parsed.path == "/api/firefire/session":
                    return self._firefire_session()
                if parsed.path == "/api/firefire/evidence":
                    return self._firefire_evidence()
                if parsed.path == "/api/firefire/analyse":
                    return self._firefire_analyse()
                if parsed.path == "/api/firefire/style":
                    return self._firefire_style()
                if parsed.path == "/api/firefire/style/publish":
                    return self._firefire_style_publish()
                if parsed.path == "/api/firefire/style/archive":
                    return self._firefire_style_archive()
                if parsed.path == "/api/firefire/validation":
                    return self._firefire_validation()
                if parsed.path == "/api/firefire/knowledge":
                    return self._firefire_knowledge()
                if parsed.path == "/api/firefire/knowledge/publish":
                    return self._firefire_knowledge_publish()
                if parsed.path == "/api/firefire/knowledge/refresh":
                    return self._firefire_knowledge_refresh()
                if parsed.path == "/api/firefire/knowledge/resolve":
                    return self._firefire_knowledge_resolve()
                if parsed.path == "/api/series/preview":
                    return self._series_preview()
                if parsed.path == "/api/series/import":
                    return self._series_import()
                if parsed.path == "/api/series/next":
                    return self._series_next()
                if parsed.path == "/api/series/settings":
                    return self._series_settings()
                if parsed.path == "/api/confirm":
                    return self._confirm()
                if parsed.path == "/api/character/select":
                    return self._character_select()
                if parsed.path == "/api/prop/select":
                    return self._prop_select()
                if parsed.path == "/api/character/assets-policy":
                    return self._character_assets_policy()
                if parsed.path == "/api/character/regenerate":
                    return self._character_regenerate()
                if parsed.path == "/api/character/refine-prompt":
                    return self._character_refine_prompt()
                if parsed.path == "/api/character/refine-prompt/apply":
                    return self._character_refine_apply()
                if parsed.path == "/api/shot/direct":
                    return self._shot_direct()
                if parsed.path == "/api/shot/ai-direct":
                    return self._shot_ai_direct()
                if parsed.path == "/api/scene/skip":
                    return self._scene_skip()
                if parsed.path == "/api/scene/delete":
                    return self._scene_delete()
                if parsed.path == "/api/scene/expand":
                    return self._scene_expand()
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
                if parsed.path == "/api/icloud-sync/backfill":
                    return self._icloud_sync_backfill()
                if parsed.path == "/api/update":
                    return self._update_now()
                if parsed.path == "/api/redo_mock":
                    return self._redo_mock()
                if parsed.path == "/api/qc_item":
                    return self._qc_item()
                if parsed.path == "/api/qc_override":
                    return self._qc_override()
                if parsed.path == "/api/qc_all":
                    return self._qc_all()
                if parsed.path == "/api/redo_items":
                    return self._redo_items()
                if parsed.path == "/api/redo_video":
                    return self._redo_video()
                if parsed.path == "/api/image_acceleration/preflight":
                    return self._image_acceleration_preflight()
                if parsed.path == "/api/image_acceleration/queue":
                    return self._image_acceleration_queue()
                if parsed.path == "/api/restyle":
                    return self._restyle()
                if parsed.path == "/api/reference/upload":
                    return self._reference_upload()
                if parsed.path == "/api/reference/delete":
                    return self._reference_delete()
                if parsed.path == "/api/asset/delete":
                    return self._asset_delete()
                if parsed.path == "/api/asset-studio/prompt":
                    return self._asset_studio_prompt()
                if parsed.path == "/api/asset-studio/generate":
                    return self._asset_studio_generate()
                if parsed.path == "/api/asset-studio/copy":
                    return self._asset_studio_copy()
                if parsed.path == "/api/history/delete":
                    return self._history_delete()
                if parsed.path == "/api/video/references":
                    return self._video_references()
                if parsed.path == "/api/lessons/approve":
                    return self._lesson_approval()
                if parsed.path == "/api/project/style":
                    return self._project_style()
                if parsed.path == "/api/story-analysis":
                    return self._story_analysis()
                if parsed.path == "/api/project/rename":
                    return self._project_rename()
                if parsed.path == "/api/project/create":
                    return self._project_create()
                if parsed.path == "/api/project/delete":
                    return self._project_delete()
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

        def _asset_images(self, query):
            """资产中心图片索引：按作品读取当前有效版本及完整溯源。"""
            title = query.get("project", [""])[0].strip()
            if not title:
                return self._error(400, "缺少 project")

            def fetch(app):
                project = app.projects.get_project(title)
                if project is None:
                    return None
                return _image_asset_catalog(app, project["id"])

            items = self._with_app(fetch)
            if items is None:
                return self._error(404, f"项目不存在: {title}")
            return self._json({"project": title, "items": items})

        def _asset_studio_options(self, query):
            """资产工坊表单选项:资产类型、用途、可勾选参考图、可关联对象。"""
            title = query.get("project", [""])[0].strip()
            if not title:
                return self._error(400, "缺少 project")
            try:
                return self._json(self._with_app(
                    lambda app: app.director.studio_asset_options(title)))
            except Exception as exc:
                return self._error(400, str(exc))

        @staticmethod
        def _asset_studio_reference_ids(body):
            values = body.get("reference_asset_ids")
            if values in (None, ""):
                return []
            if not isinstance(values, list):
                raise AifosError("reference_asset_ids 必须是数组")
            return values

        def _asset_studio_prompt(self):
            """AI 代写自建资产的出图提示词(只出文本,用户可再改)。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            title = (body.get("project") or "").strip()
            if not title:
                return self._error(400, "缺少 project")
            try:
                refs = self._asset_studio_reference_ids(body)
                result = self._with_app(lambda app: app.director.studio_prompt(
                    title, body.get("asset_type", ""),
                    name=(body.get("name") or ""),
                    brief=(body.get("brief") or ""),
                    current_prompt=(body.get("prompt") or ""),
                    feedback=(body.get("feedback") or ""),
                    reference_asset_ids=refs,
                    style=body.get("style")))
            except Exception as exc:
                return self._error(400, str(exc))
            return self._json(result)

        def _asset_studio_generate(self):
            """生产自建资产图片并登记进资产中心(可直接用于后续制作)。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            title = (body.get("project") or "").strip()
            if not title:
                return self._error(400, "缺少 project")
            try:
                refs = self._asset_studio_reference_ids(body)
                result = self._with_app(
                    lambda app: app.director.generate_studio_asset(
                        title, body.get("asset_type", ""),
                        (body.get("name") or ""), (body.get("prompt") or ""),
                        negative_prompt=(body.get("negative_prompt") or ""),
                        feedback=(body.get("feedback") or ""),
                        attach_to=(body.get("attach_to") or ""),
                        reference_role=(body.get("reference_role") or ""),
                        note=(body.get("note") or ""),
                        reference_asset_ids=refs,
                        base_asset_id=body.get("base_asset_id"),
                        aspect=(body.get("aspect") or ""),
                        style=body.get("style"),
                        count=body.get("count", 1),
                        optimize_prompt=bool(body.get("optimize_prompt")),
                        prompt_source=(body.get("prompt_source") or "manual")))
            except Exception as exc:
                return self._error(400, str(exc))
            return self._json(result, status=201)

        def _asset_studio_copy(self):
            """把自建资产复用到另一部作品(共用同一张图)。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            title = (body.get("project") or "").strip()
            target = (body.get("target_project") or "").strip()
            if not title or not target:
                return self._error(400, "缺少 project/target_project")
            if body.get("asset_id") is None:
                return self._error(400, "缺少 asset_id")
            try:
                result = self._with_app(
                    lambda app: app.director.copy_studio_asset(
                        title, body["asset_id"], target))
            except Exception as exc:
                return self._error(400, str(exc))
            return self._json(result, status=201)

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

        def _series_request(self, body):
            filename = str(body.get("filename") or "").strip()
            encoded = body.get("data_base64")
            if not filename or not isinstance(encoded, str) or not encoded:
                raise AifosError("请选择要导入的剧本文档")
            if len(encoded) > 28 * 1024 * 1024:
                raise AifosError("文档超过 20MB；请按卷拆分后导入")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise AifosError("文档数据不是有效的 Base64") from exc

            def prepare(app):
                start = body.get("start_episode")
                try:
                    start = int(start) if start not in (None, "") else None
                except (TypeError, ValueError) as exc:
                    raise AifosError("起始集数必须是正整数") from exc
                title, number, note = resolve_produce_target(
                    app, body.get("sentence", ""),
                    title=(body.get("title") or None), number=start)
                from ..series_import import (
                    SeriesImportError, parse_series_document)
                try:
                    parsed = parse_series_document(
                        filename, raw, title, start_number=number)
                except SeriesImportError as exc:
                    raise AifosError(str(exc)) from exc
                project = app.projects.get_project(title)
                conflicts = []
                if project is not None:
                    numbers = [item["episode_number"]
                               for item in parsed["episodes"]]
                    placeholders = ",".join("?" for _ in numbers)
                    conflicts = [row["number"] for row in app.db.query(
                        f"SELECT number FROM episodes WHERE project_id=? "
                        f"AND number IN ({placeholders}) ORDER BY number",
                        (project["id"], *numbers))]
                return parsed, note, conflicts

            return self._with_app(prepare)

        def _series_preview(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            try:
                parsed, note, conflicts = self._series_request(body)
            except AifosError as exc:
                return self._error(400, str(exc))
            from ..series_import import preview_payload
            payload = preview_payload(parsed)
            payload["note"] = note
            payload["conflicts"] = conflicts
            payload["can_import"] = not conflicts
            return self._json(payload)

        def _series_import(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            try:
                parsed, note, conflicts = self._series_request(body)
                if conflicts:
                    joined = "、".join(f"第{number}集" for number in conflicts)
                    return self._error(
                        409, f"以下剧集已经存在，未覆盖任何内容：{joined}；"
                        "请调整起始集数后重试")
                style_pack_id = str(body.get("style_pack_id") or "").strip()
                selected_style = body.get("style", "")
                if style_pack_id:
                    pack = self._with_app(lambda app: app.firefire.get_style(
                        style_pack_id, approved_only=True))
                    if pack is None:
                        return self._error(409, "所选火火独立风格不存在或尚未人工确认")
                    selected_style = pack["compiled_style"]
                batch = self._with_app(
                    lambda app: app.series.import_batch(
                        parsed, style=selected_style,
                        style_pack_id=style_pack_id,
                        kind=(body.get("kind")
                              if body.get("kind") in ("drama", "idol")
                              else None),
                        auto_advance=body.get("auto_advance", True)))
                step = None
                job_id = None
                if body.get("start_first", True):
                    step = self._with_app(
                        lambda app: app.series.activate_next(batch["id"]))
                    job_id = jobs.start_series_step(step)
                    if isinstance(step, dict):
                        step = {
                            key: value for key, value in step.items()
                            if key != "source_script"}
                    batch = self._with_app(
                        lambda app: app.series.get_batch(batch["id"]))
            except AifosError as exc:
                return self._error(400, str(exc))
            return self._json({
                "batch": batch,
                "first": step,
                "job_id": job_id,
                "note": note,
            }, status=201)

        def _series_next(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            try:
                batch_id = int(body.get("batch_id"))
            except (TypeError, ValueError):
                return self._error(400, "缺少有效 batch_id")
            try:
                step = self._with_app(
                    lambda app: app.series.activate_next(batch_id))
                job_id = jobs.start_series_step(step)
                if isinstance(step, dict):
                    step = {
                        key: value for key, value in step.items()
                        if key != "source_script"}
            except AifosError as exc:
                return self._error(409, str(exc))
            return self._json({"step": step, "job_id": job_id}, status=202)

        def _series_settings(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            try:
                batch_id = int(body.get("batch_id"))
            except (TypeError, ValueError):
                return self._error(400, "缺少有效 batch_id")
            try:
                batch = self._with_app(
                    lambda app: app.series.set_auto_advance(
                        batch_id, bool(body.get("auto_advance"))))
            except AifosError as exc:
                return self._error(404, str(exc))
            return self._json(batch)

        def _firefire_session(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            try:
                result = self._with_app(lambda app: app.firefire.create_session(
                    name=body.get("name", ""),
                    source_url=body.get("source_url", ""),
                    source_type=body.get("source_type", "url"),
                    rights_confirmed=bool(body.get("rights_confirmed")),
                    notes=body.get("notes", "")))
            except AifosError as exc:
                return self._error(400, str(exc))
            return self._json(result, status=201)

        def _firefire_evidence(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            try:
                session_id = int(body.get("session_id"))
            except (TypeError, ValueError):
                return self._error(400, "缺少有效 session_id")
            try:
                result = self._with_app(lambda app: app.firefire.add_evidence(
                    session_id, kind=body.get("kind", "frame"),
                    label=body.get("label", ""), uri=body.get("uri", ""),
                    timecode=body.get("timecode", ""),
                    observation=body.get("observation", ""),
                    meta=body.get("meta") or {}))
            except AifosError as exc:
                return self._error(400, str(exc))
            return self._json(result, status=201)

        def _firefire_analyse(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            try:
                session_id = int(body.get("session_id"))
            except (TypeError, ValueError):
                return self._error(400, "缺少有效 session_id")
            try:
                result = self._with_app(
                    lambda app: app.firefire.start_analysis(session_id))
            except AifosError as exc:
                return self._error(409, str(exc))
            return self._json(result)

        def _firefire_style(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            try:
                session_id = body.get("session_id")
                session_id = int(session_id) if session_id not in (None, "") else None
                result = self._with_app(lambda app: app.firefire.create_style(
                    name=body.get("name", ""), session_id=session_id,
                    summary=body.get("summary", ""),
                    compiled_style=body.get("compiled_style", ""),
                    positive_prompt=body.get("positive_prompt", ""),
                    negative_prompt=body.get("negative_prompt", ""),
                    references=body.get("references") or [],
                    validation=body.get("validation") or {},
                    director_knowledge=body.get("director_knowledge") or {}))
            except (AifosError, ValueError) as exc:
                return self._error(400, str(exc))
            return self._json(result, status=201)

        def _firefire_style_publish(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            style_id = str(body.get("style_id") or "").strip()
            if not style_id:
                return self._error(400, "缺少 style_id")
            try:
                result = self._with_app(lambda app: app.firefire.publish_style(
                    style_id, approved_by=body.get("approved_by", "human"),
                    feedback=body.get("feedback", "")))
            except AifosError as exc:
                return self._error(409, str(exc))
            return self._json(result)

        def _firefire_style_archive(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            style_id = str(body.get("style_id") or "").strip()
            if not style_id:
                return self._error(400, "缺少 style_id")
            try:
                result = self._with_app(
                    lambda app: app.firefire.archive_style(style_id))
            except AifosError as exc:
                return self._error(409, str(exc))
            return self._json(result)

        def _firefire_validation(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            try:
                session_id = body.get("session_id")
                style_id = body.get("style_id")
                result = self._with_app(
                    lambda app: app.firefire.create_validation_task(
                        title=body.get("title", ""),
                        prompt=body.get("prompt", ""),
                        session_id=(int(session_id)
                                    if session_id not in (None, "") else None),
                        style_id=(str(style_id).strip()
                                  if style_id else None),
                        references=body.get("references") or []))
            except (AifosError, ValueError) as exc:
                return self._error(400, str(exc))
            return self._json(result, status=201)

        def _firefire_knowledge(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            try:
                result = self._with_app(
                    lambda app: app.firefire.create_knowledge(body))
            except AifosError as exc:
                return self._error(400, str(exc))
            return self._json(result, status=201)

        def _firefire_knowledge_publish(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            knowledge_key = str(
                body.get("knowledge_key") or "").strip()
            if not knowledge_key:
                return self._error(400, "缺少 knowledge_key")
            try:
                result = self._with_app(
                    lambda app: app.firefire.publish_knowledge(
                        knowledge_key,
                        approved_by=body.get("approved_by", "human"),
                        note=body.get("note", "")))
            except AifosError as exc:
                return self._error(409, str(exc))
            return self._json(result)

        def _firefire_knowledge_refresh(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            knowledge_key = str(
                body.get("knowledge_key") or "").strip()
            if not knowledge_key:
                return self._error(400, "缺少 knowledge_key")
            try:
                result = self._with_app(
                    lambda app: app.firefire.refresh_knowledge(
                        knowledge_key))
            except AifosError as exc:
                return self._error(409, str(exc))
            return self._json(result, status=201)

        def _firefire_knowledge_resolve(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            try:
                result = self._with_app(
                    lambda app: app.firefire.resolve_knowledge(
                        stage=body.get("stage", ""),
                        task_type=body.get("task_type", ""),
                        query=body.get("query", ""),
                        tags=body.get("tags") or [],
                        limit=body.get("limit", 4)))
            except (AifosError, TypeError, ValueError) as exc:
                return self._error(400, str(exc))
            return self._json(result)

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
                from ..script_import import (
                    NoDialogueError, ScriptImportError, parse_any)
                try:
                    script = parse_any(
                        body["script_text"], title, int(number))
                except NoDialogueError:
                    # 纯叙述/故事梗概没有可逐字抽取的对白——这不是用户
                    # 的错,自动转 AI 编剧:原文全文作为剧情素材做影视化
                    # 改编(台词由编剧补写),剧本仍停在等待确认关口人工审。
                    source = str(body["script_text"]).strip()[:6000]
                    premise = str(body.get("premise") or "").strip()
                    body["premise"] = (
                        (premise + "\n\n" if premise else "")
                        + "【剧情素材,按影视化改编规则处理】\n" + source)
                    note = ((note + ";") if note else "") + (
                        "未识别到对白,已自动转 AI 编剧改编;"
                        "剧本生成后会停在「等待确认」供你审阅")
                except ScriptImportError as exc:
                    return self._error(400, str(exc))
            style_pack_id = str(body.get("style_pack_id") or "").strip()
            selected_style = body.get("style", "")
            if style_pack_id:
                pack = self._with_app(lambda app: app.firefire.get_style(
                    style_pack_id, approved_only=True))
                if pack is None:
                    return self._error(409, "所选火火独立风格不存在或尚未人工确认")
                selected_style = pack["compiled_style"]
            # Web 端默认走「预生产 → 确认 → 自动生产」流程
            job_id = jobs.start(
                title, int(number),
                premise=body.get("premise", ""),
                style=selected_style,
                style_pack_id=style_pack_id,
                force=bool(body.get("force")),
                script=script,
                review=bool(body.get("review", True)),
                kind=body.get("kind")
                if body.get("kind") in ("drama", "idol") else None,
                action=("force_rebuild" if body.get("force") else
                        "script_import" if script is not None else
                        "produce"),
                auto_select_assets=bool(
                    body.get("auto_select_assets",
                             not bool(body.get("review", True)))),
                fresh_assets=bool(
                    body.get("fresh_assets", body.get("force", False))),
                unique=True)
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
            running = jobs.running_for(title, number)
            if running:
                return self._json({
                    "job_id": running[0]["id"], "phase": status,
                    "reused": True,
                    "note": "本集已经在生产，无需重复确认",
                }, status=202)
            allowed = {"awaiting_script", "awaiting_cast",
                       "awaiting_confirm"}
            if status not in allowed:
                return self._error(
                    409, f"本集当前处于「{status}」，不能重复确认；"
                    "请刷新查看当前生产进度")
            if status == "awaiting_script":
                try:
                    self._with_app(
                        lambda app: app.director.lock_story_analysis(
                            found_episode_id))
                except AifosError as exc:
                    return self._error(409, str(exc))
            revision_state = self._with_app(
                lambda app: app.projects.latest_document(
                    found_episode_id, "shot_revision_state")[0])
            if (status == "awaiting_confirm"
                    and (revision_state or {}).get("active")
                    and not (revision_state or {}).get(
                        "formal_ready", True)):
                return self._error(
                    409, "当前修订镜头是低质量试错图；请先在分镜表用中/高质量"
                    "重画，低质量图不能交给 Seedance")
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
                    lambda app: app.director.production_asset_selection_status(
                        project_id,
                        app.projects.latest_document(
                            found_episode_id, "script")[0] or {}))
                if not selection.get("passed"):
                    return self._error(
                        409, "请先为每名正式角色选定最终立绘，并为每件核心道具"
                        "选定最终图，再继续生产")
            # 剧本确认 → 继续预生产(画完人物/分镜再停一次);
            # 开拍确认 → 自动完成视频/配音/剪辑/质检
            job_id = jobs.start(
                title, number,
                review=(status in ("awaiting_script", "awaiting_cast")),
                action=("confirm_script" if status == "awaiting_script"
                        else "confirm_cast" if status == "awaiting_cast"
                        else "confirm_preflight"),
                unique=True)
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

        def _prop_select(self):
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            name = str(body.get("prop") or "").strip()
            index = body.get("candidate_index")
            if not name or index is None:
                return self._error(400, "缺少 prop/candidate_index")
            title, number = found
            try:
                result = self._with_app(
                    lambda app: app.director.select_prop_candidate(
                        title, number, name, int(index)))
            except (AifosError, TypeError, ValueError) as exc:
                return self._error(400, str(exc))
            return self._json(result)

        def _character_assets_policy(self):
            """保存本集人物扩展资产的自动/简化/完整选择。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            mode = str(body.get("mode") or "").strip().lower()
            if mode not in ("auto", "simple", "full"):
                return self._error(
                    400, "mode 需为 auto、simple 或 full")
            expected_version = body.get("expected_version")
            if (isinstance(expected_version, bool)
                    or not isinstance(expected_version, int)
                    or expected_version < 0):
                return self._error(
                    400, "expected_version 需为页面返回的非负整数，请刷新后重试")
            title, number = found
            if jobs.running_for(title, number):
                return self._error(
                    409, "本集正在生产，不能中途切换人物资产模式；请先停止生成")
            try:
                policy = self._with_app(
                    lambda app: app.director.update_character_asset_policy(
                        int(body["episode_id"]), mode,
                        expected_version=expected_version))
            except AifosError as exc:
                return self._error(409, str(exc))
            return self._json({
                "policy": policy, "version": policy["version"]})

        def _character_regenerate(self):
            """放弃人物定版并重新生成候选,不绕过人物选择门禁。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            character_name = str(body.get("character") or "").strip()
            prop_name = str(body.get("prop") or "").strip()
            if character_name and prop_name:
                return self._error(400, "character 与 prop 只能指定一个")
            if jobs.running_for(title, number):
                return self._error(
                    409, "本集正在生产，请先暂停，待状态稳定后再重做人物候选")
            job_id = jobs.start_task(
                title, number,
                lambda app, run_id: app.director.regenerate_character_candidates(
                    title, number, run_id=run_id,
                    character_name=character_name,
                    prop_name=prop_name),
                action="regenerate_cast",
                request={
                    "reason": "manual_regenerate_cast",
                    "character": character_name,
                    "prop": prop_name,
                })
            return self._json({"job_id": job_id}, status=202)

        def _character_refine_prompt(self):
            """用户意见 → AI 深度改写人物形象提示词(仅预览,不出图)。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            character = str(body.get("character") or "").strip()
            feedback = str(body.get("feedback") or "").strip()
            if not character:
                return self._error(400, "缺少 character")
            if not feedback:
                return self._error(400, "请填写修改意见")
            job_id = jobs.start_task(
                title, number,
                lambda app, run_id: app.director.refine_character_prompt(
                    title, number, character, feedback),
                action="refine_character_prompt",
                request={"character": character, "feedback": feedback})
            return self._json({"job_id": job_id}, status=202)

        def _character_refine_apply(self):
            """确认改写后的提示词:写入人物设定并重生成四张候选。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            character = str(body.get("character") or "").strip()
            prompt = str(body.get("image_prompt") or "").strip()
            if not character:
                return self._error(400, "缺少 character")
            if not prompt:
                return self._error(400, "缺少确认后的 image_prompt")
            if jobs.running_for(title, number):
                return self._error(
                    409, "本集正在生产，请先暂停，待状态稳定后再改人物形象")
            job_id = jobs.start_task(
                title, number,
                lambda app, run_id: app.director.apply_character_prompt(
                    title, number, character, prompt,
                    feedback=str(body.get("feedback") or ""),
                    run_id=run_id),
                action="regenerate_cast",
                request={"reason": "refine_character_prompt_apply",
                         "character": character})
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

        def _shot_ai_direct(self):
            """AI 导演审单镜:{episode_id, shot_no}。同步返回建议
            (镜头五维/光影/意见/理由),由前端填入导演台供人审;
            不直接改分镜(apply 由人按「按导演指令重画」触发)。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            try:
                shot_no = int(body.get("shot_no"))
            except (TypeError, ValueError):
                return self._error(400, "缺少 shot_no")
            try:
                suggestion = self._with_app(
                    lambda app: app.director.ai_direct_shot(
                        title, number, shot_no,
                        apply=bool(body.get("apply"))))
            except AifosError as exc:
                return self._error(400, str(exc))
            return self._json(suggestion)

        def _shot_direct(self):
            """导演台:{episode_id, shot_no, camera{五维}, lighting_style,
            note, clear_note, redo}。指令写进分镜;redo=true 且产线空闲时
            顺手把该镜加入重画队列。生产运行中只写指令不重画。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            try:
                shot_no = int(body.get("shot_no"))
            except (TypeError, ValueError):
                return self._error(400, "缺少 shot_no")
            try:
                result = self._with_app(
                    lambda app: app.director.direct_shot(
                        title, number, shot_no,
                        camera=body.get("camera"),
                        lighting_style=str(body.get("lighting_style") or ""),
                        note=str(body.get("note") or ""),
                        clear_note=bool(body.get("clear_note"))))
            except AifosError as exc:
                return self._error(400, str(exc))
            result["redo_queued"] = False
            if body.get("redo"):
                if jobs.production_running_for(title, number):
                    result["redo_note"] = (
                        "生产运行中:指令已保存,恢复后重画本镜生效;"
                        "或先暂停再点重画")
                else:
                    # 重画是分钟级长任务,必须走后台任务通道,
                    # 不能在请求线程里同步跑
                    job_id = jobs.start_task(
                        title, number,
                        lambda app, run_id, report: app.director.redo_items(
                            title, number,
                            item_ids=[f"shot:{shot_no}"],
                            progress=report),
                        action="redo_items", tracked=True,
                        request={"item_ids": [f"shot:{shot_no}"],
                                 "source": "director_console"})
                    result["redo_queued"] = True
                    result["job_id"] = job_id
            return self._json(result)

        def _scene_skip(self):
            """场次「暂不生成/恢复生成」:{title/episode_id, scene_no, skip}。
            随时可切换,下次(恢复)生产时生效;单场跑通全流程测试用。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            try:
                scene_no = int(body.get("scene_no"))
            except (TypeError, ValueError):
                return self._error(400, "缺少 scene_no")
            try:
                result = self._with_app(
                    lambda app: app.director.set_scene_generation(
                        title, number, scene_no,
                        bool(body.get("skip", True))))
            except AifosError as exc:
                return self._error(400, str(exc))
            return self._json(result)

        def _scene_delete(self):
            """删除一场并级联:剧本/分镜去场、镜头产物软删、
            连续性圣经下次生产自动按新剧本重建。生产运行中禁止。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            try:
                scene_no = int(body.get("scene_no"))
            except (TypeError, ValueError):
                return self._error(400, "缺少 scene_no")
            if jobs.production_running_for(title, number):
                return self._error(
                    409, "本集正在生产，请先点「停止生成」，"
                         "待状态稳定后再删除场次")
            try:
                result = self._with_app(
                    lambda app: app.director.delete_scene(
                        title, number, scene_no))
            except AifosError as exc:
                return self._error(400, str(exc))
            return self._json(result)

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
            # 重写会 force 重跑预生产,与正在跑的整集生产互斥;
            # 先暂停再提交,避免两个任务同时改同一份 render_plan。
            if jobs.production_running_for(title, number):
                return self._error(
                    409, "本集正在生产，请先点「停止生成」，"
                         "待状态稳定后再提交剧本重写")
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
                                          "frames", "first_frame",
                                          "last_frame"):
                return self._error(400, "target.kind 需为 character_art/"
                                        "scene_art/shot/character_sheet/"
                                        "frames/first_frame/last_frame")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            # 整集生产时并行 worker 正在改整份 render_plan,改单张不安全,
            # 仍要求先暂停。但"另一张图正在重画"不该拦住这一张——排队即可,
            # 否则用户改完一张必须盯着等它跑完才能提交下一张。
            if jobs.production_running_for(title, number):
                return self._error(
                    409, "本集正在生产，请先暂停，待状态稳定后再修改镜头")
            feedback = (body.get("feedback") or "").strip()
            prompt = (body.get("prompt") or "").strip()
            quality = body.get("quality")
            if quality is not None:
                try:
                    quality = normalize_quality(
                        quality, allow_auto=True, field="image_quality")
                except AifosError as exc:
                    return self._error(400, str(exc))
            # Codex 判「改合同/拆镜/人工」时默认熔断；前端确认合同已修好后
            # 带 escalation_override 再来，才放行这一张。
            override = bool(body.get("escalation_override"))
            job_id = jobs.start_task(
                title, number,
                lambda app, run_id: app.director.regen_image(
                    title, number, target, feedback=feedback,
                    prompt_override=prompt, quality_override=quality,
                    escalation_override=override),
                action="regen_image",
                request={"target": target, "feedback": feedback,
                         "prompt": prompt, "quality": quality,
                         "escalation_override": override},
                # 不能用 unique:那会把第二张的请求并进第一张的 job,
                # 前端轮询到第一张成功就以为改好了,第二张其实从未重画。
                queue=True)
            job = jobs.get(job_id) or {}
            return self._json({
                "job_id": job_id,
                "status": job.get("status") or "running",
                "queue_position": job.get("queue_position") or 0,
            }, status=202)

        def _settings_update(self):
            """设置中心保存 Provider、能力路由或整套图片策略。"""
            from ..settings import set_icloud_sync, set_image_strategy, \
                set_codex_profiles, set_routing, settings_payload, \
                update_provider
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")

            def task(app):
                if body.get("codex_profiles") is not None:
                    set_codex_profiles(app.workspace.config_path,
                                       body.get("codex_profiles"))
                elif body.get("provider"):
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
                elif body.get("icloud_sync") is not None:
                    values = body.get("icloud_sync") or {}
                    if "enabled" not in values:
                        raise AifosError("icloud_sync 缺少 enabled")
                    set_icloud_sync(app.workspace.config_path,
                                    values["enabled"])
                elif body.get("capability"):
                    chain = body.get("chain") or []
                    if isinstance(chain, str):
                        chain = [c.strip() for c in chain.split(",")
                                 if c.strip()]
                    set_routing(app.workspace.config_path,
                                body["capability"], chain)
                else:
                    raise AifosError(
                        "缺少 provider、capability 或 icloud_sync")

            try:
                self._with_app(task)
            except AifosError as exc:
                return self._error(400, str(exc))
            # 重新加载,回传保存后的完整视图
            return self._json(self._with_app(settings_payload))

        def _image_production_shards(self):
            """返回图片任务按 Codex profile 的只读分片快照。

            生产线程把 profile id 写入 render_plan 条目；这里仅汇总已有
            清单，不启动任何 Provider，也不读取 CODEX_HOME 下的认证文件。
            没有新任务时返回两个可用但空闲的通道，前端仍能明确看到配置
            已生效，而不是把 404 误判成设置未保存。
            """
            def collect(app):
                profiles = app.config.codex_profiles()
                rows = {profile["id"]: {
                    "id": profile["id"],
                    "name": profile["name"],
                    "assigned": 0,
                    "running": 0,
                    "completed": 0,
                    "failed": 0,
                    "active_jobs": [],
                } for profile in profiles}
                jobs_seen = set()
                for job in jobs.list():
                    if job.get("status") not in ("running", "done", "failed"):
                        continue
                    try:
                        row = app.db.query_one(
                            "SELECT p.id AS project_id, e.id AS episode_id "
                            "FROM projects p JOIN episodes e "
                            "ON e.project_id=p.id "
                            "WHERE p.title=? AND e.number=?",
                            (job.get("title"), int(job.get("episode"))),
                        )
                    except (TypeError, ValueError):
                        row = None
                    if row is None:
                        continue
                    plan_path = (app.workspace.artifacts_dir
                                 / f"p{int(row['project_id']):03d}"
                                 / f"e{int(job['episode']):03d}"
                                 / "render_plan.json")
                    try:
                        plan = json.loads(plan_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    for item in plan.get("items", []):
                        profile_id = str(item.get("codex_profile") or "")
                        target = rows.get(profile_id)
                        if target is None or item.get("category") not in (
                                "character_art", "character_sheet",
                                "scene_art", "shot_image", "first_frame",
                                "last_frame", "cover"):
                            continue
                        item_key = f"{job.get('id')}:{item.get('id')}"
                        if item_key in jobs_seen:
                            continue
                        jobs_seen.add(item_key)
                        target["assigned"] += 1
                        status = item.get("status") or "pending"
                        if status == "generating":
                            target["running"] += 1
                        elif status in ("done", "reused"):
                            target["completed"] += 1
                        elif status in ("failed", "awaiting_human"):
                            target["failed"] += 1
                        if status == "generating":
                            target["active_jobs"].append({
                                "id": item.get("id", ""),
                                "status": status,
                                "error": item.get("error", ""),
                            })
                for target in rows.values():
                    finished = target["completed"] + target["failed"]
                    target["progress"] = (
                        finished / target["assigned"]
                        if target["assigned"] else 0)
                return {
                    "shards": list(rows.values()),
                    "codex_parallel": app.config.codex_parallel_status(),
                }
            return self._json(self._with_app(collect))

        def _icloud_sync_backfill(self):
            report = self._with_app(lambda app: app.icloud_sync.backfill())
            if report.get("status") == "disabled":
                return self._error(409, "请先启用 iCloud 图片同步")
            return self._json(report)

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

        def _story_analysis(self):
            """保存、锁定或重新运行剧本 AI 制作圣经。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            episode_id = int(body["episode_id"])
            action = str(body.get("action") or "save").strip()
            if action == "reanalyze":
                if jobs.running_for(title, number):
                    return self._error(409, "本集已有任务正在运行")
                direction = str(
                    body.get("creative_direction") or "").strip()
                job_id = jobs.start_task(
                    title, number,
                    lambda app, run_id: app.director.reanalyze_story(
                        title, number, direction, run_id=run_id),
                    action="reanalyze_story",
                    request={"creative_direction": direction},
                    unique=True)
                return self._json({"job_id": job_id}, status=202)
            try:
                if action == "lock":
                    result = self._with_app(
                        lambda app: app.director.lock_story_analysis(
                            episode_id))
                elif action == "save":
                    analysis = body.get("analysis")
                    if not isinstance(analysis, dict):
                        return self._error(400, "缺少 analysis 制作圣经")
                    result = self._with_app(
                        lambda app: app.director.save_story_analysis(
                            episode_id, analysis,
                            expected_version=body.get("expected_version"),
                            locked=bool(body.get("locked", False))))
                else:
                    return self._error(
                        400, "action 仅支持 save / lock / reanalyze")
            except AifosError as exc:
                return self._error(409, str(exc))
            return self._json(result)

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

        def _scene_expansion(self, query):
            """场景视角母版完成度:带 location 查单个,不带查全部。"""
            title = query.get("project", [""])[0].strip()
            if not title:
                return self._error(400, "缺少 project")
            location = query.get("location", [""])[0].strip()
            try:
                if location:
                    return self._json(self._with_app(
                        lambda app: app.director.scene_expansion_state(
                            title, location)))
                return self._json(self._with_app(
                    lambda app: app.director.scene_expansion_overview(title)))
            except Exception as exc:
                return self._error(400, str(exc))

        def _scene_expand(self):
            """生成 720° 全景母版 + 四向视角;已有的默认跳过不重复烧额度。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            title = (body.get("project") or "").strip()
            location = (body.get("location") or "").strip()
            if not title or not location:
                return self._error(400, "缺少 project/location")
            directions = body.get("directions")
            if directions is not None and not isinstance(directions, list):
                return self._error(400, "directions 必须是数组")
            try:
                result = self._with_app(
                    lambda app: app.director.expand_scene_views(
                        title, location,
                        regenerate=bool(body.get("regenerate")),
                        style=body.get("style"),
                        quality=(body.get("quality") or "high"),
                        include_panorama=body.get(
                            "include_panorama", True) is not False,
                        directions=directions))
            except Exception as exc:
                return self._error(400, str(exc))
            return self._json(result, status=201)

        def _project_create(self):
            """自定义作品名/资产库名:不必先跑一集就能建壳并往里存资产。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            title = (body.get("title") or "").strip()
            if not title:
                return self._error(400, "请填写作品名")
            if len(title) > 60:
                return self._error(400, "作品名不能超过 60 个字")

            def task(app):
                project, created = app.projects.get_or_create_project(
                    title, style=(body.get("style") or "").strip(),
                    aspect=(body.get("aspect") or "").strip())
                if created:
                    app.logger.info(
                        "web", f"已新建作品/资产库《{title}》(暂无剧集,"
                        "可直接在资产中心生产自建资产)")
                return {**dict(project), "created": created}

            try:
                return self._json(self._with_app(task), status=201)
            except Exception as exc:
                return self._error(400, str(exc))

        def _project_shell(self, query):
            """删除剧名前的体检:还剩几集、几张图,能不能删。"""
            title = query.get("project", [""])[0].strip()
            if not title:
                return self._error(400, "缺少 project")
            summary = self._with_app(
                lambda app: app.history.project_shell_summary(title))
            if summary is None:
                return self._error(404, f"作品不存在: {title}")
            return self._json(summary)

        def _project_delete(self):
            """删除只剩空壳的剧名;有剧集记录的一律拒绝,磁盘原图不动。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            title = (body.get("title") or "").strip()
            if not title:
                return self._error(400, "缺少 title")
            try:
                result = self._with_app(
                    lambda app: app.history.delete_project(
                        title, delete_assets=bool(body.get("delete_assets"))))
            except Exception as exc:
                return self._error(400, str(exc))
            return self._json(result)

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

        def _qc_override(self):
            """人工通过轻微问题图:{episode_id,item_ids,note,only_failed}."""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            item_ids = body.get("item_ids")
            if item_ids is not None and not isinstance(item_ids, list):
                return self._error(400, "item_ids 必须是数组")
            try:
                result = self._with_app(
                    lambda app: app.director.manual_qc_pass(
                        title, number, item_ids=item_ids,
                        only_failed=bool(body.get("only_failed")),
                        note=str(body.get("note") or "").strip()))
            except AifosError as exc:
                return self._error(400, str(exc))
            return self._json(result)

        def _qc_all(self):
            """批量质检:后台逐张核对(可暂停)。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            include_existing = bool(body.get("include_existing"))
            auto_repair = bool(body.get("auto_repair", True))
            parallel = bool(body.get("parallel"))
            categories = body.get("categories")
            if categories is not None and not isinstance(categories, list):
                return self._error(400, "categories 必须是数组")
            job_id = jobs.start_task(
                title, number,
                lambda app, run_id, report: app.director.qc_all(
                    title, number,
                    include_existing=include_existing,
                    auto_repair=auto_repair,
                    parallel=parallel,
                    progress=report,
                    categories=categories),
                action=("recheck_current_storyboard"
                        if include_existing else "qc_all"),
                tracked=True,
                request={
                    "include_existing": include_existing,
                    "auto_repair": auto_repair,
                    "parallel": parallel,
                    "categories": categories,
                })
            return self._json({"job_id": job_id}, status=202)

        def _image_acceleration_options(self, query):
            value = query.get("episode_id", [""])[0]
            try:
                episode_id = int(value)
            except (TypeError, ValueError):
                return self._error(400, "缺少有效 episode_id")
            found = self._episode_ref({"episode_id": episode_id})
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found

            def load(app):
                payload = app.director.image_acceleration_options(
                    title, number)
                for item in payload.get("items", []):
                    for ref in (item.get("references") or {}).get(
                            "items", []):
                        ref["url"] = _artifact_url(
                            app, ref.get("uri", ""))
                return payload

            return self._json(self._with_app(load))

        def _image_acceleration_body(self):
            body = self._read_body()
            if body is None:
                return None, None, (400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return None, None, (404, "剧集不存在")
            if not isinstance(body.get("item_ids"), list):
                return None, None, (400, "item_ids 必须是数组")
            return body, found, None

        def _image_acceleration_preflight(self):
            body, found, error = self._image_acceleration_body()
            if error is not None:
                return self._error(*error)
            title, number = found
            try:
                report = self._with_app(
                    lambda app: app.director.preflight_image_acceleration(
                        title, number, body.get("item_ids"),
                        str(body.get("provider") or ""),
                        str(body.get("model") or ""),
                        quality=body.get("quality") or "medium",
                        contract_tokens=body.get("contract_tokens") or {}))
            except AifosError as exc:
                return self._error(400, str(exc))
            return self._json(report)

        def _image_acceleration_queue(self):
            body, found, error = self._image_acceleration_body()
            if error is not None:
                return self._error(*error)
            title, number = found
            try:
                report = self._with_app(
                    lambda app: app.director.queue_image_acceleration(
                        title, number, body.get("item_ids"),
                        str(body.get("provider") or ""),
                        str(body.get("model") or ""),
                        quality=body.get("quality") or "medium",
                        fingerprint=str(body.get("fingerprint") or ""),
                        contract_tokens=body.get("contract_tokens") or {}))
            except AifosError as exc:
                return self._error(409, str(exc))
            running = jobs.running_for(title, number)
            if running:
                job_id = running[0]["id"]
                report["dispatch"] = "current_job"
            else:
                # 没有主任务时从断点恢复；真正的 API 调用仍由这一条原
                # Director 流水线完成，不另开抢跑的图片 worker。
                job_id = jobs.start(
                    title, number, review=False,
                    action="image_acceleration_resume")
                report["dispatch"] = "resumed_job"
            report["job_id"] = job_id
            return self._json(report, status=202)

        def _redo_items(self):
            """批量重画:{item_ids:[...]} 或 {only_failed:true}。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            if jobs.running_for(title, number):
                return self._error(
                    409, "本集正在生产，请先安全暂停后再批量优化修改")
            item_ids = body.get("item_ids") or []
            only_failed = bool(body.get("only_failed"))
            quality = body.get("quality")
            if quality is not None:
                try:
                    quality = normalize_quality(
                        quality, allow_auto=True, field="image_quality")
                except AifosError as exc:
                    return self._error(400, str(exc))
            override = bool(body.get("escalation_override"))
            job_id = jobs.start_task(
                title, number,
                lambda app, run_id, report: app.director.redo_items(
                    title, number, item_ids=item_ids,
                    only_failed=only_failed, quality_override=quality,
                    progress=report, escalation_override=override),
                action="redo_items", tracked=True,
                request={"item_ids": item_ids,
                         "only_failed": only_failed, "quality": quality,
                         "escalation_override": override})
            return self._json({"job_id": job_id}, status=202)

        def _redo_video(self):
            """人工修订后重生成单镜视频:{episode_id,shot_no,feedback}。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            feedback = (body.get("feedback") or "").strip()
            if not feedback:
                return self._error(400, "请填写视频质检问题与修改要求")
            try:
                shot_no = int(body.get("shot_no"))
            except (TypeError, ValueError):
                return self._error(400, "缺少有效 shot_no")
            title, number = found
            if jobs.running_for(title, number):
                return self._error(409, "本集正在生产，请先等待当前任务结束")
            job_id = jobs.start_task(
                title, number,
                lambda app, run_id: app.director.redo_video(
                    title, number, shot_no, feedback),
                action="redo_video", request={"shot_no": shot_no,
                                               "feedback": feedback},
                unique=True)
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
            reference_role,
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
                        note=(body.get("note") or "").strip(),
                        reference_role=(
                            body.get("reference_role") or "").strip()))
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

        def _history_delete(self):
            """删除历史对应的整集作品；关联图片由用户明确选择是否软删。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            has_run = body.get("run_id") is not None
            has_episode = body.get("episode_id") is not None
            if has_run == has_episode:
                return self._error(400, "请且仅请提供 run_id 或 episode_id")
            delete_assets = body.get("delete_assets") is True

            if has_run:
                try:
                    run_id = int(body["run_id"])
                except (TypeError, ValueError):
                    return self._error(400, "缺少合法 run_id")
                run = self._with_app(lambda app: app.history.get(run_id))
                if run is None:
                    return self._error(404, "历史记录不存在")
                title = run.get("current_project") or run["project_title"]
                number = run["episode_number"]

                def delete_target(app):
                    return app.history.delete_work(
                        run_id, delete_assets=delete_assets)
            else:
                try:
                    episode_id = int(body["episode_id"])
                except (TypeError, ValueError):
                    return self._error(400, "缺少合法 episode_id")

                def lookup(app):
                    episode = app.projects.get_episode(episode_id)
                    if episode is None:
                        return None
                    project = app.db.query_one(
                        "SELECT title FROM projects WHERE id=?",
                        (episode["project_id"],))
                    return project["title"], episode["number"]

                target = self._with_app(lookup)
                if target is None:
                    return self._error(404, "剧集不存在")
                title, number = target

                def delete_target(app):
                    return app.history.delete_episode_work(
                        episode_id, delete_assets=delete_assets)

            if jobs.running_for(title, number):
                return self._error(409, "本集仍在生成，请先安全停止后再删除")
            result = self._with_app(delete_target)
            if result is None:
                return self._error(404, "作品不存在或已删除")
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
                        body.get("asset_ids") or [],
                        reset=bool(body.get("reset"))))
            except Exception as exc:
                return self._error(400, str(exc))
            return self._json(result)

        def _lesson_approval(self):
            """人工审批一条质检观察:批准后才允许注入后续提示词。"""
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            if body.get("project_id") is None or not body.get("lesson_id"):
                return self._error(400, "缺少 project_id/lesson_id")
            try:
                result = self._with_app(
                    lambda app: set_lesson_approval(
                        app.assets, int(body["project_id"]),
                        str(body["lesson_id"]),
                        bool(body.get("approved", True))))
            except KeyError:
                return self._error(404, "经验库中没有这条记录")
            except Exception as exc:
                return self._error(400, str(exc))
            return self._json(result)

        def _upload(self):
            """人工修改素材上传:{episode_id, target, filename, data_base64}。
            target.kind: character_art / scene_art / shot / first_frame /
            last_frame / shot_video。"""
            import base64
            body = self._read_body()
            if body is None:
                return self._error(400, "请求体不是合法 JSON")
            target = body.get("target") or {}
            found = self._episode_ref(body)
            if found is None:
                return self._error(404, "剧集不存在")
            title, number = found
            if jobs.running_for(title, number):
                return self._error(
                    409, "本集正在生产，请先暂停，待状态稳定后再替换镜头")
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
