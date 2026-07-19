"""AIFOS 命令行入口。

最终目标(总体设计方案·八):
  python3 -m aifos produce "开始制作《万妖图录》第15集"
平台即自动完成剧本、分镜、图片、视频、配音、剪辑、质检和资产沉淀。
"""

import argparse
import json
import re
import sys

from . import __version__
from .app import App
from .errors import AifosError

PRODUCE_PATTERN = re.compile(r"《(?P<title>.+?)》\s*第\s*(?P<number>\d+)\s*集")


def parse_produce_sentence(text):
    """从一句话指令解析(作品名, 集数),如「开始制作《万妖图录》第15集」。"""
    match = PRODUCE_PATTERN.search(text or "")
    if not match:
        return None
    return match.group("title"), int(match.group("number"))


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="aifos",
        description="AIFOS V3.0 —— AI 精品漫剧工业化生产平台")
    parser.add_argument("--workspace", default="workspace",
                        help="工作区目录(默认 ./workspace)")
    parser.add_argument("--user", default="admin", help="操作用户")
    parser.add_argument("--version", action="version",
                        version=f"AIFOS {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="初始化工作区(数据库/配置/目录)")

    p_produce = sub.add_parser(
        "produce", help="制作一集:produce \"开始制作《万妖图录》第15集\"")
    p_produce.add_argument("sentence", nargs="?", default="",
                           help="一句话指令,含《作品名》第N集")
    p_produce.add_argument("--title", help="作品名(与 sentence 二选一)")
    p_produce.add_argument("--episode", type=int, help="集数")
    p_produce.add_argument("--premise", default="", help="本集前提/梗概")
    p_produce.add_argument("--style", default="", help="画风/风格")
    p_produce.add_argument("--verbose", action="store_true",
                           help="实时输出内部日志")

    sub.add_parser("status", help="制作状态看板")

    p_project = sub.add_parser("project", help="项目管理")
    p_project.add_argument("action", choices=["list", "create"])
    p_project.add_argument("--title", help="项目名")
    p_project.add_argument("--style", default="", help="画风")

    p_asset = sub.add_parser("asset", help="IP 资产中心")
    p_asset.add_argument("action", choices=["list", "stats"])
    p_asset.add_argument("--project", required=True, help="项目名")
    p_asset.add_argument("--kind", help="资产类型过滤")

    p_qc = sub.add_parser("qc", help="查看某集质检报告")
    p_qc.add_argument("--project", required=True)
    p_qc.add_argument("--episode", type=int, required=True)

    sub.add_parser("stats", help="成本统计与订阅额度")

    p_archive = sub.add_parser("archive", help="数据中心")
    p_archive.add_argument("action", choices=["stats", "export"])
    p_archive.add_argument("--out", default="archive_export.jsonl",
                           help="导出文件路径")

    p_logs = sub.add_parser("logs", help="查看最近日志")
    p_logs.add_argument("--limit", type=int, default=20)

    p_user = sub.add_parser("user", help="用户与权限")
    p_user.add_argument("action", choices=["list", "add"])
    p_user.add_argument("--name")
    p_user.add_argument("--role", default="operator",
                        choices=["admin", "operator", "viewer"])
    return parser


def _cmd_produce(app, args):
    title, number = args.title, args.episode
    if args.sentence:
        parsed = parse_produce_sentence(args.sentence)
        if parsed:
            title, number = parsed
    if not title or not number:
        print("无法识别制作目标。示例:produce \"开始制作《万妖图录》第15集\""
              " 或 produce --title 万妖图录 --episode 15", file=sys.stderr)
        return 2
    app.system.require(args.user, "produce")
    summary = app.director.produce(
        title, number, premise=args.premise, style=args.style)
    print(f"\n=== 《{title}》第{number}集 制作{_status_cn(summary['status'])} ===")
    print(f"质检得分: {summary['qc_score']}   "
          f"成本: {summary['cost']}/{summary['budget']}")
    print("阶段:")
    for stage in summary["stages"]:
        mark = "✓" if stage["status"] == "done" else "✗"
        providers = ",".join(stage.get("providers", [])) or "-"
        print(f"  {mark} {stage['name']:<10} cost={stage['cost']:<8} "
              f"provider={providers}")
        if stage.get("error"):
            print(f"      错误: {stage['error']}")
    outputs = summary["outputs"]
    if outputs["final"]:
        print(f"成片: {outputs['final']}")
    if outputs["cover"]:
        print(f"封面: {outputs['cover']}")
    for candidate in outputs["titles"]:
        print(f"候选标题: {candidate}")
    print(f"产物目录: {summary['artifacts_dir']}")
    return 0 if summary["status"] == "done" else 1


def _status_cn(status):
    return {"done": "完成", "failed": "失败", "qc_failed": "完成(质检未过)"}.get(
        status, status)


def _cmd_status(app):
    rows = app.projects.status_board()
    if not rows:
        print("暂无剧集。用 produce 开始制作。")
        return 0
    print(f"{'项目':<12}{'集':<5}{'状态':<12}{'质检':<8}{'成本':<8}")
    for row in rows:
        score = "-" if row["qc_score"] is None else f"{row['qc_score']:.0f}"
        print(f"{row['project']:<12}{row['number']:<5}{row['status']:<12}"
              f"{score:<8}{row['cost']:<8.2f}")
    return 0


def _cmd_project(app, args):
    if args.action == "create":
        if not args.title:
            print("--title 必填", file=sys.stderr)
            return 2
        app.system.require(args.user, "write")
        _, created = app.projects.get_or_create_project(
            args.title, style=args.style)
        print(("已创建" if created else "已存在") + f"项目《{args.title}》")
        return 0
    for row in app.projects.list_projects():
        print(f"[{row['id']}] {row['title']} 风格={row['style'] or '-'} "
              f"状态={row['status']}")
    return 0


def _cmd_asset(app, args):
    project = app.projects.get_project(args.project)
    if project is None:
        print(f"项目不存在: {args.project}", file=sys.stderr)
        return 2
    if args.action == "stats":
        for row in app.assets.stats(project["id"]):
            print(f"{row['kind']:<12} 数量={row['total']:<5} "
                  f"复用={row['reused'] or 0}")
        return 0
    for row in app.assets.list(project["id"], kind=args.kind):
        print(f"[{row['kind']}] {row['name']} v{row['version']} "
              f"复用{row['reuse_count']}次 {row['uri'] or ''}")
    return 0


def _cmd_qc(app, args):
    project = app.projects.get_project(args.project)
    if project is None:
        print(f"项目不存在: {args.project}", file=sys.stderr)
        return 2
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=?",
        (project["id"], args.episode))
    if episode is None:
        print(f"剧集不存在: 第{args.episode}集", file=sys.stderr)
        return 2
    report_path = (app.workspace.artifacts_dir / f"p{project['id']:03d}"
                   / f"e{episode['number']:03d}" / "qc_report.json")
    if not report_path.exists():
        print("尚无质检报告(未跑到质检阶段)")
        return 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    print(f"得分 {report['score']}(通过线 {report['pass_score']}),"
          f"{'通过' if report['passed'] else '未通过'}")
    for issue in report["issues"]:
        print(f"  [{issue['severity']}] {issue['check']}: {issue['message']}")
    return 0


