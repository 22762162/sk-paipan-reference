"""把已登记图片增量平铺镜像到 iCloud Drive/AIFOS，供手机快速查看。

本地 artifacts 始终是唯一事实源；本模块只在图片完整落盘并登记为资产后
复制一份，不让 iCloud 的占位文件、冲突副本或网络状态进入生产路径。
"""

import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import stat
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path

try:  # macOS/Linux 跨进程串行；Windows 测试环境退回进程内锁。
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from .asset_center import IMAGE_KINDS


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".svg"}
PROJECT_LEVEL_KINDS = {
    "character_candidate", "character_art", "character_sheet",
    "scene_art", "reference",
}
KIND_LABELS = {
    "character_candidate": "候选", "character_art": "定版",
    "character_sheet": "设定", "scene_art": "场景",
    "reference": "参考", "image": "关键帧",
    "first_frame": "首帧", "last_frame": "尾帧", "cover": "封面",
    "spatial_blocking": "空间图",
}

_ROOT_LOCKS = {}
_ROOT_LOCKS_GUARD = threading.Lock()


def _default_cloud_docs_root():
    """安全边界固定为当前用户真实 CloudDocs，不接受 workspace 自报根。"""
    return (Path.home() / "Library" / "Mobile Documents"
            / "com~apple~CloudDocs")


def _shared_root_lock(root):
    key = str(root)
    with _ROOT_LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(key, threading.RLock())


def _safe_name(value, fallback="未命名", limit=180):
    """生成 NFC 文件名；limit 按 UTF-8 字节，避免中文越过 NAME_MAX。"""
    value = unicodedata.normalize("NFC", str(value or fallback))
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = value or fallback
    encoded = value.encode("utf-8")
    if len(encoded) > limit:
        encoded = encoded[:limit]
        while encoded:
            try:
                value = encoded.decode("utf-8")
                break
            except UnicodeDecodeError:
                encoded = encoded[:-1]
    return (value or fallback).rstrip(" .")


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


