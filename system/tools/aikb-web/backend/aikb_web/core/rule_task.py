"""规则变更任务与审计协调层。

本模块把 ``RuleTransactionExecutor`` 接入现有 ``TaskStore``，但不实现原子
文件替换。任务事实源只保存 ``change_id``；确认令牌只存在于后台 worker 的
闭包中。事务执行器返回终态后才写终态审计，任何审计故障都会进入恢复阻断。
"""

from __future__ import annotations

import inspect
import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .tasks import TaskError, TaskStore
from .rule_changes import RULE_USER_UPDATE_SPEC, RuleChangeTransaction


_CHANGE_ID = re.compile(r"^change-[0-9a-f]{32}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-fA-F]{7,64}$")
_TERMINAL = frozenset({"succeeded", "expired", "rejected", "rolled_back", "recovery_required"})
_METADATA_FIELDS = frozenset(
    {
        "change_id", "rule_id", "action_id", "risk_level", "before_hash", "after_hash",
        "diff_hash", "preview_digest", "repository_revision", "rollback_status",
    }
)
_PREPARE_FIELDS = _METADATA_FIELDS | frozenset({"status", "validator_version", "created_at", "expires_at", "updated_at", "task_id", "rollback_status"})


class RuleTaskRejected(ValueError):
    """规则任务未满足准入条件；消息不包含底层异常、正文或物理路径。"""

    def __init__(self, message: str, *, status_code: int = 409, code: str = "rule_apply_rejected") -> None:
        """保存固定 HTTP 映射，供 API 层转换为安全错误响应。"""
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class RuleTransactionExecutorProtocol(Protocol):
    """原子事务执行器的最小注入协议。"""

    def apply(self, change_id: str, confirmation_token: str, preview_digest: str) -> Mapping[str, Any]: ...


