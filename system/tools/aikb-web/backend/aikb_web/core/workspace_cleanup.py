"""Workspace 数据维护的安全扫描、短期预览和受控删除核心。

首版只处理三类能够从固定事实源可靠判定为过期的数据：审计文件、终态归档
Working State 和终态 Web 任务。浏览器永远不能提供路径；规则/安装事务、活动
任务、锁及无法确定状态的对象一律留在保护集合中。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .actions import ActionError, ConfirmationTokenService
from .maintenance_lock import MaintenanceLockError, MaintenanceWriteLock


CATEGORY_DEFAULTS = {"audit": 90, "archived_work": 180, "web_tasks": 30}
CATEGORY_LABELS = {"audit": "审计数据", "archived_work": "归档运行任务", "web_tasks": "终态 Web 任务"}
TERMINAL_WORK_STATES = frozenset({"completed", "abandoned", "superseded"})
TERMINAL_TASK_STATES = frozenset({"succeeded", "failed", "cancelled", "timed_out", "interrupted"})
PLAN_TTL_SECONDS = 300
MAX_PLANS = 64
MAX_CANDIDATES = 20_000
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")


class WorkspaceCleanupError(RuntimeError):
    """数据维护请求无法安全完成；只携带固定错误码和安全计数。"""

    def __init__(self, message: str, *, code: str = "DATA_MAINTENANCE_UNAVAILABLE", status_code: int = 503, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = dict(details or {})


@dataclass(frozen=True)
class CleanupCandidate:
    """仅存在服务端内存中的删除候选；物理路径不进入公开响应。"""

    category: str
    object_id: str
    path: Path
    boundary: Path
    is_directory: bool
    bytes: int
    modified_ns: int
    fingerprint: str

    def digest_dict(self) -> dict[str, Any]:
        """返回用于陈旧预览校验的无路径稳定字段。"""

        return {
            "category": self.category,
            "object_id": self.object_id,
            "is_directory": self.is_directory,
            "bytes": self.bytes,
            "modified_ns": self.modified_ns,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class CleanupPlan:
    """绑定固定策略、候选摘要和一次性令牌的进程内短期计划。"""

    plan_id: str
    categories: tuple[str, ...]
    retention_days: dict[str, int]
    candidates: tuple[CleanupCandidate, ...]
    summaries: tuple[dict[str, Any], ...]
    preview_digest: str
    confirmation_token: str
    expires_at: float


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_reparse(path: Path) -> bool:
    """同时识别 POSIX symlink 和 Windows reparse point。"""

    try:
        info = path.lstat()
    except OSError:
        return True
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & reparse)


class WorkspaceCleanupService:
    """在可信 workspace 边界内生成并应用固定类别的数据清理计划。"""

    def __init__(
        self,
        workspace_root: Path | str,
        *,
        token_service: ConfirmationTokenService | None = None,
        write_lock: MaintenanceWriteLock | None = None,
        audit_sink: Callable[[Mapping[str, Any]], Any] | None = None,
        deleted_sink: Callable[[str, str], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        root = Path(workspace_root)
        if not root.is_absolute():
            raise WorkspaceCleanupError("workspace 边界无效")
        self.workspace_root = root.resolve()
        self.tokens = token_service or ConfirmationTokenService(clock=clock)
        self.write_lock = write_lock or MaintenanceWriteLock(self.workspace_root)
        self.audit_sink = audit_sink
        self.deleted_sink = deleted_sink
        self.clock = clock
        self._plans: dict[str, CleanupPlan] = {}
        self._plans_lock = threading.RLock()

    def overview(self) -> dict[str, Any]:
        """返回默认保留策略下的候选/保护计数；只读且不签发令牌。"""

        categories = tuple(CATEGORY_DEFAULTS)
        _, summaries, protected = self._scan(categories, dict(CATEGORY_DEFAULTS))
        return {
            "categories": summaries,
            "protected": protected,
            "defaults": dict(CATEGORY_DEFAULTS),
            "apply_supported": True,
            "scan_scope": "fixed_workspace_categories",
        }

    def preview(self, *, categories: Sequence[str] | None, retention_days: Mapping[str, int] | None) -> dict[str, Any]:
        """生成五分钟短期清理计划；扫描和返回均不删除任何内容。"""

        selected, normalized_days = self._normalize_policy(categories, retention_days)
        candidates, summaries, protected = self._scan(selected, normalized_days)
        digest = self._plan_digest(selected, normalized_days, candidates)
        plan_id = f"cleanup-{uuid.uuid4().hex}"
        parameters = {"categories": list(selected), "retention_days": normalized_days}
        with self._plans_lock:
            self._discard_expired_plans()
            if len(self._plans) >= MAX_PLANS:
                raise WorkspaceCleanupError("待确认清理计划过多，请稍后重试", code="DATA_MAINTENANCE_BUSY", status_code=409)
            try:
                token = self.tokens.issue(
                    action_id="workspace.cleanup",
                    parameters=parameters,
                    risk_level="destructive_local_data",
                    preview_digest=digest,
                )
            except ActionError as error:
                raise WorkspaceCleanupError("待确认清理计划过多，请稍后重试", code="DATA_MAINTENANCE_BUSY", status_code=409) from error
            plan = CleanupPlan(
                plan_id=plan_id,
                categories=selected,
                retention_days=normalized_days,
                candidates=tuple(candidates),
                summaries=tuple(summaries),
                preview_digest=digest,
                confirmation_token=token,
                expires_at=self.clock() + PLAN_TTL_SECONDS,
            )
            self._plans[plan_id] = plan
        return self._public_plan(plan, protected)

    def apply(self, plan_id: str, confirmation_token: str) -> dict[str, Any]:
        """锁内重新扫描并消费令牌，然后仅删除预览中完全一致的候选。"""

        if not _SAFE_ID.fullmatch(str(plan_id or "")):
            raise WorkspaceCleanupError("清理计划不存在", code="not_found", status_code=404)
        with self._plans_lock:
            self._discard_expired_plans()
            plan = self._plans.get(plan_id)
        if plan is None:
            raise WorkspaceCleanupError("清理计划不存在或已过期", code="DATA_MAINTENANCE_PLAN_EXPIRED", status_code=409)
        parameters = {"categories": list(plan.categories), "retention_days": plan.retention_days}
        try:
            self.tokens.validate(
                confirmation_token,
                action_id="workspace.cleanup",
                parameters=parameters,
                risk_level="destructive_local_data",
                preview_digest=plan.preview_digest,
            )
        except ActionError as error:
            raise WorkspaceCleanupError("确认令牌无效、已消费或已过期", code="DATA_MAINTENANCE_CONFIRMATION_INVALID", status_code=409) from error
        try:
            with self.write_lock.held(timeout=0):
                fresh, _summaries, _protected = self._scan(plan.categories, plan.retention_days)
                if self._plan_digest(plan.categories, plan.retention_days, fresh) != plan.preview_digest:
                    raise WorkspaceCleanupError("待清理数据已发生变化，请重新预览", code="DATA_MAINTENANCE_STALE_PREVIEW", status_code=409)
                self.tokens.consume(
                    confirmation_token,
                    action_id="workspace.cleanup",
                    parameters=parameters,
                    risk_level="destructive_local_data",
                    preview_digest=plan.preview_digest,
                )
                deleted_count = 0
                deleted_bytes = 0
                deleted_candidates: list[CleanupCandidate] = []
                for candidate in fresh:
                    try:
                        self._delete_candidate(candidate)
                    except OSError as error:
                        self._notify_deleted(deleted_candidates)
                        self._audit("failed", plan, deleted_count, deleted_bytes)
                        raise WorkspaceCleanupError(
                            "清理执行未完整完成，请重新扫描核对",
                            code="DATA_MAINTENANCE_PARTIAL_FAILURE",
                            status_code=500,
                            details={"deleted_count": deleted_count, "deleted_bytes": deleted_bytes},
                        ) from error
                    deleted_count += 1
                    deleted_bytes += candidate.bytes
                    deleted_candidates.append(candidate)
        except WorkspaceCleanupError:
            raise
        except MaintenanceLockError as error:
            raise WorkspaceCleanupError("其他维护写入正在执行，请稍后重试", code="DATA_MAINTENANCE_BUSY", status_code=409) from error
        with self._plans_lock:
            self._plans.pop(plan_id, None)
        self._notify_deleted(deleted_candidates)
        self._audit("succeeded", plan, deleted_count, deleted_bytes)
        return {
            "plan_id": plan.plan_id,
            "status": "succeeded",
            "deleted_count": deleted_count,
            "deleted_bytes": deleted_bytes,
            "categories": list(plan.categories),
        }

    def _normalize_policy(self, categories: Sequence[str] | None, retention_days: Mapping[str, int] | None) -> tuple[tuple[str, ...], dict[str, int]]:
        """只接受固定类别和 1～36500 天保留期，不允许空选择。"""

        raw = list(categories) if categories is not None else list(CATEGORY_DEFAULTS)
        if not raw or len(raw) > len(CATEGORY_DEFAULTS) or any(item not in CATEGORY_DEFAULTS for item in raw):
            raise WorkspaceCleanupError("清理类别无效", code="invalid_request", status_code=422)
        selected = tuple(item for item in CATEGORY_DEFAULTS if item in set(raw))
        values = dict(CATEGORY_DEFAULTS)
        for key, value in dict(retention_days or {}).items():
            if key not in CATEGORY_DEFAULTS or isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 36500:
                raise WorkspaceCleanupError("保留期无效", code="invalid_request", status_code=422)
            values[key] = value
        return selected, {key: values[key] for key in selected}

    def _scan(self, categories: Sequence[str], retention_days: Mapping[str, int]) -> tuple[list[CleanupCandidate], list[dict[str, Any]], dict[str, Any]]:
        """低频维护扫描；普通运行/审计页面不得调用此入口。"""

        now = datetime.fromtimestamp(self.clock(), tz=timezone.utc)
        candidates: list[CleanupCandidate] = []
        protected_by_reason: dict[str, int] = {}
        summaries: list[dict[str, Any]] = []
        for category in categories:
            cutoff = now - timedelta(days=retention_days[category])
            if category == "audit":
                found, protected = self._scan_audit(cutoff)
            elif category == "archived_work":
                found, protected = self._scan_archived_work(cutoff)
            else:
                found, protected = self._scan_web_tasks(cutoff)
            if len(candidates) + len(found) > MAX_CANDIDATES:
                raise WorkspaceCleanupError("候选数量超过单次安全上限，请缩小范围", code="DATA_MAINTENANCE_LIMIT", status_code=409)
            candidates.extend(found)
            for reason, count in protected.items():
                protected_by_reason[reason] = protected_by_reason.get(reason, 0) + count
            summaries.append({
                "id": category,
                "label": CATEGORY_LABELS[category],
                "retention_days": retention_days[category],
                "candidate_count": len(found),
                "candidate_bytes": sum(item.bytes for item in found),
                "protected_count": sum(protected.values()),
            })
        candidates.sort(key=lambda item: (item.category, item.object_id, item.fingerprint))
        return candidates, summaries, {
            "count": sum(protected_by_reason.values()),
            "reasons": [{"code": key, "count": value} for key, value in sorted(protected_by_reason.items())],
        }

    def _scan_audit(self, cutoff: datetime) -> tuple[list[CleanupCandidate], dict[str, int]]:
        root = self.workspace_root / "audit"
        candidates: list[CleanupCandidate] = []
        protected: dict[str, int] = {}
        for name in ("events", "diagnostic", "fallback", "reports"):
            boundary = root / name
            if not boundary.is_dir() or _is_reparse(boundary):
                continue
            for path in self._safe_files(boundary, protected=protected):
                try:
                    info = path.stat()
                except OSError:
                    protected["unreadable"] = protected.get("unreadable", 0) + 1
                    continue
                if datetime.fromtimestamp(info.st_mtime, tz=timezone.utc) >= cutoff:
                    protected["within_retention"] = protected.get("within_retention", 0) + 1
                    continue
                candidates.append(self._candidate("audit", path, boundary, False, f"audit-{hashlib.sha256(str(path.relative_to(root)).encode()).hexdigest()[:12]}"))
        return candidates, protected

    def _scan_archived_work(self, cutoff: datetime) -> tuple[list[CleanupCandidate], dict[str, int]]:
        root = self.workspace_root / "archive"
        candidates: list[CleanupCandidate] = []
        protected: dict[str, int] = {}
        if not root.is_dir() or _is_reparse(root):
            return candidates, protected
        for work_file in self._safe_files(root, wanted_name="work.md", protected=protected):
            try:
                text = work_file.read_text(encoding="utf-8")[:65536]
                status = self._frontmatter_value(text, "status")
                work_id = self._frontmatter_value(text, "work_id")
                info = work_file.stat()
            except (OSError, UnicodeError):
                protected["unreadable"] = protected.get("unreadable", 0) + 1
                continue
            if status not in TERMINAL_WORK_STATES or not work_id or not _SAFE_ID.fullmatch(work_id):
                protected["uncertain_or_active"] = protected.get("uncertain_or_active", 0) + 1
                continue
            if datetime.fromtimestamp(info.st_mtime, tz=timezone.utc) >= cutoff:
                protected["within_retention"] = protected.get("within_retention", 0) + 1
                continue
            try:
                candidates.append(self._candidate("archived_work", work_file.parent, root, True, work_id))
            except WorkspaceCleanupError:
                protected["unsafe_object"] = protected.get("unsafe_object", 0) + 1
        return candidates, protected

    def _scan_web_tasks(self, cutoff: datetime) -> tuple[list[CleanupCandidate], dict[str, int]]:
        root = self.workspace_root / "runtime" / "web" / "tasks"
        candidates: list[CleanupCandidate] = []
        protected: dict[str, int] = {}
        if not root.is_dir() or _is_reparse(root):
            return candidates, protected
        for task_dir in root.glob("*/*/*"):
            if not task_dir.is_dir() or _is_reparse(task_dir) or not _SAFE_ID.fullmatch(task_dir.name):
                protected["uncertain_or_active"] = protected.get("uncertain_or_active", 0) + 1
                continue
            snapshot = task_dir / "snapshot.json"
            try:
                if snapshot.stat().st_size > 1024 * 1024:
                    raise ValueError("snapshot too large")
                data = json.loads(snapshot.read_text(encoding="utf-8"))
                status = str(data.get("status") or "") if isinstance(data, dict) else ""
                timestamp = self._parse_time(data.get("updated_at") if isinstance(data, dict) else None)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                protected["unreadable"] = protected.get("unreadable", 0) + 1
                continue
            if status not in TERMINAL_TASK_STATES or timestamp is None:
                protected["uncertain_or_active"] = protected.get("uncertain_or_active", 0) + 1
                continue
            if timestamp >= cutoff:
                protected["within_retention"] = protected.get("within_retention", 0) + 1
                continue
            try:
                candidates.append(self._candidate("web_tasks", task_dir, root, True, task_dir.name))
            except WorkspaceCleanupError:
                protected["unsafe_object"] = protected.get("unsafe_object", 0) + 1
        return candidates, protected

    def _candidate(self, category: str, path: Path, boundary: Path, is_directory: bool, object_id: str) -> CleanupCandidate:
        resolved = path.resolve()
        resolved.relative_to(boundary.resolve())
        if _is_reparse(path):
            raise WorkspaceCleanupError("检测到不可安全处理的链接对象", code="DATA_MAINTENANCE_UNSAFE_OBJECT", status_code=409)
        entries: list[tuple[str, int, int]] = []
        if is_directory:
            for file in self._safe_files(path):
                info = file.stat()
                entries.append((str(file.relative_to(path)).replace("\\", "/"), info.st_size, info.st_mtime_ns))
        else:
            info = path.stat()
            entries.append((path.name, info.st_size, info.st_mtime_ns))
        fingerprint = hashlib.sha256(_canonical(entries).encode("utf-8")).hexdigest()
        return CleanupCandidate(category, object_id, path, boundary, is_directory, sum(item[1] for item in entries), max((item[2] for item in entries), default=0), fingerprint)

    def _safe_files(
        self,
        root: Path,
        *,
        wanted_name: str | None = None,
        protected: dict[str, int] | None = None,
    ) -> Iterable[Path]:
        """递归枚举普通文件；目录候选内的链接或不可读项会使整个候选失效。"""

        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                if protected is not None:
                    protected["unreadable"] = protected.get("unreadable", 0) + 1
                    continue
                raise WorkspaceCleanupError("候选目录无法完整读取", code="DATA_MAINTENANCE_UNSAFE_OBJECT", status_code=409)
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink() or _is_reparse(path):
                    if protected is not None:
                        protected["unsafe_object"] = protected.get("unsafe_object", 0) + 1
                        continue
                    raise WorkspaceCleanupError("候选包含不可安全处理的链接对象", code="DATA_MAINTENANCE_UNSAFE_OBJECT", status_code=409)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(path)
                    elif entry.is_file(follow_symlinks=False) and (wanted_name is None or entry.name == wanted_name):
                        yield path
                    elif not entry.is_file(follow_symlinks=False):
                        if protected is not None:
                            protected["unsafe_object"] = protected.get("unsafe_object", 0) + 1
                            continue
                        raise WorkspaceCleanupError("候选包含未知对象", code="DATA_MAINTENANCE_UNSAFE_OBJECT", status_code=409)
                except OSError as error:
                    if protected is not None:
                        protected["unreadable"] = protected.get("unreadable", 0) + 1
                        continue
                    raise WorkspaceCleanupError("候选目录无法完整读取", code="DATA_MAINTENANCE_UNSAFE_OBJECT", status_code=409) from error

    def _delete_candidate(self, candidate: CleanupCandidate) -> None:
        """再次验证边界和链接属性后删除单个服务端候选。"""

        resolved = candidate.path.resolve(strict=True)
        resolved.relative_to(candidate.boundary.resolve(strict=True))
        if _is_reparse(candidate.path):
            raise OSError("unsafe object")
        current = self._candidate(candidate.category, candidate.path, candidate.boundary, candidate.is_directory, candidate.object_id)
        if current.fingerprint != candidate.fingerprint:
            raise OSError("candidate changed")
        if candidate.is_directory:
            shutil.rmtree(candidate.path)
        else:
            candidate.path.unlink()

    def _plan_digest(self, categories: Sequence[str], retention_days: Mapping[str, int], candidates: Sequence[CleanupCandidate]) -> str:
        value = {"categories": list(categories), "retention_days": dict(retention_days), "candidates": [item.digest_dict() for item in candidates]}
        return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()

    def _public_plan(self, plan: CleanupPlan, protected: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "plan_id": plan.plan_id,
            "preview_digest": plan.preview_digest,
            "confirmation_token": plan.confirmation_token,
            "expires_at": datetime.fromtimestamp(plan.expires_at, tz=timezone.utc).isoformat(),
            "risk_level": "destructive_local_data",
            "categories": list(plan.summaries),
            "candidate_count": len(plan.candidates),
            "candidate_bytes": sum(item.bytes for item in plan.candidates),
            "protected": dict(protected),
            "steps": ["重新扫描固定类别", "校验预览未过期", "取得全局维护写锁", "删除完全一致的过期候选", "记录安全审计摘要"],
        }

    def _discard_expired_plans(self) -> None:
        now = self.clock()
        self._plans = {key: value for key, value in self._plans.items() if value.expires_at > now}

    def _audit(self, status: str, plan: CleanupPlan, count: int, size: int) -> None:
        if not callable(self.audit_sink):
            return
        record = {
            "schema_version": "4",
            "record_type": "invocation_finished",
            "event_id": uuid.uuid4().hex,
            "invocation_id": plan.plan_id,
            "source": "web",
            "operation": "workspace_cleanup",
            "action_id": "workspace.cleanup",
            "action": {"action_id": "workspace.cleanup", "categories": list(plan.categories)},
            "status": status,
            "outcome_code": "workspace_cleanup_completed" if status == "succeeded" else "workspace_cleanup_partial_failure",
            "result": {"deleted_count": count, "deleted_bytes": size},
        }
        try:
            self.audit_sink(record)
        except Exception:
            return

    def _notify_deleted(self, candidates: Sequence[CleanupCandidate]) -> None:
        """用逻辑类别和 ID 通知同进程缓存；回调失败不改变已经完成的磁盘事实。"""

        if not callable(self.deleted_sink):
            return
        for candidate in candidates:
            try:
                self.deleted_sink(candidate.category, candidate.object_id)
            except Exception:
                continue

    @staticmethod
    def _frontmatter_value(text: str, key: str) -> str | None:
        match = re.search(rf"(?m)^{re.escape(key)}:\s*[\"']?([^\"'\r\n]+)[\"']?\s*$", text)
        return match.group(1).strip() if match else None

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None


__all__ = ["CATEGORY_DEFAULTS", "WorkspaceCleanupError", "WorkspaceCleanupService"]
