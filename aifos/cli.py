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
    p_produce.add_argument("--kind", choices=["drama", "idol"],
                           help="内容类型:drama 剧情短剧 / idol 虚拟偶像;"
                                "不填按标题/前提关键词自动识别")
    p_produce.add_argument("--script-file",
                           help="自带剧本文件(纯文本或 JSON);提供时跳过 AI "
                                "编剧,人物/分镜自动从剧本推导")
    p_produce.add_argument("--review", action="store_true",
                           help="预生产(剧本/人物/场景/分镜)完成后暂停,"
                                "确认后用 confirm 命令继续视频生产")
    p_produce.add_argument("--force", action="store_true",
                           help="强制全部重新生成(默认增量:已有产物直接复用)")
    p_produce.add_argument("--verbose", action="store_true",
                           help="实时输出内部日志")

    p_batch = sub.add_parser(
        "batch", help="批量制作:batch 万妖图录 --from 1 --to 5")
    p_batch.add_argument("title", help="作品名")
    p_batch.add_argument("--from", dest="start", type=int, required=True)
    p_batch.add_argument("--to", dest="end", type=int, required=True)
    p_batch.add_argument("--style", default="")
    p_batch.add_argument("--force", action="store_true")

    sub.add_parser("status", help="制作状态看板")

    p_serve = sub.add_parser(
        "serve", help="启动 Web 控制台(仪表盘 + 分镜画布)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8619)

    p_project = sub.add_parser("project", help="项目管理(账号矩阵)")
    p_project.add_argument("action", choices=["list", "create", "set"])
    p_project.add_argument("--title", help="项目名(偶像项目=偶像人设名)")
    p_project.add_argument("--style", default=None, help="画风/人设风格")
    p_project.add_argument("--kind", default=None,
                           choices=["drama", "idol"],
                           help="内容类型:drama 漫剧 / idol AI虚拟偶像")
    p_project.add_argument("--account", default=None,
                           help="绑定的抖音等平台账号名")
    p_project.add_argument("--aspect", default=None,
                           choices=["9:16", "16:9"],
                           help="画幅(默认跟随全局 9:16)")

    p_publish = sub.add_parser("publish", help="查看某集发布包(成片/封面/标题/话题)")
    p_publish.add_argument("--project", required=True)
    p_publish.add_argument("--episode", type=int, required=True)

    p_confirm = sub.add_parser(
        "confirm", help="确认预生产,继续自动完成视频/配音/剪辑/质检")
    p_confirm.add_argument("--project", required=True)
    p_confirm.add_argument("--episode", type=int, required=True)

    p_revise = sub.add_parser(
        "revise", help="按修改意见重写剧本并重出人物/分镜(回到待确认)")
    p_revise.add_argument("--project", required=True)
    p_revise.add_argument("--episode", type=int, required=True)
    p_revise.add_argument("--feedback", required=True, help="修改意见")

    p_regen = sub.add_parser(
        "regen", help="附意见重画单张图(人物立绘/场景图/镜头画面)")
    p_regen.add_argument("--project", required=True)
    p_regen.add_argument("--episode", type=int, required=True)
    group = p_regen.add_mutually_exclusive_group(required=True)
    group.add_argument("--shot", type=int, help="镜头号")
    group.add_argument("--character", help="人物名")
    group.add_argument("--scene", help="场景名")
    p_regen.add_argument("--feedback", default="", help="修改意见(可选)")

    p_import = sub.add_parser(
        "import", help="上传人工修改后的素材(图片/mp4,按扩展名识别)")
    p_import.add_argument("--project", required=True)
    p_import.add_argument("--episode", type=int, required=True)
    p_import.add_argument("--file", required=True, help="素材文件路径")
    igroup = p_import.add_mutually_exclusive_group(required=True)
    igroup.add_argument("--shot", type=int, help="镜头号")
    igroup.add_argument("--character", help="人物名")
    igroup.add_argument("--scene", help="场景名")

    p_export = sub.add_parser(
        "export", help="导出成品包 zip(成片/封面/文案/配音/草稿/剧本)")
    p_export.add_argument("--project", required=True)
    p_export.add_argument("--episode", type=int, required=True)
    p_export.add_argument("--out", help="输出路径(默认当前目录按集命名)")

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
    script = None
    if getattr(args, "script_file", None):
        from .script_import import ScriptImportError, load_script_file
        try:
            script = load_script_file(args.script_file, title, number)
        except (OSError, ScriptImportError) as exc:
            print(f"剧本导入失败: {exc}", file=sys.stderr)
            return 2
        print(f"已导入剧本:{len(script['scenes'])} 场,"
              f"{len(script['characters'])} 个角色")
    summary = app.director.produce(
        title, number, premise=args.premise, style=args.style,
        force=args.force, script=script,
        pause_for_confirm=args.review, kind=args.kind)
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
    if summary["status"] == "awaiting_confirm":
        print("\n预生产完成(剧本/人物图/场景图/分镜/首尾帧)。检查满意后运行:")
        print(f"  python3 -m aifos confirm --project {title} --episode {number}")
        print("即可自动完成 视频 → 配音 → 剪辑 → 质检 → 发布包。")
        return 0
    return 0 if summary["status"] == "done" else 1


def _status_cn(status):
    return {"done": "完成", "failed": "失败", "qc_failed": "完成(质检未过)",
            "awaiting_confirm": "待确认"}.get(status, status)


def _cmd_confirm(app, args):
    project = app.projects.get_project(args.project)
    if project is None:
        print(f"项目不存在: {args.project}", file=sys.stderr)
        return 2
    app.system.require(args.user, "produce")
    print(f"已确认,继续生产《{args.project}》第{args.episode}集 …")
    summary = app.director.produce(args.project, args.episode)
    print(f"制作{_status_cn(summary['status'])},"
          f"质检 {summary['qc_score']},成本 {summary['cost']}")
    return 0 if summary["status"] == "done" else 1


def _cmd_batch(app, args):
    """批量制作:逐集执行,单集失败不中断整批。"""
    if args.end < args.start:
        print("--to 不能小于 --from", file=sys.stderr)
        return 2
    app.system.require(args.user, "produce")
    rows = []
    for number in range(args.start, args.end + 1):
        print(f"—— 制作《{args.title}》第{number}集 ——")
        try:
            summary = app.director.produce(
                args.title, number, style=args.style, force=args.force)
            rows.append((number, summary["status"], summary["qc_score"],
                         summary["cost"]))
        except Exception as exc:
            rows.append((number, "failed", None, None))
            print(f"  第{number}集异常: {exc}", file=sys.stderr)
    print(f"\n{'集':<6}{'状态':<12}{'质检':<8}{'成本':<8}")
    for number, status, score, cost in rows:
        score_s = "-" if score is None else f"{score:.0f}"
        cost_s = "-" if cost is None else f"{cost:.2f}"
        print(f"{number:<6}{_status_cn(status):<12}{score_s:<8}{cost_s:<8}")
    return 0 if all(r[1] == "done" for r in rows) else 1


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
    if args.action in ("create", "set"):
        if not args.title:
            print("--title 必填", file=sys.stderr)
            return 2
        app.system.require(args.user, "write")
        _, created = app.projects.get_or_create_project(
            args.title, style=args.style or "",
            kind=args.kind or "drama",
            account=args.account or "", aspect=args.aspect or "")
        if not created:
            app.projects.update_project(
                args.title, style=args.style, kind=args.kind,
                account=args.account, aspect=args.aspect)
        row = app.projects.get_project(args.title)
        kind_cn = {"drama": "漫剧", "idol": "AI虚拟偶像"}.get(
            row["kind"], row["kind"])
        print(("已创建" if created else "已更新") + f"项目《{args.title}》 "
              f"类型={kind_cn} 账号={row['account'] or '-'} "
              f"画幅={row['aspect'] or '默认(9:16)'}")
        return 0
    for row in app.projects.list_projects():
        kind_cn = {"drama": "漫剧", "idol": "偶像"}.get(
            row["kind"], row["kind"])
        print(f"[{row['id']}] {row['title']} 类型={kind_cn} "
              f"账号={row['account'] or '-'} "
              f"画幅={row['aspect'] or '9:16'} 状态={row['status']}")
    return 0


def _cmd_publish(app, args):
    project = app.projects.get_project(args.project)
    if project is None:
        print(f"项目不存在: {args.project}", file=sys.stderr)
        return 2
    kit_path = (app.workspace.artifacts_dir / f"p{project['id']:03d}"
                / f"e{args.episode:03d}" / "publish" / "publish.json")
    if not kit_path.exists():
        print("发布包不存在(该集尚未制作完成或质检未过)", file=sys.stderr)
        return 1
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    print(f"=== 发布包 · {kit['account']} · 第{kit['episode']}集 "
          f"({kit['aspect']}) ===")
    print(f"成片: {kit['video']}")
    print(f"封面: {kit['cover']}")
    for title in kit["titles"]:
        print(f"候选标题: {title}")
    print(f"话题: {' '.join(kit['hashtags'])}")
    for clip in kit["clips"]:
        print(f"拆条: 场{clip['scene_no']}({clip['duration']}s) {clip['uri']}")
    print(f"建议: {kit['publish_hint']}")
    print(f"包文件: {kit_path}")
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
    for name, provider in app.router.providers.items():
        if provider.enabled and hasattr(provider, "credit"):
            try:
                print(f"{name} 实时余额: {provider.credit()}")
            except Exception as exc:
                print(f"{name} 余额查询失败: {exc}")
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
    if args.command == "serve":
        from .web.server import serve
        App(args.workspace).close()  # 确保工作区就绪
        httpd = serve(args.workspace, host=args.host, port=args.port)
        print(f"AIFOS 控制台: http://{args.host}:{httpd.server_address[1]}/")
        print("Ctrl+C 停止")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")
        finally:
            httpd.server_close()
        return 0
    echo = getattr(args, "verbose", False)
    app = App(args.workspace, echo_logs=echo)
    try:
        if args.command == "produce":
            return _cmd_produce(app, args)
        if args.command == "batch":
            return _cmd_batch(app, args)
        if args.command == "status":
            return _cmd_status(app)
        if args.command == "project":
            return _cmd_project(app, args)
        if args.command == "publish":
            return _cmd_publish(app, args)
        if args.command == "confirm":
            return _cmd_confirm(app, args)
        if args.command == "revise":
            app.system.require(args.user, "produce")
            summary = app.director.revise_script(
                args.project, args.episode, args.feedback)
            print(f"剧本已按意见重写,{_status_cn(summary['status'])};"
                  f"检查后用 confirm 继续")
            return 0
        if args.command == "regen":
            app.system.require(args.user, "produce")
            if args.shot is not None:
                target = {"kind": "shot", "shot_no": args.shot}
            elif args.character:
                target = {"kind": "character_art", "name": args.character}
            else:
                target = {"kind": "scene_art", "name": args.scene}
            result = app.director.regen_image(
                args.project, args.episode, target, feedback=args.feedback)
            print(f"已重画: {result['uri']}(成本 {result['cost']})")
            if target["kind"] == "shot":
                print("该镜头旧视频已作废;运行 produce(增量)重拍并重剪")
            return 0
        if args.command == "import":
            app.system.require(args.user, "produce")
            from pathlib import Path as _P
            data = _P(args.file).read_bytes()
            ext = _P(args.file).suffix.lower()
            if ext == ".mp4":
                if args.shot is None:
                    print("mp4 需配合 --shot", file=sys.stderr)
                    return 2
                result = app.director.import_video(
                    args.project, args.episode, args.shot, data, ext)
            else:
                if args.shot is not None:
                    target = {"kind": "shot", "shot_no": args.shot}
                elif args.character:
                    target = {"kind": "character_art",
                              "name": args.character}
                else:
                    target = {"kind": "scene_art", "name": args.scene}
                result = app.director.import_image(
                    args.project, args.episode, target, data, ext)
            print(f"已导入: {result['uri']}")
            return 0
        if args.command == "export":
            from .export_kit import build_export_zip
            data, filename = build_export_zip(
                app, args.project, args.episode)
            out = args.out or filename
            with open(out, "wb") as f:
                f.write(data)
            print(f"成品包已导出: {out}({len(data) // 1024} KB)")
            return 0
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