def _utc_now() -> str:
    """生成事务恢复标记使用的 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe_metadata(value: Mapping[str, Any], change_id: str) -> dict[str, Any]:
    """严格投影事务摘要，拒绝正文、diff、路径和任意扩展字段。"""
    if not isinstance(value, Mapping) or set(value) - _PREPARE_FIELDS:
        raise RuleTaskRejected("规则变更摘要无效", status_code=503, code="rule_transaction_unavailable")
    if _CHANGE_ID.fullmatch(change_id or "") is None or value.get("change_id") != change_id:
        raise RuleTaskRejected("规则变更标识无效", code="rule_change_invalid")
    if value.get("rule_id") != "user" or value.get("action_id") != "rule.user.update" or value.get("risk_level") != "source_write":
        raise RuleTaskRejected("规则变更动作无效", code="rule_change_invalid")
    for field in ("before_hash", "after_hash", "diff_hash", "preview_digest"):
        if not isinstance(value.get(field), str) or _HASH.fullmatch(value[field]) is None:
            raise RuleTaskRejected("规则变更摘要无效", status_code=503, code="rule_transaction_unavailable")
    if not isinstance(value.get("repository_revision"), str) or _REVISION.fullmatch(value["repository_revision"]) is None:
        raise RuleTaskRejected("规则变更 revision 无效", status_code=503, code="rule_transaction_unavailable")
    if value.get("rollback_status", "not_started") not in {"not_applicable", "not_started", "pending", "succeeded", "recovery_required"}:
        raise RuleTaskRejected("规则回滚状态无效", status_code=503, code="rule_transaction_unavailable")
    return {field: value.get(field) for field in _METADATA_FIELDS}


class RuleChangeTaskCoordinator:
    """创建并执行 ``rule.user.update`` 任务，维护事务、任务和审计关联。"""

    def __init__(
        self,
        transaction_executor: RuleTransactionExecutorProtocol,
        *,
        workspace_root: Path | str | None = None,
        task_store: TaskStore | None = None,
        audit_sink: Callable[[Mapping[str, Any]], Any] | None = None,
        max_workers: int = 2,
    ) -> None:
        """绑定原子事务执行器、任务事实源和审计端；不接受请求提供的路径。"""
        self.executor = transaction_executor
        service = getattr(transaction_executor, "_service", None)
        inferred_root = workspace_root or getattr(service, "_workspace_root", None)
        if task_store is None and inferred_root is None:
            raise RuleTaskRejected("规则任务工作区不可用", status_code=503, code="rule_task_unavailable")
        self.store = task_store or TaskStore(Path(inferred_root), recover=False)
        self.audit_sink = audit_sink
        self._guard = threading.RLock()
        self._blocked = False
        self._pending: dict[str, tuple[str, dict[str, Any]]] = {}
        # 一个 change 在本协调器生命周期内只允许排队一次，避免 worker 尚未
        # 消费令牌时第二个并发请求再次通过非消费 prepare。
        self._submitted: set[str] = set()
        self._jobs: dict[str, tuple[Any, str, str]] = {}
        self._pool = ThreadPoolExecutor(max_workers=max(1, int(max_workers)), thread_name_prefix="aikb-rule-task")
        self._shutdown = False

    def _audit(self, *, record_type: str, status: str, metadata: Mapping[str, Any], task_id: str | None = None) -> None:
        """写入固定审计投影；底层审计异常只转换为 fail-closed 错误。"""
        if not callable(self.audit_sink):
            raise RuleTaskRejected("审计服务不可用", status_code=503, code="audit_unavailable")
        record = {
            "schema_version": 4,
            "record_type": record_type,
            "event_id": uuid.uuid4().hex,
            "invocation_id": metadata["change_id"],
            "source": "web",
            "operation": "rule.user.update",
            "status": status,
            "action_id": "rule.user.update",
            "change_id": metadata["change_id"],
            "resource_type": "rule",
            "resource_id": "user",
            "before_hash": metadata["before_hash"],
            "after_hash": metadata["after_hash"],
            "rollback_status": metadata.get("rollback_status") or "not_started",
            "task_id": task_id,
            "target_task_id": task_id,
        }
        try:
            self.audit_sink(record)
        except Exception as error:
            raise RuleTaskRejected("审计写入失败", status_code=503, code="audit_unavailable") from error

    def _load_metadata(self, change_id: str, token: str) -> dict[str, Any]:
        """调用非消费 prepare；旧执行器缺少该接口时从事务和进程令牌事实源读取。"""
        prepare = getattr(self.executor, "prepare", None)
        if callable(prepare):
            try:
                raw = prepare(change_id, token)
            except Exception as error:
                raise RuleTaskRejected("确认令牌无效或变更不可用", code="rule_confirmation_invalid") from error
            if isinstance(raw, Mapping) and raw.get("status") not in (None, "prepared"):
                raise RuleTaskRejected("规则变更当前不可应用", code="rule_change_invalid")
            return _safe_metadata(raw, change_id)

        store = getattr(self.executor, "_store", None)
        service = getattr(self.executor, "_service", None)
        try:
            transaction = store.load(change_id)
            raw = {field: getattr(transaction, field) for field in _METADATA_FIELDS}
            expected = {
                "rule_id": transaction.rule_id, "change_id": transaction.change_id, "risk_level": transaction.risk_level,
                "repository_revision": transaction.repository_revision, "before_hash": transaction.before_hash,
                "after_hash": transaction.after_hash, "diff_hash": transaction.diff_hash,
                "validator_version": transaction.validator_version, "preview_digest": transaction.preview_digest,
            }
            service._tokens.peek(token, expected)
        except Exception as error:
            raise RuleTaskRejected("确认令牌无效或变更不可用", code="rule_confirmation_invalid") from error
        if transaction.status != "prepared":
            raise RuleTaskRejected("规则变更当前不可应用", code="rule_change_invalid")
        return _safe_metadata(raw, change_id)

    def _claim(self, change_id: str, task_id: str) -> None:
        """在事务锁内把唯一预生成 task_id 认领到变更；兼容 A 的 claim 接口。"""
        claim = getattr(self.executor, "claim", None)
        if callable(claim):
            try:
                outcome = claim(change_id, task_id)
            except Exception as error:
                raise RuleTaskRejected("规则变更已被其他任务认领", code="rule_change_in_progress") from error
            if outcome is False or (isinstance(outcome, Mapping) and outcome.get("claimed") is False):
                raise RuleTaskRejected("规则变更已被其他任务认领", code="rule_change_in_progress")
            return
        acquire = getattr(self.executor, "_acquire_repository", None)
        store = getattr(self.executor, "_store", None)
        if not callable(acquire) or store is None:
            raise RuleTaskRejected("规则事务认领接口不可用", status_code=503, code="rule_task_unavailable")
        try:
            with acquire():
                transaction = store.load(change_id)
                if transaction.status != "prepared":
                    raise RuleTaskRejected("规则变更当前不可应用", code="rule_change_invalid")
                existing_task_id = getattr(transaction, "task_id", None)
                if existing_task_id is not None and existing_task_id != task_id:
                    raise RuleTaskRejected("规则变更已被其他任务认领", code="rule_change_in_progress")
                if existing_task_id != task_id:
                    store.save(replace(transaction, task_id=task_id, updated_at=_utc_now()))
        except RuleTaskRejected:
            raise
        except Exception as error:
            raise RuleTaskRejected("规则变更认领失败", status_code=503, code="rule_task_unavailable") from error

    def _release_claim(self, change_id: str, task_id: str) -> None:
        """取消尚未启动的 worker 时安全释放旧执行器的事务认领。"""
        release = getattr(self.executor, "release_claim", None)
        if callable(release):
            try:
                release(change_id, task_id)
            except Exception:
                pass
            return
        acquire = getattr(self.executor, "_acquire_repository", None)
        store = getattr(self.executor, "_store", None)
        if not callable(acquire) or store is None:
            return
        try:
            with acquire():
                transaction = store.load(change_id)
                if getattr(transaction, "task_id", None) == task_id and transaction.status == "prepared":
                    store.save(replace(transaction, task_id=None, updated_at=_utc_now()))
        except Exception:
            pass

    def _apply_transaction(self, change_id: str, token: str, digest: str, task_id: str) -> Mapping[str, Any]:
        """按 A 的新 task_id 协议调用 apply，旧 executor 保持三参数兼容。"""
        apply = getattr(self.executor, "apply")
        try:
            parameters = inspect.signature(apply).parameters
            accepts_task = len(parameters) >= 4 or "task_id" in parameters
        except (TypeError, ValueError):
            accepts_task = False
        if accepts_task:
            return apply(change_id, token, digest, task_id)
        return apply(change_id, token, digest)

    def _finalize_success(self, change_id: str, task_id: str) -> None:
        """在 succeeded 审计成功后请求执行器清理候选/备份材料。"""
        finalize = getattr(self.executor, "finalize_success", None)
        if not callable(finalize):
            return
        try:
            try:
                parameters = inspect.signature(finalize).parameters
                accepts_task = len(parameters) >= 2 or "task_id" in parameters
            except (TypeError, ValueError):
                accepts_task = True
            outcome = finalize(change_id, task_id) if accepts_task else finalize(change_id)
        except Exception as error:
            raise RuleTaskRejected("规则事务收尾需要恢复", status_code=503, code="rule_recovery_required") from error
        if outcome is False:
            raise RuleTaskRejected("规则事务收尾需要恢复", status_code=503, code="rule_recovery_required")

    def prepare_apply(self, *, change_id: str, confirmation_token: str) -> Mapping[str, Any]:
        """做非消费确认并暂存令牌；返回只含哈希/ID 的安全摘要。"""
        with self._guard:
            if self._blocked or self._shutdown:
                raise RuleTaskRejected("规则写入暂时被阻断", status_code=503, code="rule_apply_blocked")
            if _CHANGE_ID.fullmatch(change_id or "") is None or not isinstance(confirmation_token, str) or not confirmation_token:
                raise RuleTaskRejected("规则变更标识无效", code="rule_change_invalid")
            if change_id in self._pending or change_id in self._submitted:
                raise RuleTaskRejected("规则变更正在提交", code="rule_change_in_progress")
            metadata = self._load_metadata(change_id, confirmation_token)
            self._pending[change_id] = (confirmation_token, metadata)
            return dict(metadata)

    def submit_prepared(self, prepared: Mapping[str, Any]) -> Mapping[str, Any]:
        """在开始审计成功后创建任务；令牌只被 worker 闭包捕获，不进入 TaskStore。"""
        with self._guard:
            change_id = str(prepared.get("change_id") or "") if isinstance(prepared, Mapping) else ""
            pending = self._pending.get(change_id)
            if pending is None:
                raise RuleTaskRejected("规则变更未准备", code="rule_change_invalid")
            metadata = _safe_metadata(prepared, change_id)
            token, expected = pending
            if metadata != expected:
                raise RuleTaskRejected("规则变更摘要已变化", code="rule_change_invalid")
            try:
                # 内部动作不注册到浏览器动作目录；共享契约仍严格限定唯一 change_id 参数。
                RULE_USER_UPDATE_SPEC.validate_parameters({"change_id": change_id})
            except Exception as error:
                raise RuleTaskRejected("规则变更标识无效", code="rule_change_invalid") from error
            task_id = uuid.uuid4().hex
            try:
                # 审计开始先于任务落盘和 worker 启动，也先于令牌消费。
                self._audit(record_type="invocation_started", status="started", metadata=metadata)
                # claim 必须发生在事务执行器锁内，两个进程即使同时写开始审计也只能
                # 有一个认领成功；认领失败时绝不创建第二个 TaskStore 任务。
                self._claim(change_id, task_id)
                task = self.store.create_task(
                    action_id=RULE_USER_UPDATE_SPEC.action_id, parameters={"change_id": change_id},
                    risk_level=RULE_USER_UPDATE_SPEC.risk_level, effects=list(RULE_USER_UPDATE_SPEC.effects),
                    timeout_seconds=120, concurrency_group=RULE_USER_UPDATE_SPEC.action_id,
                    preview_digest=metadata["preview_digest"], invocation_id=change_id, task_id=task_id,
                )
            except RuleTaskRejected:
                self._pending.pop(change_id, None)
                # 认领冲突是正常去重拒绝，不扩大成全局恢复阻断；审计端自身失败
                # 才需要阻断后续写入。
                if task_id and self._transaction_status(change_id) in {"applying", "validating", "rolling_back", "recovery_required"}:
                    self._blocked = True
                try:
                    self._audit(record_type="invocation_finished", status="failed", metadata=metadata, task_id=task_id)
                except RuleTaskRejected:
                    self._blocked = True
                raise
            except Exception as error:
                self._pending.pop(change_id, None)
                self._blocked = True
                try:
                    self._audit(record_type="invocation_finished", status="recovery_required", metadata=metadata)
                except RuleTaskRejected:
                    pass
                raise RuleTaskRejected("规则任务创建失败", status_code=503, code="rule_task_unavailable") from error
            self._pending.pop(change_id, None)
            self._submitted.add(change_id)
            try:
                future = self._pool.submit(self._run, task["task_id"], change_id, token, metadata)
                # token 仍只被 worker 参数闭包持有；jobs 只是取消/等待索引，不持久化。
                self._jobs[task["task_id"]] = (future, change_id, token)
            except Exception as error:
                self._submitted.discard(change_id)
                self._blocked = True
                try:
                    self.store.transition(task["task_id"], "interrupted", reason="rule_worker_unavailable")
                    self._audit(record_type="invocation_finished", status="recovery_required", metadata=metadata, task_id=task["task_id"])
                except Exception:
                    pass
                raise RuleTaskRejected("规则任务不可启动", status_code=503, code="rule_task_unavailable") from error
            public_task = self._public_task(task)
            public_task["change_id"] = change_id
            return public_task

    def apply(self, *, change_id: str, confirmation_token: str) -> dict[str, Any]:
        """串行完成非消费准备、开始审计和入队；不在请求线程消费令牌。"""
        with self._guard:
            metadata = self.prepare_apply(change_id=change_id, confirmation_token=confirmation_token)
            return {"change_id": change_id, "status": "submitted", "task": self.submit_prepared(metadata)}

    def _semantic_output(self, task_id: str, text: str) -> None:
        """只追加固定阶段语义，不把底层异常、正文或路径写入任务输出。"""
        try:
            self.store.append_output(task_id, text + "\n")
        except TaskError:
            pass

    def _mark_recovery_required(self, change_id: str, task_id: str | None = None) -> None:
        """通知执行器恢复；旧执行器没有 hook 时安全地持久化显式恢复态。"""
        marker = getattr(self.executor, "mark_audit_failure", None)
        if callable(marker):
            try:
                try:
                    parameters = inspect.signature(marker).parameters
                    accepts_task = len(parameters) >= 2 or "task_id" in parameters
                except (TypeError, ValueError):
                    accepts_task = False
                if accepts_task and task_id is not None:
                    marker(change_id, task_id)
                else:
                    marker(change_id)
                return
            except Exception:
                pass
        store = getattr(self.executor, "_store", None)
        if store is None:
            return
        try:
            transaction = store.load(change_id)
            if transaction.status not in _TERMINAL:
                updated = transaction.transition("rolling_back", updated_at=_utc_now()) if transaction.status != "rolling_back" else transaction
                updated = updated.transition("recovery_required", updated_at=_utc_now())
            elif transaction.status != "recovery_required":
                # 审计失败后的安全态必须可观察；不伪造 succeeded，显式要求恢复。
                updated = replace(transaction, status="recovery_required", rollback_status="recovery_required", updated_at=_utc_now())
            else:
                updated = transaction
            store.save(updated)
        except Exception:
            pass

    def _transaction_status(self, change_id: str) -> str | None:
        """读取事务当前状态；读取失败返回 None，避免猜测底层写入结果。"""
        store = getattr(self.executor, "_store", None)
        try:
            return str(store.load(change_id).status) if store is not None else None
        except Exception:
            return None

    def _run(self, task_id: str, change_id: str, token: str, metadata: Mapping[str, Any]) -> None:
        """后台消费令牌、执行原子事务并在真实终态后写终态审计。"""
        try:
            self.store.transition(task_id, "running")
            self._semantic_output(task_id, "校验")
            result = self._apply_transaction(change_id, token, str(metadata["preview_digest"]), task_id)
            status = str(result.get("status")) if isinstance(result, Mapping) else ""
            self._semantic_output(task_id, "应用")
            self._semantic_output(task_id, "复核")
            if status == "rolled_back":
                self._semantic_output(task_id, "回滚")
                task_status, outcome, audit_status = "failed", "rolled_back", "rolled_back"
            elif status in {"recovery_required", "uncertain"}:
                self._semantic_output(task_id, "回滚")
                task_status, outcome, audit_status = "failed", "recovery_required", "recovery_required"
                status = "recovery_required"
            elif status == "succeeded":
                task_status, outcome, audit_status = "succeeded", "succeeded", "succeeded"
            else:
                raise RuleTaskRejected("规则事务状态无效", status_code=503, code="rule_transaction_unavailable")
            # 事务 executor 已经返回真实终态；此处才尝试终态审计。
            terminal_metadata = dict(metadata)
            terminal_metadata["rollback_status"] = {
                "succeeded": "not_applicable",
                "rolled_back": "succeeded",
                "recovery_required": "recovery_required",
            }[status]
            try:
                self._audit(record_type="invocation_finished", status=audit_status, metadata=terminal_metadata, task_id=task_id)
            except RuleTaskRejected:
                self._mark_recovery_required(change_id, task_id)
                self._blocked = True
                task_status, outcome = "failed", "recovery_required"
            else:
                if status == "succeeded":
                    try:
                        # 审计已成功落盘后，才允许 executor 清理正式事务材料。
                        self._finalize_success(change_id, task_id)
                    except RuleTaskRejected:
                        self._mark_recovery_required(change_id, task_id)
                        self._blocked = True
                        task_status, outcome = "failed", "recovery_required"
            self.store.finish(task_id, status=task_status, result={"outcome": outcome, "change_id": change_id})
        except Exception as error:
            # 令牌/过期/仓库前置拒绝通常仍是 prepared 或已安全 expired，不能把
            # 普通客户端失败扩大为全局 recovery 阻断；只有写入中间态需要恢复。
            transaction_status = self._transaction_status(change_id)
            uncertain = bool(getattr(error, "uncertain", False)) or "uncertain" in type(error).__name__.lower()
            needs_recovery = uncertain or transaction_status in {"applying", "validating", "rolling_back", "recovery_required"}
            if needs_recovery:
                self._blocked = True
            try:
                latest = self.store.get_task(task_id)
                if latest.get("status") in {"running", "cancelling"}:
                    outcome = "recovery_required" if needs_recovery else "validation_rejected"
                    self.store.finish(task_id, status="failed", result={"outcome": outcome, "change_id": change_id})
                if needs_recovery:
                    self._mark_recovery_required(change_id, task_id)
                    failure_metadata = dict(metadata)
                    failure_metadata["rollback_status"] = "recovery_required"
                    self._audit(record_type="invocation_finished", status="recovery_required", metadata=failure_metadata, task_id=task_id)
                else:
                    safe_status = "expired" if transaction_status == "expired" else "failed"
                    self._audit(record_type="invocation_finished", status=safe_status, metadata=metadata, task_id=task_id)
            except Exception:
                self._blocked = True
        finally:
            with self._guard:
                self._jobs.pop(task_id, None)

    @staticmethod
    def _public_task(task: Mapping[str, Any]) -> dict[str, Any]:
        """返回任务安全投影；显式排除任务正文、令牌和物理路径。"""
        return {key: task.get(key) for key in ("task_id", "status", "action_id", "preview_digest", "invocation_id", "change_id") if key in task}

    def get_change(self, change_id: str) -> dict[str, Any]:
        """返回事务和关联任务的安全状态，不读取或投影候选正文/diff/路径。"""
        if _CHANGE_ID.fullmatch(change_id or "") is None:
            raise RuleTaskRejected("规则变更标识无效", status_code=404, code="not_found")
        store = getattr(self.executor, "_store", None)
        if store is None:
            raise RuleTaskRejected("规则事务不可用", status_code=503, code="rule_transaction_unavailable")
        try:
            transaction = store.load(change_id)
        except Exception as error:
            raise RuleTaskRejected("规则变更不存在", status_code=404, code="not_found") from error
        task = next(
            (
                item for item in self.store.list_tasks()
                if isinstance(item.get("parameters"), Mapping) and item["parameters"].get("change_id") == change_id
            ),
            None,
        )
        public_task = self._public_task(task) if task else None
        if public_task is not None:
            public_task["change_id"] = change_id
        return {
            "change": transaction.public_dict(),
            "task": public_task,
            "blocked": bool(self._blocked),
        }

    def _has_damaged_material(self) -> bool:
        """扫描事务安全 JSON 和必需材料，发现损坏非终态即要求人工恢复。"""
        store = getattr(self.executor, "_store", None)
        root = getattr(store, "_workspace_root", None)
        if root is None:
            return False
        root = Path(root) / "runtime" / "web" / "rule-changes"
        for path in root.glob("????/??/change-*/transaction.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                transaction = RuleChangeTransaction.from_dict(payload)
                if transaction.status in _TERMINAL:
                    continue
                # prepared 必须有 candidate；写入中间态必须保留 backup，便于恢复。
                if transaction.status == "prepared":
                    material = path.parent / "candidate.md"
                else:
                    material = path.parent / "backup.md"
                if material.is_symlink() or not material.is_file():
                    return True
            except Exception:
                return True
        return False

    def _reconcile_terminal_tasks(self) -> None:
        """启动对账事务终态与任务状态，避免崩溃窗口留下未终态任务。"""
        store = getattr(self.executor, "_store", None)
        if store is None:
            return
        try:
            transactions = store.all_transactions()
            tasks = self.store.list_tasks()
        except Exception:
            self._blocked = True
            return
        for transaction in transactions:
            if transaction.status not in {"succeeded", "rolled_back", "recovery_required"}:
                continue
            if transaction.status == "recovery_required":
                self._blocked = True
            related = [
                item for item in tasks
                if isinstance(item.get("parameters"), Mapping)
                and item["parameters"].get("change_id") == transaction.change_id
            ]
            task_id = transaction.task_id or (str(related[0].get("task_id")) if related else None)
            if task_id is None:
                continue
            current = next((item for item in related if item.get("task_id") == task_id), None)
            needs_audit = transaction.status == "recovery_required"
            if current is not None and current.get("status") not in _TASK_TERMINAL:
                needs_audit = True
                try:
                    if current.get("status") == "queued":
                        # 旧进程已不存在，先用合法 running 边界重建终态，避免把
                        # 已成功事务错误降级成 interrupted。
                        self.store.transition(task_id, "running", reason="transaction_terminal_reconcile")
                    self.store.finish(
                        task_id,
                        status="succeeded" if transaction.status == "succeeded" else "failed",
                        result={"outcome": transaction.status, "change_id": transaction.change_id},
                    )
                except Exception:
                    self._blocked = True
            if needs_audit:
                metadata = _safe_metadata(transaction.public_dict(), transaction.change_id)
                try:
                    self._audit(
                        record_type="invocation_finished",
                        status=transaction.status,
                        metadata={**metadata, "rollback_status": transaction.rollback_status},
                        task_id=task_id,
                    )
                except RuleTaskRejected:
                    self._blocked = True

    def public_status(self) -> dict[str, Any]:
        """返回系统状态页使用的最小恢复提示。"""
        with self._guard:
            blocked = self._blocked
        return {"available": True, "blocked": blocked, "recovery_required": blocked, **({"warning": "rule_recovery_required"} if blocked else {})}

    def recover(self) -> list[dict[str, Any]]:
        """启动时恢复事务和历史任务；恢复异常只进入安全阻断。"""
        try:
            recovered = self.executor.recover() if callable(getattr(self.executor, "recover", None)) else []
            if any(isinstance(item, Mapping) and item.get("status") == "recovery_required" for item in (recovered or [])):
                self._blocked = True
            if self._has_damaged_material():
                self._blocked = True
            self._reconcile_terminal_tasks()
            self.store.recover_interrupted()
            return list(recovered or [])
        except Exception:
            self._blocked = True
            return []

    def shutdown(self) -> None:
        """停止后台线程池；不取消正在执行的原子事务。"""
        with self._guard:
            if self._shutdown:
                return
            self._shutdown = True
            jobs = list(self._jobs.items())
        for task_id, (future, change_id, _token) in jobs:
            # Future 尚未开始时取消它并把 queued 任务收敛为 cancelled，令牌不会
            # 被消费；已经开始的 worker 返回 False，交给 shutdown 等待原子事务完成。
            if future.cancel():
                try:
                    self.store.transition(task_id, "cancelled", reason="service_shutdown")
                except Exception:
                    pass
                self._release_claim(change_id, task_id)
        self._pool.shutdown(wait=True, cancel_futures=False)