class ICloudImageSync:
    """单 worker、跨进程锁、原子复制且失败不阻断生产的图片镜像。"""

    MANIFEST = ".aifos-image-sync.json"
    LOCK_FILE = ".aifos-image-sync.lock"

    def __init__(self, config, db, artifacts_root, logger=None):
        self.config = config
        self.db = db
        self.db_path = str(db.path)
        workspace_path = str(Path(self.db_path).resolve().parent)
        self.workspace_id = hashlib.sha256(
            workspace_path.encode("utf-8")).hexdigest()[:8].upper()
        self.artifacts_root = Path(artifacts_root).resolve()
        self.logger = logger
        enabled = config.get("icloud_sync", "enabled", default=False)
        self.enabled = (enabled if isinstance(enabled, bool) else
                        str(enabled).strip().lower() in
                        ("1", "true", "on", "yes"))
        configured = config.get(
            "icloud_sync", "root",
            default="~/Library/Mobile Documents/com~apple~CloudDocs/AIFOS")
        self.root = Path(str(configured)).expanduser().resolve()
        self.cloud_docs = _default_cloud_docs_root().resolve()
        self._queue = queue.Queue()
        self._worker = None
        self._worker_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._manifest_lock = _shared_root_lock(self.root)
        self._resolved_failures = set()
        self._dirty_entries = set()
        self._dirty_failures = set()
        self._last_run_dirty = False
        self._closed = False
        self._manifest = self._read_manifest_file()
        self._dirty = False

    def _root_is_safe(self):
        try:
            return os.path.commonpath(
                (str(self.root), str(self.cloud_docs))) == str(self.cloud_docs)
        except ValueError:
            return False

    def available(self):
        """根目录可尚未创建，但真实 CloudDocs 容器必须存在且可写。"""
        return (self._root_is_safe() and self.cloud_docs.is_dir()
                and os.access(str(self.cloud_docs), os.W_OK))

    @property
    def manifest_path(self):
        return self.root / self.MANIFEST

    def _empty_manifest(self):
        return {
            "schema": "aifos.icloud-image-sync/v2",
            "entries": {}, "failures": {}, "last_runs": {},
        }

    def _normalize_manifest(self, value):
        data = dict(value) if isinstance(value, dict) else self._empty_manifest()
        data.setdefault("schema", "aifos.icloud-image-sync/v1")
        for key in ("entries", "failures", "last_runs"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        return data

    def _read_manifest_file(self):
        if not self.enabled or not self._root_is_safe():
            return self._empty_manifest()
        try:
            return self._normalize_manifest(json.loads(
                self.manifest_path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return self._empty_manifest()
        except (OSError, ValueError, TypeError) as exc:
            self._log("warn", f"同步清单读取失败，将安全重建: {exc}")
            return self._empty_manifest()

    def _log(self, level, message):
        if self.logger is None:
            return
        method = getattr(self.logger, level, None)
        if method:
            try:
                method("icloud_sync", message)
            except Exception:
                # App.close 超时后主数据库可能已关闭；日志也不能杀死 worker。
                pass

    @contextmanager
    def _root_guard(self):
        """图片与 manifest 共用锁，Web/CLI 多进程也不能乱序覆盖。"""
        if not self.available():
            raise OSError("iCloud Drive 不存在、不可写或目标路径不安全")
        self.root.mkdir(parents=True, exist_ok=True)
        with self._manifest_lock:
            lock_handle = (self.root / self.LOCK_FILE).open("a+b")
            try:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

    def _merge_disk_locked(self):
        disk = self._read_manifest_file()
        local = self._normalize_manifest(self._manifest)
        entries = dict(disk["entries"])
        for key in self._dirty_entries:
            if key in local["entries"]:
                entries[key] = local["entries"][key]
        failures = dict(disk["failures"])
        for key in self._dirty_failures:
            if key in local["failures"]:
                failures[key] = local["failures"][key]
        for key in self._resolved_failures:
            failures.pop(key, None)
        merged = dict(disk)
        merged["entries"] = entries
        merged["failures"] = failures
        last_runs = dict(disk["last_runs"])
        if self._last_run_dirty:
            last_runs[self.workspace_id] = (
                local["last_runs"].get(self.workspace_id) or {})
        merged["last_runs"] = last_runs
        self._manifest = merged

    def _write_manifest_locked(self):
        """调用方已持有 _root_guard。"""
        if not self._dirty:
            return
        self._merge_disk_locked()
        self._manifest["schema"] = "aifos.icloud-image-sync/v2"
        self._manifest.pop("layout", None)  # 布局以每条 entry 为准。
        self._manifest["updated_at"] = time.time()
        temp = self.root / (f".{self.MANIFEST}.tmp.{os.getpid()}."
                            f"{uuid.uuid4().hex}")
        try:
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(self._manifest, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temp), str(self.manifest_path))
            self._dirty = False
            self._resolved_failures.clear()
            self._dirty_entries.clear()
            self._dirty_failures.clear()
            self._last_run_dirty = False
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def _write_manifest(self):
        if not self.enabled or not self._dirty:
            return
        with self._root_guard():
            self._write_manifest_locked()

    def _current_asset(self, asset_id):
        """独立只读连接：App 主 DB 关闭后后台 worker 仍可安全收尾。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            row = conn.execute(
                "SELECT a.*, p.title AS project_title, "
                "(SELECT x.id FROM assets x WHERE x.project_id=a.project_id "
                "AND x.kind=a.kind AND x.name=a.name "
                "ORDER BY x.version DESC LIMIT 1) AS latest_id "
                "FROM assets a JOIN projects p ON p.id=a.project_id "
                "WHERE a.id=?", (int(asset_id),)).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def _source(self, row):
        uri = str(row.get("uri") or "")
        if not uri or "://" in uri:
            return None
        source = Path(uri).expanduser().resolve()
        try:
            relative = source.relative_to(self.artifacts_root)
        except ValueError:
            return None
        if (".thumbs" in relative.parts
                or source.suffix.lower() not in IMAGE_SUFFIXES
                or not source.is_file()):
            return None
        return source, relative

    @staticmethod
    def _meta(row):
        raw = row.get("meta") or {}
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except ValueError:
                return {}
        return raw if isinstance(raw, dict) else {}

    def _episode_number(self, row, relative):
        meta = self._meta(row)
        for key in ("source_episode_number", "episode_number", "episode"):
            value = meta.get(key)
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                pass
        for value in (str(row.get("name") or ""), *relative.parts):
            match = re.search(r"(?:^|[^a-z])e(\d{1,4})(?:[^0-9]|$)",
                              value, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    def _target(self, row, source, relative):
        title = row.get("project_title") or "未知项目"
        kind = row["kind"]
        if kind in PROJECT_LEVEL_KINDS:
            scope = "E000"
        else:
            episode = self._episode_number(row, relative)
            scope = (f"E{episode:03d}" if episode is not None
                     else "E待归档")
        label = KIND_LABELS.get(kind, "图片")
        version = int(row.get("version") or 1)
        asset_id = int(row["id"])
        suffix = source.suffix.lower()
        prefix = f"P{int(row['project_id']):03d}_W{self.workspace_id}_"
        middle = (f"__{scope}__{label}__A{asset_id:06d}"
                  f"__v{version:03d}__")
        fixed_bytes = len((prefix + middle + suffix).encode("utf-8"))
        variable_budget = 255 - fixed_bytes
        if variable_budget < 2:
            raise OSError("资产编号过长，无法生成安全的 iCloud 文件名")
        project_budget = min(30, max(1, variable_budget // 3))
        name_budget = max(1, variable_budget - project_budget)
        project = _safe_name(title, limit=project_budget)
        name = _safe_name(row.get("name"), limit=name_budget)
        filename = f"{prefix}{project}{middle}{name}{suffix}"
        if len(filename.encode("utf-8")) > 255:
            raise OSError("iCloud 文件名超过系统限制")
        return self.root / filename

    @contextmanager
    def _managed_parent_fd(self, path):
        """在 AIFOS 根内逐层 O_NOFOLLOW 打开父目录，拒绝任何符号链接。"""
        path = Path(os.path.abspath(os.path.normpath(str(path))))
        relative = path.relative_to(self.root)
        if not relative.parts:
            raise OSError("不能把 AIFOS 根目录当作图片文件")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptors = []
        try:
            current = os.open(str(self.root), directory_flags)
            descriptors.append(current)
            for part in relative.parts[:-1]:
                current = os.open(part, directory_flags, dir_fd=current)
                descriptors.append(current)
            yield current, relative.parts[-1], relative
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _managed_digest(self, path):
        with self._managed_parent_fd(path) as (parent_fd, name, _):
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(name, flags, dir_fd=parent_fd)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("镜像路径不是普通文件")
                digest = hashlib.sha256()
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        return digest.hexdigest()
                    digest.update(chunk)
            finally:
                os.close(descriptor)

    def _unlink_managed_file(self, path, expected_digest):
        with self._managed_parent_fd(path) as (parent_fd, name, relative):
            actual = self._managed_digest(path)
            if expected_digest and actual != expected_digest:
                raise OSError("旧目录中的镜像已被修改，已保留以免丢失内容")
            os.unlink(name, dir_fd=parent_fd)
        self._prune_managed_parents(relative.parent.parts)

    def _prune_managed_parents(self, parts):
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        for depth in range(len(parts), 0, -1):
            descriptors = []
            try:
                current = os.open(str(self.root), directory_flags)
                descriptors.append(current)
                for part in parts[:depth - 1]:
                    current = os.open(part, directory_flags, dir_fd=current)
                    descriptors.append(current)
                os.rmdir(parts[depth - 1], dir_fd=current)
            except OSError:
                break
            finally:
                for descriptor in reversed(descriptors):
                    os.close(descriptor)

    def _retire_previous_target_locked(
            self, existing, replacement, key, replacement_digest):
        """新单层副本校验完成后，移除本模块登记的旧目录版镜像。"""
        old_rel = str(existing.get("target") or "")
        if not old_rel:
            return
        # 只按词法路径定位；绝不跟随 manifest 路径中的符号链接。
        old = Path(os.path.abspath(os.path.normpath(str(self.root / old_rel))))
        replacement = Path(os.path.abspath(os.path.normpath(str(replacement))))
        if old == replacement:
            return
        try:
            if os.path.commonpath((str(old), str(self.root))) != str(self.root):
                raise OSError("旧版镜像路径超出 AIFOS 目录，拒绝清理")
        except ValueError as exc:
            raise OSError("旧版镜像路径无效，拒绝清理") from exc
        try:
            if self._managed_digest(replacement) != replacement_digest:
                raise OSError("新单层镜像缺失或校验失败，已保留旧目录镜像")
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise OSError("新单层镜像缺失，已保留旧目录镜像") from exc
        if any(manifest_key != key and
               Path(os.path.abspath(os.path.normpath(
                   str(self.root / str(item.get("target") or ""))))) == old
               for manifest_key, item in
               self._manifest.get("entries", {}).items()):
            raise OSError("旧版镜像仍被其他资产引用，暂不清理")
        try:
            self._unlink_managed_file(
                old, str(existing.get("sha256") or ""))
        except FileNotFoundError:
            try:
                relative = old.relative_to(self.root)
                self._prune_managed_parents(relative.parent.parts)
            except ValueError:
                pass

    def _finish_pending_retirement_locked(self, key, target):
        entry = self._manifest["entries"].get(key) or {}
        old_rel = entry.get("previous_target")
        if not old_rel:
            return
        self._retire_previous_target_locked({
            "target": old_rel,
            "sha256": entry.get("previous_sha256"),
        }, target, key, str(entry.get("sha256") or ""))
        entry.pop("previous_target", None)
        entry.pop("previous_sha256", None)
        self._manifest["entries"][key] = entry
        self._dirty_entries.add(key)
        self._dirty = True
        self._write_manifest_locked()

    def _asset_key(self, row):
        return (f"W{self.workspace_id}:{int(row['id'])}:"
                f"v{int(row.get('version') or 1)}")

    def _copy_atomic(self, source, target, source_digest):
        target.parent.mkdir(parents=True, exist_ok=True)
        before = source.stat()
        # 临时文件名不继承可读长文件名，避免 NAME_MAX 被额外后缀撑爆。
        temp = target.parent / f".aifos-copy-{uuid.uuid4().hex}.tmp"
        try:
            shutil.copy2(str(source), str(temp))
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (
                    after.st_size, after.st_mtime_ns):
                raise OSError("源图片复制时仍在变化，已取消本次镜像")
            copied_digest = _hash_file(temp)
            if copied_digest != source_digest:
                raise OSError("复制校验失败，未替换 iCloud 图片")
            with temp.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(str(temp), str(target))
            return after, copied_digest
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def _set_failure(self, row, error):
        row = dict(row or {})
        key = self._asset_key(row) if row.get("id") else "unknown"
        self._manifest["failures"][key] = {
            "asset_id": row.get("id"), "kind": row.get("kind"),
            "name": row.get("name"), "error": error,
            "failed_at": time.time(),
        }
        self._dirty_failures.add(key)
        self._dirty = True

    def _record_failure_locked(self, row, error):
        """调用方持有根锁；失败不能晚于随后到来的成功写入。"""
        self._set_failure(row, error)
        self._write_manifest_locked()
        self._log("warn", f"图片镜像失败({row.get('kind')}/{row.get('name')}): {error}")
        return {"status": "failed", "error": error}

    def _record_failure(self, row, error):
        row = dict(row or {})
        try:
            with self._root_guard():
                self._merge_disk_locked()
                return self._record_failure_locked(row, error)
        except Exception:
            self._set_failure(row, error)
        self._log("warn", f"图片镜像失败({row.get('kind')}/{row.get('name')}): {error}")
        return {"status": "failed", "error": error}

    def sync_asset(self, row):
        """同步一条资产；所有失败转为结果，绝不向生产调用方抛出。"""
        snapshot = dict(row)
        if not self.enabled:
            return {"status": "disabled"}
        if snapshot.get("kind") not in IMAGE_KINDS:
            return {"status": "ignored", "reason": "not_image"}
        current = snapshot
        try:
            with self._root_guard():
                self._merge_disk_locked()
                try:
                    current = self._current_asset(snapshot["id"])
                    if current is None or current.get("kind") not in IMAGE_KINDS:
                        return {"status": "ignored", "reason": "missing_asset"}
                    if int(current.get("latest_id") or 0) != int(current["id"]):
                        return {"status": "ignored", "reason": "superseded"}
                    if self._meta(current).get("deleted"):
                        return {"status": "ignored", "reason": "deleted"}
                    found = self._source(current)
                    if found is None:
                        return {"status": "ignored", "reason": "invalid_source"}
                    source, relative = found
                    target = self._target(current, source, relative)
                    target_rel = target.relative_to(self.root).as_posix()
                    key = self._asset_key(current)
                    stat = source.stat()
                    source_digest = _hash_file(source)
                    existing = self._manifest["entries"].get(key) or {}
                    if (existing.get("source") == relative.as_posix()
                            and existing.get("target") == target_rel
                            and existing.get("size") == stat.st_size
                            and existing.get("mtime_ns") == stat.st_mtime_ns
                            and existing.get("sha256") == source_digest
                            and target.is_file()
                            and target.stat().st_size == stat.st_size
                            and _hash_file(target) == source_digest):
                        self._finish_pending_retirement_locked(
                            key, target)
                        if key in self._manifest["failures"]:
                            self._manifest["failures"].pop(key, None)
                            self._resolved_failures.add(key)
                            self._dirty = True
                            self._write_manifest_locked()
                        return {"status": "skipped", "target": str(target)}
                    stat, digest = self._copy_atomic(
                        source, target, source_digest)
                    entry = {
                        "workspace_id": self.workspace_id,
                        "asset_id": int(current["id"]),
                        "version": int(current.get("version") or 1),
                        "kind": current["kind"],
                        "source": relative.as_posix(), "target": target_rel,
                        "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                        "sha256": digest, "synced_at": time.time(),
                        "layout": "flat-v1",
                    }
                    previous_target = str(existing.get("target") or "")
                    if previous_target and previous_target != target_rel:
                        entry["previous_target"] = previous_target
                        entry["previous_sha256"] = existing.get("sha256")
                    self._manifest["entries"][key] = entry
                    self._dirty_entries.add(key)
                    self._manifest["failures"].pop(key, None)
                    self._resolved_failures.add(key)
                    self._dirty = True
                    self._write_manifest_locked()
                    self._finish_pending_retirement_locked(key, target)
                    return {"status": "synced", "target": str(target),
                            "bytes": stat.st_size}
                except Exception as exc:
                    return self._record_failure_locked(current, str(exc))
        except Exception as exc:  # 同步不能反向打断图片生产
            return self._record_failure(current, str(exc))

    def _ensure_worker(self):
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._run_worker, name="aifos-icloud-sync", daemon=True)
            self._worker.start()

    def enqueue_asset(self, row):
        """登记完成点只入队，不把 iCloud 复制耗时加到生图调用上。"""
        if not self.enabled:
            return
        row = dict(row)
        if row.get("kind") not in IMAGE_KINDS or not row.get("uri"):
            return
        with self._lifecycle_lock:
            if self._closed:
                return
            self._ensure_worker()
            self._queue.put(row)

    def _run_worker(self):
        while True:
            row = self._queue.get()
            try:
                if row is None:
                    return
                try:
                    self.sync_asset(row)
                except Exception as exc:  # 最后一层保护，单图不能杀死整队
                    self._log("warn", f"图片镜像 worker 异常: {exc}")
            finally:
                self._queue.task_done()

    def close(self, timeout=60):
        """有限等待；超时 worker 使用独立 DB 连接继续，不阻塞 App 关闭。"""
        if self._closed:
            return
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            worker = self._worker
            if not self._queue.empty() and (
                    worker is None or not worker.is_alive()):
                self._ensure_worker()
                worker = self._worker
            if worker is not None and worker.is_alive():
                self._queue.put(None)
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(0, timeout))
            if worker.is_alive():
                self._log("warn", "iCloud 镜像仍在后台收尾，稍后可用补同步核对")
                return
        if self._dirty:
            try:
                self._write_manifest()
            except Exception as exc:
                self._log("warn", f"同步清单保存失败: {exc}")

    def _active_rows(self):
        rows = self.db.query(
            "SELECT * FROM assets WHERE kind IN ("
            + ",".join("?" for _ in IMAGE_KINDS)
            + ") ORDER BY project_id, kind, name, version",
            tuple(IMAGE_KINDS))
        latest = {}
        for row in rows:
            latest[(row["project_id"], row["kind"], row["name"])] = row
        return [dict(row) for row in latest.values()
                if row["uri"] and not self._meta(dict(row)).get("deleted")]

    def _migrate_legacy_entry(self, key):
        """把一个清单内的旧多层镜像迁为单层；历史版本同样保留。"""
        current = None
        try:
            with self._root_guard():
                self._merge_disk_locked()
                existing = dict(
                    (self._manifest.get("entries") or {}).get(key) or {})
                if not existing or existing.get("workspace_id") != self.workspace_id:
                    return {"status": "ignored"}
                old_rel = str(existing.get("target") or "")
                if existing.get("previous_target"):
                    target = self.root / old_rel
                    self._finish_pending_retirement_locked(key, target)
                    return {"status": "migrated", "bytes": 0}
                if not old_rel or "/" not in old_rel:
                    return {"status": "skipped"}
                current = self._current_asset(existing.get("asset_id"))
                if current is None:
                    raise OSError("旧目录镜像对应的资产记录不存在，已保留原文件")
                found = self._source(current)
                if found is None:
                    raise OSError("旧目录镜像对应的本地原图不可用，已保留原文件")
                source, relative = found
                target = self._target(current, source, relative)
                digest = _hash_file(source)
                stat, copied_digest = self._copy_atomic(source, target, digest)
                entry = dict(existing)
                entry.update({
                    "source": relative.as_posix(),
                    "target": target.relative_to(self.root).as_posix(),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": copied_digest,
                    "synced_at": time.time(),
                    "layout": "flat-v1",
                    "previous_target": old_rel,
                    "previous_sha256": existing.get("sha256"),
                })
                self._manifest["entries"][key] = entry
                self._dirty_entries.add(key)
                self._dirty = True
                self._write_manifest_locked()
                self._finish_pending_retirement_locked(key, target)
                return {"status": "migrated", "bytes": stat.st_size}
        except Exception as exc:
            return self._record_failure(current or {
                "id": (self._manifest.get("entries", {}).get(key) or {}).get(
                    "asset_id"),
                "version": (self._manifest.get("entries", {}).get(key) or {}).get(
                    "version", 1),
            }, str(exc))

    def _migrate_legacy_entries(self):
        prefix = f"W{self.workspace_id}:"
        keys = [key for key, entry in
                (self._manifest.get("entries") or {}).items()
                if key.startswith(prefix)
                and ("/" in str(entry.get("target") or "")
                     or entry.get("previous_target"))]
        report = {"migrated": 0, "failed": 0, "bytes": 0, "errors": []}
        for key in keys:
            result = self._migrate_legacy_entry(key)
            if result.get("status") == "migrated":
                report["migrated"] += 1
                report["bytes"] += int(result.get("bytes") or 0)
            elif result.get("status") == "failed":
                report["failed"] += 1
                if result.get("error") and len(report["errors"]) < 20:
                    report["errors"].append(result["error"])
        return report

    def backfill(self):
        """幂等补齐当前有效图片，并迁移本模块登记的旧目录版镜像。"""
        if not self.enabled:
            return {"status": "disabled", "total": 0,
                    "synced": 0, "skipped": 0, "failed": 0,
                    "bytes": 0, "errors": ["iCloud 图片同步尚未启用"]}
        migration = self._migrate_legacy_entries()
        rows = self._active_rows()
        report = {"status": "done", "total": len(rows), "synced": 0,
                  "skipped": 0, "failed": 0, "ignored": 0,
                  "migrated": migration["migrated"],
                  "bytes": migration["bytes"],
                  "errors": list(migration["errors"])}
        report["failed"] += migration["failed"]
        for row in rows:
            result = self.sync_asset(row)
            state = result["status"]
            report[state if state in report else "ignored"] += 1
            report["bytes"] += int(result.get("bytes") or 0)
            if result.get("error") and len(report["errors"]) < 20:
                report["errors"].append(result["error"])
        report["status"] = "partial" if report["failed"] else "done"
        report["finished_at"] = time.time()
        self._manifest["last_runs"][self.workspace_id] = dict(report)
        self._last_run_dirty = True
        self._dirty = True
        try:
            self._write_manifest()
        except Exception as exc:
            report["status"] = "partial"
            report["failed"] += 1
            report["errors"].append(str(exc))
        return report

    def status(self):
        available = self.available()
        prefix = f"W{self.workspace_id}:"
        failures = {
            key: value for key, value in
            (self._manifest.get("failures") or {}).items()
            if key.startswith(prefix)
        }
        if not self.enabled:
            state = "disabled"
        elif not available:
            state = "unavailable"
        elif failures:
            state = "partial"
        else:
            state = "ready"
        entries = {
            key: value for key, value in
            (self._manifest.get("entries") or {}).items()
            if key.startswith(prefix)
        }
        last_run = (self._manifest.get("last_runs") or {}).get(
            self.workspace_id, {})
        return {
            "enabled": self.enabled, "available": available, "state": state,
            "display_path": str(self.root).replace(str(Path.home()), "~", 1),
            "synced": len(entries), "failed": len(failures),
            "running": bool(self._worker and self._worker.is_alive()),
            "last_success_at": last_run.get("finished_at"),
            "last_error": next(
                (item.get("error", "") for item in failures.values()), ""),
        }
