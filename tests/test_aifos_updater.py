"""零操作自动更新:决策逻辑与 build 暴露。"""

from pathlib import Path

from aifos.updater import check_and_update, current_build, repo_root


class _R:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _runner(script):
    """script: {git子命令元组: _R};未列出的返回成功空输出。"""
    def run(*args):
        return script.get(args, _R())
    return run


def test_no_repo_skipped(tmp_path):
    assert check_and_update(None)[0] == "no_repo"
    assert current_build(tmp_path) == ""


def test_dirty_worktree_never_touched():
    status, detail = check_and_update(Path("/x"), runner=_runner({
        ("status", "--porcelain", "--untracked-files=no"): _R("M aifos/app.py\n")}))
    assert status == "dirty"
    assert "跳过" in detail


def test_up_to_date():
    status, _ = check_and_update(Path("/x"), runner=_runner({
        ("status", "--porcelain", "--untracked-files=no"): _R(""),
        ("rev-parse", "--abbrev-ref", "HEAD"): _R("main\n"),
        ("rev-parse", "HEAD"): _R("abc123\n"),
        ("rev-parse", "origin/main"): _R("abc123\n")}))
    assert status == "up_to_date"


def test_updates_when_behind():
    calls = []

    def run(*args):
        calls.append(args)
        table = {
            ("status", "--porcelain", "--untracked-files=no"): _R(""),
            ("rev-parse", "--abbrev-ref", "HEAD"): _R("main\n"),
            ("rev-parse", "HEAD"): _R("old\n"),
            ("rev-parse", "origin/main"): _R("new\n"),
            ("rev-parse", "--short", "HEAD"): _R("newshort\n"),
        }
        return table.get(args, _R())

    status, detail = check_and_update(Path("/x"), runner=run)
    assert status == "updated"
    assert detail == "newshort"
    assert ("pull", "--ff-only", "origin", "main") in calls


def test_fetch_failure_is_safe():
    status, detail = check_and_update(Path("/x"), runner=_runner({
        ("status", "--porcelain", "--untracked-files=no"): _R(""),
        ("rev-parse", "--abbrev-ref", "HEAD"): _R("main\n"),
        ("fetch", "origin", "main"): _R(returncode=1, stderr="offline")}))
    assert status == "error"
    assert "离线" in detail


def test_repo_root_detects_this_checkout():
    root = repo_root()
    assert root is not None and (root / ".git").exists()
