"""Codex 通道速度统计:按登录态通道记录每次真实调用的墙钟耗时。

数据以 JSON Lines 追加写入 workspace/logs/channel-stats.jsonl,无需
数据库迁移;读取端只扫尾部有限行数做聚合。记录动作绝不阻断产线:
任何 IO 异常都静默放弃。

每行结构:
  {"ts": 1785097000.1, "provider": "codex", "profile": "codex_b",
   "capability": "image", "seconds": 187.4, "ok": true}
失败条目附带 "error" 摘要(截断),超时也计入失败——平均速度只统计
成功条目,失败/超时单独计数,避免把 1200s 超时摊进"平均出图速度"。
"""

import json
import time
from pathlib import Path

FILENAME = "channel-stats.jsonl"
# 聚合时最多回看的行数;按每张图一行,足够覆盖数天生产
MAX_SCAN_LINES = 20000
IMAGE_CAPABILITIES = ("image", "frames", "cover")


def stats_path_for(out_dir):
    """从产物目录推导 workspace/logs 下的统计文件路径。

    out_dir 形如 <workspace>/artifacts/pXXX/eYYY/...;找不到 artifacts
    锚点(非常规布局)返回 None,调用方应放弃记录。
    """
    try:
        path = Path(out_dir).resolve()
    except OSError:
        return None
    for parent in (path, *path.parents):
        if parent.name == "artifacts":
            return parent.parent / "logs" / FILENAME
    return None


def record(out_dir, provider, profile, capability, seconds, ok, error=""):
    """追加一条通道耗时记录;失败静默,绝不影响产线。"""
    path = stats_path_for(out_dir)
    if path is None:
        return
    entry = {
        "ts": round(time.time(), 3),
        "provider": str(provider or ""),
        "profile": str(profile or "") or "default",
        "capability": str(capability or ""),
        "seconds": round(float(seconds), 3),
        "ok": bool(ok),
    }
    if error:
        entry["error"] = str(error)[:200]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def summarize(logs_dir_or_file, hours=24.0,
              capabilities=IMAGE_CAPABILITIES, provider="codex"):
    """按通道聚合最近 hours 小时的记录。

    返回 {"hours": ..., "provider": ..., "capabilities": [...],
          "channels": {profile: {"completed", "failed", "avg_seconds",
                                 "per_minute", "last_ts"}}}
    avg_seconds/per_minute 只统计成功条目;capabilities 传 None 表示
    不过滤能力(例如也想看 image_qc/prompt_review 的通道占用)。
    """
    path = Path(logs_dir_or_file)
    if path.is_dir():
        path = path / FILENAME
    cutoff = time.time() - float(hours) * 3600
    wanted = tuple(capabilities) if capabilities else None
    buckets = {}
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
        if provider and entry.get("provider") != provider:
            continue
        if float(entry.get("ts") or 0) < cutoff:
            continue
        if wanted is not None and entry.get("capability") not in wanted:
            continue
        profile = str(entry.get("profile") or "default")
        bucket = buckets.setdefault(profile, {
            "completed": 0, "failed": 0,
            "seconds_total": 0.0, "last_ts": 0.0})
        bucket["last_ts"] = max(bucket["last_ts"],
                                float(entry.get("ts") or 0))
        if entry.get("ok"):
            bucket["completed"] += 1
            bucket["seconds_total"] += float(entry.get("seconds") or 0)
        else:
            bucket["failed"] += 1
    channels = {}
    for profile in sorted(buckets):
        bucket = buckets[profile]
        avg = (bucket["seconds_total"] / bucket["completed"]
               if bucket["completed"] else None)
        channels[profile] = {
            "completed": bucket["completed"],
            "failed": bucket["failed"],
            "avg_seconds": round(avg, 1) if avg else None,
            "per_minute": round(60.0 / avg, 2) if avg else None,
            "last_ts": bucket["last_ts"] or None,
        }
    return {"hours": float(hours), "provider": provider,
            "capabilities": list(wanted) if wanted else None,
            "channels": channels}


def format_table(summary):
    """把 summarize() 结果排成等宽文本表(CLI / 日志用)。"""
    channels = summary.get("channels") or {}
    if not channels:
        return "最近 %.0f 小时没有通道记录" % summary.get("hours", 24)
    rows = [("通道", "完成", "失败", "平均耗时", "张/分钟")]
    for profile, item in channels.items():
        avg = item.get("avg_seconds")
        rate = item.get("per_minute")
        rows.append((
            profile,
            str(item.get("completed", 0)),
            str(item.get("failed", 0)),
            f"{avg:.1f}s" if avg else "-",
            f"{rate:.2f}" if rate else "-",
        ))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(cell.ljust(widths[i])
                       for i, cell in enumerate(row)) for row in rows]
    return "\n".join(lines)
