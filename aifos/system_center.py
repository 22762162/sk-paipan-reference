"""系统中心:权限、日志、成本统计、CLI/API(Provider)管理入口。"""

from .db import now
from .errors import PermissionDenied

# 角色 → 允许的动作
ROLES = {
    "admin": {"produce", "write", "read", "manage"},
    "operator": {"produce", "write", "read"},
    "viewer": {"read"},
}


class Logger:
    """结构化日志:同时写入数据库与 workspace/logs/aifos.log。"""

    def __init__(self, db, log_dir, echo=False):
        self.db = db
        self.log_path = log_dir / "aifos.log"
        self.echo = echo

    def log(self, level, source, message):
        ts = now()
        self.db.execute(
            "INSERT INTO logs(ts, level, source, message) VALUES(?,?,?,?)",
            (ts, level, source, message),
        )
        line = f"{ts:.3f} [{level}] {source}: {message}\n"
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line)
        if self.echo:
            print(line, end="")

    def info(self, source, message):
        self.log("INFO", source, message)

    def warn(self, source, message):
        self.log("WARN", source, message)

    def error(self, source, message):
        self.log("ERROR", source, message)

    def tail(self, limit=20):
        rows = self.db.query(
            "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))
        return list(reversed(rows))


class SystemCenter:
    def __init__(self, db, logger):
        self.db = db
        self.log = logger

    # ---- 权限 ----
    def ensure_user(self, name, role="operator"):
        row = self.db.query_one("SELECT * FROM users WHERE name=?", (name,))
        if row is None:
            self.db.execute(
                "INSERT INTO users(name, role) VALUES(?,?)", (name, role))
            row = self.db.query_one("SELECT * FROM users WHERE name=?", (name,))
        return row

    def require(self, user_name, action):
        row = self.db.query_one(
            "SELECT * FROM users WHERE name=?", (user_name,))
        if row is None:
            raise PermissionDenied(f"用户不存在: {user_name}")
        allowed = ROLES.get(row["role"], set())
        if action not in allowed:
            raise PermissionDenied(
                f"用户 {user_name}(角色 {row['role']})无权执行 {action}")
        return row

    def list_users(self):
        return self.db.query("SELECT * FROM users ORDER BY id")

    # ---- 成本统计 ----
    def _unassigned_cost(self, *, provider_only=False):
        assigned_where = " WHERE provider != ''" if provider_only else ""
        row = self.db.query_one(
            "SELECT "
            "COALESCE((SELECT SUM(cost) FROM episodes), 0) AS charged, "
            "COALESCE((SELECT SUM(cost) FROM tasks"
            + assigned_where + "), 0) AS assigned")
        return round(max(
            0.0, float(row["charged"] or 0) - float(row["assigned"] or 0)), 4)

    def cost_by_provider(self):
        rows = [dict(row) for row in self.db.query(
            "SELECT provider, COUNT(*) AS calls, SUM(cost) AS total "
            "FROM tasks WHERE provider != '' GROUP BY provider "
            "ORDER BY total DESC")]
        unassigned = self._unassigned_cost(provider_only=True)
        if unassigned:
            rows.append({
                "provider": "历史调整调用（待归档）",
                "calls": 0,
                "total": unassigned,
            })
        return sorted(
            rows, key=lambda row: float(row["total"] or 0), reverse=True)

    def cost_by_stage(self):
        rows = [dict(row) for row in self.db.query(
            "SELECT stage, COUNT(*) AS tasks, SUM(cost) AS total "
            "FROM tasks GROUP BY stage ORDER BY total DESC")]
        unassigned = self._unassigned_cost()
        if unassigned:
            rows.append({
                "stage": "historical_adjustment",
                "tasks": 0,
                "total": unassigned,
            })
        return sorted(
            rows, key=lambda row: float(row["total"] or 0), reverse=True)

    def cost_by_episode(self):
        return self.db.query(
            "SELECT e.id, p.title, e.number, e.cost, e.status, e.qc_score "
            "FROM episodes e JOIN projects p ON p.id = e.project_id "
            "ORDER BY e.id")

    # ---- 额度 ----
    def quota_status(self):
        return self.db.query("SELECT * FROM quota ORDER BY provider")