def _cmd_stats(app):
    print("== 成本(按 Provider)==")
    for row in app.system.cost_by_provider():
        print(f"{row['provider']:<20} 调用{row['calls']:<6} "
              f"成本{row['total']:.2f}")
    print("== 成本(按阶段)==")
    for row in app.system.cost_by_stage():
        print(f"{row['stage']:<12} 任务{row['tasks']:<6} "
              f"成本{(row['total'] or 0):.2f}")
    print("== 订阅额度 ==")
    rows = app.system.quota_status()
    if not rows:
        print("(无额度限制的 Provider)")
    for row in rows:
        print(f"{row['provider']:<12} 已用 {row['used']}/{row['quota_limit']}")
    return 0


def _cmd_archive(app, args):
    if args.action == "export":
        count = app.data.export_jsonl(args.out)
        print(f"已导出 {count} 条沉淀数据到 {args.out}")
        return 0
    for row in app.data.stats():
        print(f"{row['kind']:<10}{row['label']:<10}{row['total']}")
    return 0


def _cmd_logs(app, args):
    for row in app.logger.tail(args.limit):
        print(f"[{row['level']}] {row['source']}: {row['message']}")
    return 0


def _cmd_user(app, args):
    if args.action == "add":
        if not args.name:
            print("--name 必填", file=sys.stderr)
            return 2
        app.system.require(args.user, "manage")
        app.system.ensure_user(args.name, args.role)
        print(f"已添加用户 {args.name}({args.role})")
        return 0
    for row in app.system.list_users():
        print(f"{row['name']:<16}{row['role']}")
    return 0


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    from .config import Config
    if args.command == "init":
        app = App(args.workspace)
        Config.write_default(app.workspace.config_path)
        print(f"工作区已初始化: {app.workspace.root}")
        print(f"  配置: {app.workspace.config_path}")
        print(f"  数据库: {app.workspace.db_path}")
        app.close()
        return 0
    echo = getattr(args, "verbose", False)
    app = App(args.workspace, echo_logs=echo)
    try:
        if args.command == "produce":
            return _cmd_produce(app, args)
        if args.command == "status":
            return _cmd_status(app)
        if args.command == "project":
            return _cmd_project(app, args)
        if args.command == "asset":
            return _cmd_asset(app, args)
        if args.command == "qc":
            return _cmd_qc(app, args)
        if args.command == "stats":
            return _cmd_stats(app)
        if args.command == "archive":
            return _cmd_archive(app, args)
        if args.command == "logs":
            return _cmd_logs(app, args)
        if args.command == "user":
            return _cmd_user(app, args)
        parser.print_help()
        return 0
    except AifosError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    finally:
        app.close()


if __name__ == "__main__":
    sys.exit(main())
