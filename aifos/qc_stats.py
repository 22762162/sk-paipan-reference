"""图片/视频质检台账:结构化沉淀每次质检结果与失败原因分类。

散在日志文本里的质检结论(身份漂移、人数不一致、视角不符…)无法
按产线/镜头类型/失败类型统计,精准度迭代就只能凭感觉。本模块把
每次质检写成一行 JSONL(workspace/logs/qc-stats.jsonl),并提供
按失败类型 × 产线聚合的汇总——"总结每次失败原因"的数据底座。

记录动作绝不阻断产线:任何 IO 异常都静默放弃。
"""

import json
import re
import time
from pathlib import Path

from .channel_stats import stats_path_for as _channel_stats_path

FILENAME = "qc-stats.jsonl"
MAX_SCAN_LINES = 20000

# 失败原因分类法:按优先级首个命中归类(与历史日志高频表述对齐)。
# 顺序重要:"性别…与立绘不一致"应归 gender 而不是 identity_drift。
ISSUE_TAXONOMY = (
    ("figure_count", re.compile(
        r"人数|多余人物|新增人物|复制人物|检测到\S{0,4}人")),
    ("gender", re.compile(r"性别")),
    ("identity_drift", re.compile(
        r"身份|立绘不一致|骨相|五官|脸型|年龄感|漂移|面部\S{0,6}不一致")),
    ("camera", re.compile(
        r"视角|构图|俯拍|仰拍|平拍|过肩|镜位|机位|back_|profile_")),
    ("onscreen_text", re.compile(r"文字|字幕|花字|水印|乱码|泄漏")),
    ("wardrobe", re.compile(r"服装|妆发|妆容|发型|头饰|饰品|穿戴")),
    ("props", re.compile(r"道具")),
    ("continuity", re.compile(r"空间|站位|连续性|继承|首帧|尾帧")),
    ("contract", re.compile(r"合同|提示词|参考图|绑定")),
)

# 分类的中文名(CLI 表格用)
TYPE_LABELS = {
    "figure_count": "人数/多余人物",
    "gender": "性别表达",
    "identity_drift": "身份漂移",
    "camera": "视角/构图",
    "onscreen_text": "画面文字",
    "wardrobe": "服装/妆发",
    "props": "道具",
    "continuity": "空间/连续性",
    "contract": "提示词/合同",
    "other": "其他",
}


def classify_issue(issue):
    """单条质检问题 → 失败类型(首个命中,无命中归 other)。"""
    text = str(issue or "")
    for name, pattern in ISSUE_TAXONOMY:
        if pattern.search(text):
            return name
    return "other"


def classify_issues(issues):
    """问题列表 → 去重后的失败类型列表(保持出现顺序)。"""
    seen, types = set(), []
    for issue in issues or []:
        name = classify_issue(issue)
        if name not in seen:
            seen.add(name)
            types.append(name)
    return types


def record_qc(out_dir, *, episode_id=None, item_id="", category="",
              capability="", provider="", model="", task_class="",
              passed=None, issues=None, attempts=None):
    """追加一条质检台账;失败静默,绝不影响产线。"""
    anchor = _channel_stats_path(out_dir)
    if anchor is None:
        return
    path = anchor.parent / FILENAME
    issues = [str(item)[:200] for item in (issues or []) if item][:10]
    entry = {
        "ts": round(time.time(), 3),
        "episode_id": episode_id,
        "item_id": str(item_id or ""),
        "category": str(category or ""),
        "capability": str(capability or ""),
        "provider": str(provider or ""),
        "model": str(model or ""),
        "task_class": str(task_class or ""),
        "passed": bool(passed) if passed is not None else None,
        "issue_types": classify_issues(issues),
        "issues": issues,
    }
    if attempts is not None:
        entry["attempts"] = attempts
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def summarize_qc(logs_dir_or_file, hours=168.0):
    """聚合最近 hours 小时的质检台账。

    返回 {"hours", "total", "passed", "failed",
          "by_issue_type": {type: {"count", "providers": {name: n}}},
          "by_provider": {name: {"passed", "failed"}}}
    """
    path = Path(logs_dir_or_file)
    if path.is_dir():
        path = path / FILENAME
    cutoff = time.time() - float(hours) * 3600
    total = passed = failed = 0
    by_type, by_provider = {}, {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines[-MAX_SCAN_LINES:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if float(entry.get("ts") or 0) < cutoff:
            continue
        if entry.get("passed") is None:
            continue
        total += 1
        provider = str(entry.get("provider") or "未知")
        stat = by_provider.setdefault(provider, {"passed": 0, "failed": 0})
        if entry["passed"]:
            passed += 1
            stat["passed"] += 1
            continue
        failed += 1
        stat["failed"] += 1
        for name in entry.get("issue_types") or ["other"]:
            bucket = by_type.setdefault(
                name, {"count": 0, "providers": {}})
            bucket["count"] += 1
            bucket["providers"][provider] = (
                bucket["providers"].get(provider, 0) + 1)
    ordered = dict(sorted(
        by_type.items(), key=lambda kv: -kv[1]["count"]))
    return {"hours": float(hours), "total": total,
            "passed": passed, "failed": failed,
            "by_issue_type": ordered, "by_provider": by_provider}


def format_qc_table(summary):
    """summarize_qc() 结果 → 等宽文本表(CLI / 日志用)。"""
    total = summary.get("total") or 0
    if not total:
        return "最近 %.0f 小时没有质检台账记录" % summary.get("hours", 168)
    lines = ["质检 %d 次:通过 %d,未通过 %d(近 %.0f 小时)" % (
        total, summary.get("passed", 0), summary.get("failed", 0),
        summary.get("hours", 168))]
    rows = [("失败类型", "次数", "主要产线")]
    for name, item in (summary.get("by_issue_type") or {}).items():
        providers = sorted(item.get("providers", {}).items(),
                           key=lambda kv: -kv[1])
        top = "、".join(f"{prov}×{count}" for prov, count in providers[:3])
        rows.append((TYPE_LABELS.get(name, name), str(item["count"]), top))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    lines.extend("  ".join(cell.ljust(widths[i])
                           for i, cell in enumerate(row)) for row in rows)
    return "\n".join(lines)
