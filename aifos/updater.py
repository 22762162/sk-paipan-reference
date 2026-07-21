"""自动更新:服务空闲时自动拉取最新版并自愈重启,用户零操作。

- 只在安装目录是 git 仓库时生效(一键安装/启动脚本装出来的就是);
- 本地有改动 / 离线 / 拉取失败一律安全跳过,不影响正在运行的服务;
- 有生产任务在跑时绝不更新,只在空闲时进行;
- 更新成功后用 execv 原地重启同一条启动命令,几秒内自动恢复,
  浏览器端发现 build 变化会自动刷新页面。
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def repo_root():
    root = Path(__file__).resolve().parent.parent
    return root if (root / ".git").exists() else None


def _git_runner(root):
    def run(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=120)
    return run


def current_build(root=None):
    """当前代码版本(git 短哈希);非 git 安装返回空串。"""
    root = root or repo_root()
    if root is None:
        return ""
    try:
        proc = _git_runner(root)("rev-parse", "--short", "HEAD")
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def check_and_update(root, runner=None):
    """检查并更新代码。返回 (status, detail):
    no_repo / dirty / up_to_date / updated / error"""
    if root is None:
        return "no_repo", "非 git 安装,跳过自动更新"
    run = runner or _git_runner(root)
    try:
        # 只看已跟踪文件:workspace/日志等未跟踪目录不算"有改动"
        status = run("status", "--porcelain", "--untracked-files=no")
        if status.returncode != 0:
            return "error", status.stderr.strip()[:200]
        if status.stdout.strip():
            return "dirty", "本地有改动,跳过自动更新"
        branch = run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if not branch or branch == "HEAD":
            return "error", "无法识别当前分支"
        if run("fetch", "origin", branch).returncode != 0:
            return "error", "fetch 失败(可能离线),稍后再试"
        head = run("rev-parse", "HEAD").stdout.strip()
        remote = run("rev-parse", f"origin/{branch}").stdout.strip()
        if not remote:
            return "error", "远端分支不可读"
        if head == remote:
            return "up_to_date", head[:8]
        pull = run("pull", "--ff-only", "origin", branch)
        if pull.returncode != 0:
            return "error", pull.stderr.strip()[:200]
        new = run("rev-parse", "--short", "HEAD").stdout.strip()
        return "updated", new
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "error", str(exc)[:200]


def restart_process():
    """原地重启同一条启动命令(aifos serve …),监听端口自动重挂。"""
    os.execv(sys.executable, [sys.executable, *sys.argv])


def start_auto_updater(jobs_idle, on_log, interval=600, initial_delay=90):
    """后台自动更新线程:空闲时检查,更新成功即自愈重启。

    jobs_idle(): 当前没有生产任务在跑才返回 True;
    on_log(message): 更新动作写入平台日志,前端日志流可见。
    """
    root = repo_root()
    if root is None:
        return None

    def loop():
        time.sleep(initial_delay)
        while True:
            try:
                if jobs_idle():
                    status, detail = check_and_update(root)
                    if status == "updated":
                        on_log(f"发现新版本,已自动更新到 {detail};"
                               "正在重启服务,几秒后自动恢复,页面会自动刷新")
                        time.sleep(1)
                        restart_process()
            except Exception:
                pass    # 自动更新永不拖垮主服务
            time.sleep(interval)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread
