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
        # 只有远端领先(本地是 origin/branch 的祖先)才更新。本地有未推送
        # 提交或分叉时,head != remote 但本地并不落后——绝不能 pull/重启,
        # 否则每一轮自动更新都会把“本地领先”误判为“有新版本”而反复 os.execv
        # 重启服务(在测试进程里则表现为无限重跑)。
        ancestor = run("merge-base", "--is-ancestor", "HEAD",
                       f"origin/{branch}")
        if ancestor.returncode != 0:
            return "up_to_date", head[:8]
        pull = run("pull", "--ff-only", "origin", branch)
        if pull.returncode != 0:
            return "error", pull.stderr.strip()[:200]
        new = run("rev-parse", "--short", "HEAD").stdout.strip()
        return "updated", new
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "error", str(exc)[:200]


def restart_process():
    """原地重启同一条启动命令(aifos serve …),监听端口自动重挂。

    `python3 -m aifos` 启动时 sys.argv[0] 是包内 __main__.py 的路径,
    直接 exec 会破坏相对导入;识别后重组回 -m 形式。"""
    argv = list(sys.argv)
    if argv and argv[0].endswith("__main__.py"):
        package = Path(argv[0]).resolve().parent.name
        args = [sys.executable, "-m", package, *argv[1:]]
    else:
        args = [sys.executable, *argv]
    os.execv(sys.executable, args)


def start_auto_updater(jobs_idle, on_log, interval=600, initial_delay=90):
    """后台自动更新线程:空闲时检查,更新成功即自愈重启。

    jobs_idle(): 当前没有生产任务在跑才返回 True;
    on_log(message): 更新动作写入平台日志,前端日志流可见。
    """
    # 测试进程内绝不启动自愈重启:否则 os.execv 会把 pytest 进程原地替换,
    # 导致整套测试被无限重跑(serve() 在 Web 测试里会拉起本线程)。
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
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
