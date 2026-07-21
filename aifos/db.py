"""数据层:SQLite 统一存储全部结构化数据(项目/资产/任务/额度/日志/沉淀)。"""

import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  style TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  kind TEXT NOT NULL DEFAULT 'drama',     -- drama 漫剧 / idol AI虚拟偶像
  account TEXT NOT NULL DEFAULT '',       -- 绑定的抖音等平台账号
  aspect TEXT NOT NULL DEFAULT '',        -- 画幅:空=用全局默认(9:16)
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  number INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  premise TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'created',
  qc_score REAL,
  cost REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(project_id, number)
);

-- 剧本 / 分镜等结构化文档,按版本沉淀
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id INTEGER NOT NULL REFERENCES episodes(id),
  kind TEXT NOT NULL,
  version INTEGER NOT NULL,
  content TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(episode_id, kind, version)
);

-- IP 资产:角色/场景/动作/镜头/Prompt/首尾帧/图片/视频/配音/封面/成片等
CREATE TABLE IF NOT EXISTS assets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  uri TEXT NOT NULL DEFAULT '',
  meta TEXT NOT NULL DEFAULT '{}',
  reuse_count INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  UNIQUE(project_id, kind, name, version)
);

-- AI 导演中心的调度任务(每个流水线阶段一条)
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id INTEGER NOT NULL REFERENCES episodes(id),
  stage TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT '',
  cost REAL NOT NULL DEFAULT 0,
  retries INTEGER NOT NULL DEFAULT 0,
  payload TEXT NOT NULL DEFAULT '{}',
  result TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

-- 订阅额度(即梦 CLI 优先使用订阅额度,耗尽回退 API)
CREATE TABLE IF NOT EXISTS quota(
  provider TEXT PRIMARY KEY,
  used INTEGER NOT NULL DEFAULT 0,
  quota_limit INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  level TEXT NOT NULL,
  source TEXT NOT NULL,
  message TEXT NOT NULL
);

-- 数据中心:Prompt、图片、视频、成功案例、失败案例沉淀
CREATE TABLE IF NOT EXISTS archive(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id INTEGER,
  kind TEXT NOT NULL,
  label TEXT NOT NULL,
  prompt TEXT NOT NULL DEFAULT '',
  uri TEXT NOT NULL DEFAULT '',
  meta TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL DEFAULT 'operator'
);

-- 制作标准中心:版本记录只追加不修改，state 只保存各 profile 的激活指针。
CREATE TABLE IF NOT EXISTS production_standard_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_key TEXT NOT NULL,
  version INTEGER NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  change_note TEXT NOT NULL DEFAULT '',
  fingerprint TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(profile_key, version)
);

CREATE TABLE IF NOT EXISTS production_standard_state(
  profile_key TEXT PRIMARY KEY,
  active_version_id INTEGER NOT NULL
    REFERENCES production_standard_versions(id) ON DELETE RESTRICT,
  updated_at REAL NOT NULL
);

-- 数据库级保护，避免绕过 StandardCenter 意外篡改历史版本。
CREATE TRIGGER IF NOT EXISTS production_standard_versions_immutable_update
BEFORE UPDATE ON production_standard_versions
BEGIN
  SELECT RAISE(ABORT, 'production standard versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS production_standard_versions_immutable_delete
BEFORE DELETE ON production_standard_versions
BEGIN
  SELECT RAISE(ABORT, 'production standard versions are immutable');
END;
"""


def now():
    return time.time()


# 既有库的列级迁移:(表, 列, 声明)
MIGRATIONS = [
    ("projects", "kind", "TEXT NOT NULL DEFAULT 'drama'"),
    ("projects", "account", "TEXT NOT NULL DEFAULT ''"),
    ("projects", "aspect", "TEXT NOT NULL DEFAULT ''"),
]


class Database:
    def __init__(self, path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        # 标准保存会使用 BEGIN IMMEDIATE；给并发 UI/API 写入留出等待窗口，
        # 避免短暂锁竞争直接抛出 "database is locked"。
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        for table, column, decl in MIGRATIONS:
            cols = {row[1] for row in self.conn.execute(
                f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def execute(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def query(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def close(self):
        self.conn.close()
