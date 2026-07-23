"""应用装配:workspace + 八大业务中心 + 制作标准中心统一初始化。"""

from pathlib import Path

from .asset_center import AssetCenter
from .config import Config
from .data_center import DataCenter
from .db import Database
from .director import Director
from .firefire_center import FireFireCenter
from .history_center import HistoryCenter
from .icloud_sync import ICloudImageSync
from .ops_center import OpsCenter
from .production.router import ProviderRouter
from .project_center import ProjectCenter
from .qc_center import QcCenter
from .series_center import SeriesCenter
from .standard_center import StandardCenter
from .system_center import Logger, SystemCenter


class Workspace:
    def __init__(self, root):
        # 绝对路径:产物 uri 会交给外部 Provider 子进程(codex/dreamina),
        # 相对路径会随子进程 cwd 漂移
        self.root = Path(root).resolve()
        self.config_path = self.root / "config.json"
        self.db_path = self.root / "aifos.db"
        self.logs_dir = self.root / "logs"
        self.artifacts_dir = self.root / "artifacts"

    def ensure(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)


class App:
    """AIFOS 运行时:一个 workspace 对应一套数据库、配置与产物目录。"""

    def __init__(self, root, config_overrides=None, echo_logs=False):
        self.workspace = Workspace(root)
        self.workspace.ensure()
        self.config = Config.load(
            self.workspace.config_path, overrides=config_overrides)
        self.db = Database(self.workspace.db_path)
        self.history = HistoryCenter(self.db)
        self.standards = StandardCenter(self.db, self.config)
        self.logger = Logger(self.db, self.workspace.logs_dir, echo=echo_logs)
        self.system = SystemCenter(self.db, self.logger)
        self.system.ensure_user("admin", "admin")
        self.projects = ProjectCenter(self.db)
        self.series = SeriesCenter(self.db)
        self.icloud_sync = ICloudImageSync(
            self.config, self.db, self.workspace.artifacts_dir,
            logger=self.logger)
        self.assets = AssetCenter(
            self.db, on_registered=self.icloud_sync.enqueue_asset)
        self.data = DataCenter(self.db)
        self.firefire = FireFireCenter(self.db, self.workspace.artifacts_dir)
        self.router = ProviderRouter(self.config, self.db, self.logger)
        self.qc = QcCenter(self.config)
        self.ops = OpsCenter(self.router)
        self.director = Director(
            self.db, self.config, self.logger, self.projects, self.assets,
            self.router, self.qc, self.ops, self.data,
            self.workspace.artifacts_dir, standards=self.standards)

    def close(self):
        self.icloud_sync.close()
        self.db.close()
