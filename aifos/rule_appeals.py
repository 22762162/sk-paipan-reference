"""规则上诉台账:死规则判败 → AI 仲裁复核 → 误杀放行的完整记录。

用户拍板的三级法院(2026-07-28):内置规则是死的,剧情是活的。
规则做初审(零成本、秒过,绝大多数直接放行进生产);判败的才上诉,
由 AI 仲裁带着裁决条款、剧本上下文和被判条款原文判断——到底是
真违规,还是规则字面化误杀 / 剧情本就需要这么写。

误杀不是终点而是数据:每次翻案写一行 JSONL,同一条规则被翻案
到一定次数,就说明规则本身该改(把今晚人肉"发现误杀→改规则"
的闭环变成系统自动积累)。

记录动作绝不阻断产线:任何 IO 异常都静默放弃。
"""

import json
import time
from pathlib import Path

from .channel_stats import stats_path_for as _channel_stats_path

FILENAME = "rule-appeals.jsonl"
MAX_SCAN_LINES = 20000

# 仲裁裁定
VERDICT_UPHELD = "upheld"        # 维持原判:确实违规,该修
VERDICT_OVERTURNED = "overturned"  # 撤销原判:规则误杀,放行
VERDICT_LABELS = {
    VERDICT_UPHELD: "维持原判(真违规)",
    VERDICT_OVERTURNED: "撤销原判(规则误杀)",
}

# 建议固化阈值:同一规则被撤销这么多次,就该改规则本身
FIX_RULE_THRESHOLD = 3


def record_appeal(out_dir, *, episode_id=None, item_id="", rule_id="",
                  capability="", verdict="", rule_reason="",
                  arbiter_reason="", evidence="", provider="", model="",
                  suggested_rule_fix=""):
    """追加一条规则上诉台账;失败静默,绝不影响产线。"""
    anchor = _channel_stats_path(out_dir)
    if anchor is None:
        return
    path = anchor.parent / FILENAME
    entry = {
        "ts": round(time.time(), 3),
        "episode_id": episode_id,
        "item_id": str(item_id or ""),
        "rule_id": str(rule_id or ""),
        "capability": str(capability or ""),
        "verdict": str(verdict or ""),
        "rule_reason": str(rule_reason or "")[:400],
        "arbiter_reason": str(arbiter_reason or "")[:400],
        "evidence": str(evidence or "")[:400],
        "provider": str(provider or ""),
        "model": str(model or ""),
        "suggested_rule_fix": str(suggested_rule_fix or "")[:300],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def summarize_appeals(logs_dir_or_file, hours=168.0):
    """聚合最近 hours 小时的上诉台账。

    返回 {"hours", "total", "overturned", "upheld",
          "by_rule": {rule_id: {"overturned", "upheld", "reasons": [...],
                                "fixes": [...], "needs_rule_fix": bool}}}
    """
    path = Path(logs_dir_or_file)
    if path.is_dir():
        path = path / FILENAME
    cutoff = time.time() - float(hours) * 3600
    total = overturned = upheld = 0
    by_rule = {}
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
        verdict = str(entry.get("verdict") or "")
        if verdict not in VERDICT_LABELS:
            continue
        total += 1
        rule_id = str(entry.get("rule_id") or "未知规则")
        stat = by_rule.setdefault(rule_id, {
            "overturned": 0, "upheld": 0, "reasons": [], "fixes": []})
        if verdict == VERDICT_OVERTURNED:
            overturned += 1
            stat["overturned"] += 1
            reason = str(entry.get("arbiter_reason") or "").strip()
            if reason and reason not in stat["reasons"]:
                stat["reasons"].append(reason[:160])
            fix = str(entry.get("suggested_rule_fix") or "").strip()
            if fix and fix not in stat["fixes"]:
                stat["fixes"].append(fix[:160])
        else:
            upheld += 1
            stat["upheld"] += 1
    for stat in by_rule.values():
        stat["needs_rule_fix"] = stat["overturned"] >= FIX_RULE_THRESHOLD
        stat["reasons"] = stat["reasons"][:3]
        stat["fixes"] = stat["fixes"][:3]
    return {
        "hours": float(hours),
        "total": total,
        "overturned": overturned,
        "upheld": upheld,
        "by_rule": by_rule,
    }


def rules_needing_fix(summary):
    """返回撤销次数达到阈值、建议直接改规则的条目(撤销数降序)。"""
    rows = [
        {"rule_id": rule_id, **stat}
        for rule_id, stat in (summary.get("by_rule") or {}).items()
        if stat.get("needs_rule_fix")
    ]
    rows.sort(key=lambda row: -row["overturned"])
    return rows


def format_appeal_table(summary):
    """summarize_appeals() 结果 → 等宽文本表(CLI / 日志用)。"""
    total = summary.get("total") or 0
    hours = summary.get("hours", 168)
    if not total:
        return "最近 %.0f 小时没有规则上诉记录(死规则初审全过)" % hours
    overturned = summary.get("overturned", 0)
    lines = ["规则上诉 %d 次:撤销原判(规则误杀)%d,维持原判(真违规)%d"
             "(近 %.0f 小时)" % (
                 total, overturned, summary.get("upheld", 0), hours)]
    rows = [("规则", "误杀", "真违规", "建议")]
    ordered = sorted((summary.get("by_rule") or {}).items(),
                     key=lambda kv: -kv[1].get("overturned", 0))
    for rule_id, stat in ordered:
        advice = ""
        if stat.get("needs_rule_fix"):
            advice = "⚠ 该改规则:" + (
                (stat.get("fixes") or stat.get("reasons") or [""])[0][:40])
        rows.append((rule_id, str(stat.get("overturned", 0)),
                     str(stat.get("upheld", 0)), advice))
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    lines.extend("  ".join(cell.ljust(widths[i])
                           for i, cell in enumerate(row)) for row in rows)
    return "\n".join(lines)
