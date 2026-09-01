"""SQLite-backed background job ledger and job-to-output relationships.

The first version deliberately persists facts only.  It does not serialize or
replay Python callables after a process restart; callers can inspect an
``interrupted`` row and explicitly dispatch the appropriate action again.
"""

import json
import os
import sqlite3
import uuid

from .db import now
from .errors import AifosError

JOB_STATUSES = frozenset({
    "queued", "running", "cancelling", "done", "failed", "cancelled",
    "interrupted",
})
ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "cancelling"})
TERMINAL_JOB_STATUSES = frozenset({
    "done", "failed", "cancelled", "interrupted",
})
JOB_LINK_TYPES = frozenset({"episode", "shot", "artifact", "output"})
DEFAULT_LEASE_SECONDS = 120.0

_TRANSITIONS = {
    "queued": frozenset({
        "running", "failed", "cancelled", "interrupted",
    }),
    "running": frozenset({
        "cancelling", "done", "failed", "cancelled", "interrupted",
    }),
    "cancelling": frozenset({
        "done", "failed", "cancelled", "interrupted",
    }),
    # Manual recovery may reuse the same durable id.  Automatic callable
    # replay remains intentionally out of scope for this first version.
    "interrupted": frozenset({"queued", "running", "cancelled", "failed"}),
    "done": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

_UNSET = object()


def _json_object(value):
    """Return a JSON object, tolerating corrupt or legacy stored values."""
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dump_object(value, *, field):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise AifosError(f"{field} 必须是 JSON 对象")
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise AifosError(f"{field} 不能安全序列化为 JSON") from exc


def _status(value):
    normalized = str(value or "").strip().lower()
    if normalized not in JOB_STATUSES:
        raise AifosError(
            "无效任务状态：" + normalized + "；允许值为 "
            + "、".join(sorted(JOB_STATUSES)))
    return normalized


def _position(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise AifosError("队列位置必须是正整数或空值")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise AifosError("队列位置必须是正整数或空值") from exc
    if normalized < 1:
        raise AifosError("队列位置必须是正整数或空值")
    return normalized


class JobCenter:
    """Durable source of truth for Web and background production jobs."""

    def __init__(self, db, *, owner_id="", owner_pid=None, owner_host="",
                 lease_seconds=DEFAULT_LEASE_SECONDS):
        self.db = db
        self.owner_id = str(owner_id or "").strip()
        self.owner_host = str(owner_host or "").strip()
        try:
            self.owner_pid = (
                None if owner_pid is None else int(owner_pid))
            self.lease_seconds = float(lease_seconds)
        except (TypeError, ValueError) as exc:
            raise AifosError("任务 owner_pid/lease_seconds 格式无效") from exc
        if self.owner_pid is not None and self.owner_pid < 1:
            raise AifosError("任务 owner_pid 必须是正整数")
        if self.lease_seconds <= 0:
            raise AifosError("任务 lease_seconds 必须大于 0")

    def _lease_values(self, timestamp):
        if not self.owner_id:
            return "", None, "", None, None
        return (
            self.owner_id, self.owner_pid, self.owner_host, timestamp,
            timestamp + self.lease_seconds,
        )

    def _assert_active_owner(self, row):
        """Reject writes from an instance that does not own an active row."""
        if row is None or _status(row["status"]) not in ACTIVE_JOB_STATUSES:
            return
        row_owner = str(row["owner_id"] or "").strip()
        if row_owner != self.owner_id and (row_owner or self.owner_id):
            raise AifosError("任务正由其他服务实例持有，不能改写其运行状态")

    @staticmethod
    def _pid_alive(pid):
        try:
            normalized = int(pid)
        except (TypeError, ValueError):
            return False
        if normalized < 1:
            return False
        try:
            os.kill(normalized, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _job_payload(row):
        if row is None:
            return None
        item = dict(row)
        item["cancel_requested"] = bool(item.get("cancel_requested"))
        for field in ("progress", "request", "result"):
            item[field] = _json_object(item.get(field))
        return item

    @staticmethod
    def _link_payload(row):
        if row is None:
            return None
        item = dict(row)
        item["meta"] = _json_object(item.get("meta"))
        return item

    @staticmethod
    def _normalize_identity(job_id, run_id, episode_id, idempotency_key):
        normalized_id = str(job_id or "").strip()
        normalized_key = str(idempotency_key or "").strip()
        try:
            normalized_run = None if run_id is None else int(run_id)
            normalized_episode = (
                None if episode_id is None else int(episode_id))
        except (TypeError, ValueError) as exc:
            raise AifosError("run_id 和 episode_id 必须是整数或空值") from exc
        if normalized_run is not None and normalized_run < 1:
            raise AifosError("run_id 必须是正整数")
        if normalized_episode is not None and normalized_episode < 1:
            raise AifosError("episode_id 必须是正整数")
        return normalized_id, normalized_run, normalized_episode, normalized_key

    @staticmethod
    def _existing(conn, job_id, run_id, idempotency_key):
        rows = []
        if job_id:
            row = conn.execute(
                "SELECT * FROM production_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is not None:
                rows.append(row)
        if run_id is not None:
            row = conn.execute(
                "SELECT * FROM production_jobs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is not None:
                rows.append(row)
        if idempotency_key:
            row = conn.execute(
                "SELECT * FROM production_jobs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                rows.append(row)
        unique = {row["id"]: row for row in rows}
        if len(unique) > 1:
            raise AifosError("job_id、run_id 与幂等键指向不同任务")
        return next(iter(unique.values()), None)

    @staticmethod
    def _validate_existing_identity(row, *, job_id, run_id, episode_id,
                                    idempotency_key):
        # A different requested job id is allowed when run/idempotency lookup
        # already identifies the durable row.  Reusing an existing explicit id
        # with a different non-null run/key is never allowed.
        if row is None:
            return
        if (run_id is not None and row["run_id"] is not None
                and int(row["run_id"]) != run_id):
            raise AifosError("同一 job_id 不能绑定不同 run_id")
        if (episode_id is not None and row["episode_id"] is not None
                and int(row["episode_id"]) != episode_id):
            raise AifosError("同一持久任务不能绑定不同 episode_id")
        if (idempotency_key and row["idempotency_key"]
                and str(row["idempotency_key"]) != idempotency_key):
            raise AifosError("同一 job_id 不能绑定不同幂等键")

    def create(self, job_id=None, *, run_id=None, episode_id=None,
               action="adjustment", status="queued", request=None,
               progress=None, idempotency_key="", queue_key="",
               queue_position=None, links=None):
        """Idempotently create one job by explicit id, run id, or key."""
        return self._write(
            job_id=job_id, run_id=run_id, episode_id=episode_id,
            action=action, status=status,
            request={} if request is None else request,
            progress={} if progress is None else progress,
            idempotency_key=idempotency_key,
            queue_key=queue_key, queue_position=queue_position,
            links=links, update_existing=False)

    def upsert(self, job_id=None, *, run_id=None, episode_id=None,
               action=_UNSET, status=_UNSET, request=_UNSET,
               progress=_UNSET, result=_UNSET, error=_UNSET,
               idempotency_key="", queue_key=_UNSET,
               queue_position=_UNSET, links=None):
        """Create a missing job or patch explicitly supplied mutable fields."""
        return self._write(
            job_id=job_id, run_id=run_id, episode_id=episode_id,
            action=action, status=status, request=request, progress=progress,
            result=result, error=error, idempotency_key=idempotency_key,
            queue_key=queue_key, queue_position=queue_position, links=links,
            update_existing=True)

    def _write(self, *, job_id, run_id, episode_id, action, status,
               request, progress, idempotency_key, queue_key,
               queue_position, links, update_existing, result=_UNSET,
               error=_UNSET):
        (normalized_id, normalized_run, normalized_episode,
         normalized_key) = self._normalize_identity(
             job_id, run_id, episode_id, idempotency_key)
        timestamp = now()
        try:
            with self.db.transaction(immediate=True) as conn:
                existing = self._existing(
                    conn, normalized_id, normalized_run, normalized_key)
                self._validate_existing_identity(
                    existing, job_id=normalized_id, run_id=normalized_run,
                    episode_id=normalized_episode,
                    idempotency_key=normalized_key)
                if existing is None:
                    actual_id = normalized_id or (
                        "job-" + uuid.uuid4().hex)
                    actual_action = (
                        "adjustment" if action is _UNSET
                        else str(action or "adjustment").strip())
                    actual_status = _status(
                        "queued" if status is _UNSET else status)
                    actual_queue_key = (
                        "" if queue_key is _UNSET else str(queue_key or ""))
                    actual_position = _position(
                        None if queue_position is _UNSET else queue_position)
                    if (actual_position is not None
                            and actual_status != "queued"):
                        raise AifosError(
                            "只有 queued 任务可以设置队列位置")
                    actual_request = _dump_object(
                        {} if request is _UNSET else request,
                        field="request")
                    actual_progress = _dump_object(
                        {} if progress is _UNSET else progress,
                        field="progress")
                    actual_result = _dump_object(
                        {} if result is _UNSET else result,
                        field="result")
                    started_at = timestamp if actual_status == "running" else None
                    finished_at = (
                        timestamp if actual_status in TERMINAL_JOB_STATUSES
                        else None)
                    (actual_owner, actual_owner_pid, actual_owner_host,
                     heartbeat_at, lease_expires_at) = (
                        self._lease_values(timestamp)
                        if actual_status in ACTIVE_JOB_STATUSES
                        else ("", None, "", None, None))
                    conn.execute(
                        "INSERT INTO production_jobs("
                        "id, run_id, episode_id, idempotency_key, action, "
                        "status, queue_key, queue_position, progress, request, "
                        "result, error, owner_id, owner_pid, owner_host, "
                        "heartbeat_at, lease_expires_at, started_at, "
                        "finished_at, created_at, updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (actual_id, normalized_run, normalized_episode,
                         normalized_key, actual_action, actual_status,
                         actual_queue_key, actual_position, actual_progress,
                         actual_request, actual_result,
                         "" if error is _UNSET else str(error or "")[:4000],
                         actual_owner, actual_owner_pid, actual_owner_host,
                         heartbeat_at, lease_expires_at, started_at,
                         finished_at, timestamp, timestamp),
                    )
                else:
                    actual_id = str(existing["id"])
                    if not update_existing:
                        if normalized_episode is not None:
                            self._upsert_link_conn(
                                conn, actual_id, "episode",
                                str(normalized_episode), role="owner", uri="",
                                meta={}, timestamp=timestamp)
                        for link in links or []:
                            if not isinstance(link, dict):
                                raise AifosError(
                                    "任务关联必须是 JSON 对象")
                            self._upsert_link_conn(
                                conn, actual_id, link.get("relation_type"),
                                link.get("relation_id"),
                                role=link.get("role", ""),
                                uri=link.get("uri", ""),
                                meta=link.get("meta", {}),
                                timestamp=timestamp)
                        return self.get(actual_id)
                    updates, params = [], []
                    fields = {
                        "action": (action, lambda value: str(
                            value or "adjustment").strip()),
                        "request": (request, lambda value: _dump_object(
                            value, field="request")),
                        "progress": (progress, lambda value: _dump_object(
                            value, field="progress")),
                        "result": (result, lambda value: _dump_object(
                            value, field="result")),
                        "error": (error, lambda value: str(value or "")[:4000]),
                        "queue_key": (queue_key, lambda value: str(value or "")),
                    }
                    current_status = _status(existing["status"])
                    self._assert_active_owner(existing)
                    effective_status = current_status
                    if status is not _UNSET:
                        effective_status = _status(status)
                        if (effective_status != current_status
                                and effective_status
                                not in _TRANSITIONS[current_status]):
                            raise AifosError(
                                "不允许任务状态从 " + current_status
                                + " 变为 " + effective_status)
                        updates.append("status=?")
                        params.append(effective_status)
                        if (effective_status != current_status
                                and effective_status == "running"):
                            updates.extend((
                                "started_at=COALESCE(started_at, ?)",
                                "finished_at=NULL", "queue_position=NULL"))
                            params.append(timestamp)
                        elif (effective_status != current_status
                              and effective_status == "queued"):
                            updates.extend(("started_at=NULL",
                                            "finished_at=NULL"))
                        elif (effective_status != current_status
                              and effective_status
                              in TERMINAL_JOB_STATUSES):
                            updates.extend(("finished_at=?",
                                            "queue_position=NULL",
                                            "lease_expires_at=NULL"))
                            params.append(timestamp)
                    if (effective_status in ACTIVE_JOB_STATUSES
                            and self.owner_id):
                        (owner_id, owner_pid, owner_host, heartbeat_at,
                         lease_expires_at) = self._lease_values(timestamp)
                        updates.extend((
                            "owner_id=?", "owner_pid=?", "owner_host=?",
                            "heartbeat_at=?", "lease_expires_at=?"))
                        params.extend((owner_id, owner_pid, owner_host,
                                       heartbeat_at, lease_expires_at))
                    if queue_position is not _UNSET:
                        actual_position = _position(queue_position)
                        if (actual_position is not None
                                and effective_status != "queued"):
                            raise AifosError(
                                "只有 queued 任务可以设置队列位置")
                        fields["queue_position"] = (
                            actual_position, lambda value: value)
                    for column, (value, normalize) in fields.items():
                        if value is _UNSET:
                            continue
                        updates.append(column + "=?")
                        params.append(normalize(value))
                    if normalized_run is not None and existing["run_id"] is None:
                        updates.append("run_id=?")
                        params.append(normalized_run)
                    if (normalized_episode is not None
                            and existing["episode_id"] is None):
                        updates.append("episode_id=?")
                        params.append(normalized_episode)
                    if normalized_key and not existing["idempotency_key"]:
                        updates.append("idempotency_key=?")
                        params.append(normalized_key)
                    if updates:
                        updates.append("updated_at=?")
                        params.extend((timestamp, actual_id))
                        conn.execute(
                            "UPDATE production_jobs SET "
                            + ", ".join(updates) + " WHERE id=?", params)
                if normalized_episode is not None:
                    self._upsert_link_conn(
                        conn, actual_id, "episode", str(normalized_episode),
                        role="owner", uri="", meta={}, timestamp=timestamp)
                for link in links or []:
                    if not isinstance(link, dict):
                        raise AifosError("任务关联必须是 JSON 对象")
                    self._upsert_link_conn(
                        conn, actual_id, link.get("relation_type"),
                        link.get("relation_id"), role=link.get("role", ""),
                        uri=link.get("uri", ""), meta=link.get("meta", {}),
                        timestamp=timestamp)
        except sqlite3.IntegrityError as exc:
            raise AifosError("持久任务关联的 run、幂等键或外键无效") from exc
        return self.get(actual_id)

    def get(self, job_id, *, include_links=True):
        row = self.db.query_one(
            "SELECT * FROM production_jobs WHERE id=?",
            (str(job_id or "").strip(),))
        item = self._job_payload(row)
        if item is not None and include_links:
            item["links"] = self.links(item["id"])
        return item

    def list(self, *, statuses=None, run_id=None, episode_id=None,
             action=None, queue_key=None, cancel_requested=None, limit=200,
             include_links=False):
        where, params = [], []
        if statuses:
            values = [statuses] if isinstance(statuses, str) else list(statuses)
            normalized = [_status(value) for value in values]
            placeholders = ",".join("?" for _ in normalized)
            where.append("status IN (" + placeholders + ")")
            params.extend(normalized)
        if run_id is not None:
            where.append("run_id=?")
            params.append(int(run_id))
        if episode_id is not None:
            where.append("episode_id=?")
            params.append(int(episode_id))
        if action is not None:
            where.append("action=?")
            params.append(str(action))
        if queue_key is not None:
            where.append("queue_key=?")
            params.append(str(queue_key))
        if cancel_requested is not None:
            where.append("cancel_requested=?")
            params.append(1 if cancel_requested else 0)
        try:
            bounded_limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError) as exc:
            raise AifosError("任务列表 limit 必须是整数") from exc
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.db.query(
            "SELECT * FROM production_jobs" + clause
            + " ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, bounded_limit))
        items = [self._job_payload(row) for row in rows]
        if include_links:
            for item in items:
                item["links"] = self.links(item["id"])
        return items

    def set_progress(self, job_id, progress):
        payload = _dump_object(progress, field="progress")
        normalized_id = str(job_id or "").strip()
        timestamp = now()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM production_jobs WHERE id=?", (normalized_id,)
            ).fetchone()
            if row is None:
                raise AifosError("任务不存在")
            self._assert_active_owner(row)
            updates = ["progress=?", "updated_at=?"]
            params = [payload, timestamp]
            if _status(row["status"]) in ACTIVE_JOB_STATUSES and self.owner_id:
                updates.extend(("heartbeat_at=?", "lease_expires_at=?"))
                params.extend((timestamp, timestamp + self.lease_seconds))
            params.append(normalized_id)
            conn.execute(
                "UPDATE production_jobs SET " + ", ".join(updates)
                + " WHERE id=?", params)
        return self.get(job_id)

    def progress(self, job_id, value=_UNSET):
        if value is not _UNSET:
            return self.set_progress(job_id, value)
        item = self.get(job_id, include_links=False)
        if item is None:
            raise AifosError("任务不存在")
        return item["progress"]

    def set_state(self, job_id, status, *, expected_status=None, result=_UNSET,
                  error=_UNSET):
        target = _status(status)
        timestamp = now()
        normalized_id = str(job_id or "").strip()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM production_jobs WHERE id=?", (normalized_id,)
            ).fetchone()
            if row is None:
                raise AifosError("任务不存在")
            current = _status(row["status"])
            if expected_status is not None and current != _status(expected_status):
                raise AifosError(
                    f"任务状态已变化：预期 {expected_status}，实际 {current}")
            self._assert_active_owner(row)
            if target != current and target not in _TRANSITIONS[current]:
                raise AifosError(f"不允许任务状态从 {current} 变为 {target}")
            updates, params = ["status=?", "updated_at=?"], [target, timestamp]
            if target == "running":
                updates.extend(("started_at=COALESCE(started_at, ?)",
                                "finished_at=NULL", "queue_position=NULL"))
                params.append(timestamp)
            elif target == "queued":
                updates.extend(("started_at=NULL", "finished_at=NULL"))
            elif target in TERMINAL_JOB_STATUSES:
                updates.extend(("finished_at=?", "queue_position=NULL",
                                "lease_expires_at=NULL"))
                params.append(timestamp)
            if target in ACTIVE_JOB_STATUSES and self.owner_id:
                (owner_id, owner_pid, owner_host, heartbeat_at,
                 lease_expires_at) = self._lease_values(timestamp)
                updates.extend((
                    "owner_id=?", "owner_pid=?", "owner_host=?",
                    "heartbeat_at=?", "lease_expires_at=?"))
                params.extend((owner_id, owner_pid, owner_host, heartbeat_at,
                               lease_expires_at))
            if result is not _UNSET:
                updates.append("result=?")
                params.append(_dump_object(result, field="result"))
            if error is not _UNSET:
                updates.append("error=?")
                params.append(str(error or "")[:4000])
            params.append(normalized_id)
            conn.execute(
                "UPDATE production_jobs SET " + ", ".join(updates)
                + " WHERE id=?", params)
        return self.get(normalized_id)

    def state(self, job_id, value=_UNSET, **kwargs):
        if value is not _UNSET:
            return self.set_state(job_id, value, **kwargs)
        item = self.get(job_id, include_links=False)
        if item is None:
            raise AifosError("任务不存在")
        return item["status"]

    def set_queue_position(self, job_id, position):
        normalized = _position(position)
        normalized_id = str(job_id or "").strip()
        timestamp = now()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM production_jobs WHERE id=?", (normalized_id,)
            ).fetchone()
            if row is None:
                raise AifosError("任务不存在")
            self._assert_active_owner(row)
            if normalized is not None and row["status"] != "queued":
                raise AifosError("只有 queued 任务可以设置队列位置")
            updates = ["queue_position=?", "updated_at=?"]
            params = [normalized, timestamp]
            if _status(row["status"]) in ACTIVE_JOB_STATUSES and self.owner_id:
                updates.extend(("heartbeat_at=?", "lease_expires_at=?"))
                params.extend((timestamp, timestamp + self.lease_seconds))
            params.append(normalized_id)
            conn.execute(
                "UPDATE production_jobs SET " + ", ".join(updates)
                + " WHERE id=?", params)
        return self.get(job_id)

    def queue_position(self, job_id, value=_UNSET):
        if value is not _UNSET:
            return self.set_queue_position(job_id, value)
        item = self.get(job_id, include_links=False)
        if item is None:
            raise AifosError("任务不存在")
        return item["queue_position"]

    def request_cancel(self, job_id, reason=""):
        normalized_id = str(job_id or "").strip()
        timestamp = now()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT status FROM production_jobs WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise AifosError("任务不存在")
            current = _status(row["status"])
            target = current
            finished_at = None
            if current == "queued":
                target, finished_at = "cancelled", timestamp
            elif current == "running":
                target = "cancelling"
            elif current == "interrupted":
                target, finished_at = "cancelled", timestamp
            conn.execute(
                "UPDATE production_jobs SET status=?, cancel_requested=1, "
                "cancel_reason=?, finished_at=COALESCE(?, finished_at), "
                "queue_position=CASE WHEN ?='cancelled' THEN NULL "
                "ELSE queue_position END, "
                "lease_expires_at=CASE WHEN ?='cancelled' THEN NULL "
                "ELSE lease_expires_at END, updated_at=? WHERE id=?",
                (target, str(reason or "")[:1000], finished_at, target,
                 target, timestamp, normalized_id),
            )
        return self.get(normalized_id)

    def cancel_requested(self, job_id):
        item = self.get(job_id, include_links=False)
        if item is None:
            raise AifosError("任务不存在")
        return bool(item["cancel_requested"])

    @staticmethod
    def _upsert_link_conn(conn, job_id, relation_type, relation_id, *, role,
                          uri, meta, timestamp):
        normalized_type = str(relation_type or "").strip().lower()
        if normalized_type not in JOB_LINK_TYPES:
            raise AifosError(
                "无效任务关联类型：" + normalized_type + "；允许值为 "
                + "、".join(sorted(JOB_LINK_TYPES)))
        normalized_id = str(relation_id or "").strip()
        if not normalized_id:
            raise AifosError("任务关联 relation_id 不能为空")
        normalized_role = str(role or "").strip()
        payload = _dump_object(
            {} if meta is None else meta, field="link.meta")
        conn.execute(
            "INSERT INTO production_job_links("
            "job_id, relation_type, relation_id, role, uri, meta, created_at, "
            "updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(job_id, relation_type, relation_id, role) DO UPDATE "
            "SET uri=excluded.uri, meta=excluded.meta, "
            "updated_at=excluded.updated_at",
            (job_id, normalized_type, normalized_id, normalized_role,
             str(uri or ""), payload, timestamp, timestamp),
        )

    def upsert_link(self, job_id, relation_type, relation_id, *, role="",
                    uri="", meta=None):
        normalized_job = str(job_id or "").strip()
        timestamp = now()
        try:
            with self.db.transaction(immediate=True) as conn:
                self._upsert_link_conn(
                    conn, normalized_job, relation_type, relation_id,
                    role=role, uri=uri,
                    meta={} if meta is None else meta, timestamp=timestamp)
        except sqlite3.IntegrityError as exc:
            raise AifosError("任务不存在，不能登记关联产物") from exc
        return next((
            item for item in self.links(normalized_job)
            if item["relation_type"] == str(relation_type).strip().lower()
            and item["relation_id"] == str(relation_id).strip()
            and item["role"] == str(role or "").strip()
        ), None)

    def links(self, job_id, relation_type=None):
        params = [str(job_id or "").strip()]
        sql = "SELECT * FROM production_job_links WHERE job_id=?"
        if relation_type is not None:
            normalized_type = str(relation_type or "").strip().lower()
            if normalized_type not in JOB_LINK_TYPES:
                raise AifosError("无效任务关联类型：" + normalized_type)
            sql += " AND relation_type=?"
            params.append(normalized_type)
        sql += " ORDER BY id"
        return [self._link_payload(row) for row in self.db.query(sql, params)]

    def renew_owned_leases(self):
        """Extend leases for this process without claiming another owner."""
        if not self.owner_id:
            return 0
        timestamp = now()
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
        cur = self.db.execute(
            "UPDATE production_jobs SET heartbeat_at=?, lease_expires_at=? "
            "WHERE owner_id=? AND status IN (" + placeholders + ")",
            (timestamp, timestamp + self.lease_seconds, self.owner_id,
             *sorted(ACTIVE_JOB_STATUSES)),
        )
        return int(cur.rowcount or 0)

    def _row_owner_is_stale(self, row, timestamp):
        row_owner = str(row["owner_id"] or "").strip()
        if row_owner and row_owner == self.owner_id:
            return False
        lease = row["lease_expires_at"]
        lease_expired = lease is not None and float(lease) <= timestamp
        if row_owner:
            same_host = bool(
                self.owner_host and row["owner_host"]
                and str(row["owner_host"]) == self.owner_host)
            pid_dead = bool(
                same_host and row["owner_pid"] is not None
                and not self._pid_alive(row["owner_pid"]))
            return lease_expired or pid_dead
        # Legacy/unowned rows cannot be declared dead merely because a second
        # process opened the database.  Only age past a full lease proves that
        # no compatible owner has refreshed them.
        updated_at = float(row["updated_at"] or 0)
        return lease_expired or updated_at <= timestamp - self.lease_seconds

    def bootstrap_interrupt(self, reason="服务重启，原后台任务已中断",
                            *, at=None, return_jobs=False):
        """Interrupt only active rows whose owner is provably stale.

        A second Web process may share the same SQLite ledger during a rolling
        restart or local diagnostic session.  Fresh leases and live same-host
        PIDs therefore survive bootstrap; dead owners, expired leases, and old
        ownerless legacy rows are landed as ``interrupted`` atomically.
        """
        timestamp = now() if at is None else float(at)
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
        interrupted = []
        with self.db.transaction(immediate=True) as conn:
            rows = conn.execute(
                "SELECT * FROM production_jobs WHERE status IN ("
                + placeholders + ")",
                tuple(sorted(ACTIVE_JOB_STATUSES)),
            ).fetchall()
            for row in rows:
                if not self._row_owner_is_stale(row, timestamp):
                    continue
                cur = conn.execute(
                    "UPDATE production_jobs SET status='interrupted', "
                    "error=CASE WHEN error='' THEN ? ELSE error END, "
                    "finished_at=COALESCE(finished_at, ?), "
                    "queue_position=NULL, lease_expires_at=NULL, "
                    "updated_at=? WHERE id=? AND status IN ("
                    + placeholders + ")",
                    (str(reason or "")[:4000], timestamp, timestamp,
                     row["id"], *sorted(ACTIVE_JOB_STATUSES)),
                )
                if cur.rowcount:
                    recovered = conn.execute(
                        "SELECT * FROM production_jobs WHERE id=?",
                        (row["id"],),
                    ).fetchone()
                    if recovered is not None:
                        interrupted.append(self._job_payload(recovered))
        return interrupted if return_jobs else len(interrupted)

    def bootstrap(self, reason="服务重启，原后台任务已中断"):
        return self.bootstrap_interrupt(reason=reason)
