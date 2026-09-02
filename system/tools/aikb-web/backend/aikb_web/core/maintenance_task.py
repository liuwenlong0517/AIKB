"""阶段 4B 维护事务的任务协调层。

本模块把已经准备好的维护事务接入 ``TaskStore``。浏览器只提供 change_id
和确认令牌；任务、审计和异常投影只保留逻辑 ID、摘要和固定状态，不携带
令牌、环境值、备份正文、物理路径或底层异常。真实写入仍完全由
``MaintenanceExecutor`` 和平台适配器负责。
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import inspect
from pathlib import Path
from typing import Any, Callable, Mapping

from .actions import ActionError, ConfirmationTokenService
from .maintenance_changes import MaintenanceChange
from .maintenance_execution import MaintenanceExecutionError
from .maintenance_targets import MAINTENANCE_TARGET_REGISTRY, validate_logical_id
from .maintenance_transaction_store import MaintenanceTransactionStore
from .maintenance_recovery_gate import MaintenanceRecoveryGate
from .tasks import TERMINAL_STATES, TaskError, TaskStore


class MaintenanceTaskRejected(ValueError):
    """维护任务未满足准入条件；消息不包含令牌、正文或路径。"""

    def __init__(self, message: str, *, status_code: int = 409, code: str = "maintenance_apply_rejected") -> None:
        """保存 API 所需的固定 HTTP 状态和错误码。"""
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class MaintenanceAuditAdapter:
    """把执行器的开始/终态门禁投影到共享审计 v4。"""

    def __init__(self, sink: Callable[[Mapping[str, Any]], Any] | None) -> None:
        """绑定共享审计写入回调；不在构造阶段触碰审计事实源。"""
        self._sink = sink

    @staticmethod
    def _record(transaction: MaintenanceChange, task_id: str, status: str, *, outcome: str | None = None) -> dict[str, Any]:
        """生成固定维护字段，排除材料、路径和异常文本。"""
        rollback_status = transaction.rollback_status
        if outcome == "succeeded":
            rollback_status = "not_applicable"
        elif outcome == "rolled_back":
            rollback_status = "succeeded"
        elif outcome == "recovery_required":
            rollback_status = "recovery_required"
        return {
            "schema_version": 4,
            "record_type": "invocation_started" if status == "started" else "invocation_finished",
            "event_id": uuid.uuid4().hex,
            "invocation_id": transaction.change_id,
            "source": "web",
            "operation": "maintenance.apply",
            "status": status,
            "outcome_code": outcome,
            "change_id": transaction.change_id,
            "maintenance_target_id": transaction.target_id,
            "action_id": transaction.action_id,
            "before_fingerprint": transaction.before_fingerprint,
            "after_fingerprint": transaction.after_fingerprint,
            "rollback_status": rollback_status,
            "restart_required": transaction.restart_required,
            "task_id": task_id,
            "target_task_id": task_id,
        }

    def start(self, transaction: MaintenanceChange, task_id: str) -> bool:
        """写入开始事实；任何缺失或异常均返回 False 以阻止写入。"""
        if not callable(self._sink):
            return False
        try:
            result = self._sink(self._record(transaction, task_id, "started"))
            return result is True or (isinstance(result, Mapping) and result.get("written") is True)
        except Exception:
            return False

    def finish(self, transaction: MaintenanceChange, outcome: str) -> bool:
        """写入固定终态事实；回滚使用 failed + outcome_code 组合。"""
        if outcome not in {"succeeded", "rolled_back", "recovery_required"} or not callable(self._sink):
            return False
        status = "succeeded" if outcome == "succeeded" else "failed"
        try:
            result = self._sink(self._record(transaction, transaction.task_id or "", status, outcome=outcome))
            return result is True or (isinstance(result, Mapping) and result.get("written") is True)
        except Exception:
            return False


def _utc_now() -> str:
    """生成用于释放认领的 UTC 时间；不写入用户配置。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class MaintenanceTaskCoordinator:
    """创建并异步执行维护任务，维护事务、任务和审计的安全关联。"""

    def __init__(
        self,
        executor: Any,
        *,
        transactions: Any | None = None,
        token_service: ConfirmationTokenService,
        workspace_root: Path | str,
        task_store: TaskStore | None = None,
        audit_sink: Callable[[Mapping[str, Any]], Any] | None = None,
        recovery_gate: MaintenanceRecoveryGate | None = None,
        max_workers: int = 2,
    ) -> None:
        """绑定维护执行器和任务事实源；构造阶段不消费令牌、不创建事务。"""
        if not callable(getattr(executor, "execute", None)):
            raise MaintenanceTaskRejected("维护执行器不可用", status_code=503, code="maintenance_task_unavailable")
        store = transactions or getattr(executor, "_store", None)
        if not callable(getattr(store, "load", None)) or not callable(getattr(store, "save", None)):
            raise MaintenanceTaskRejected("维护事务事实源不可用", status_code=503, code="maintenance_task_unavailable")
        if not isinstance(token_service, ConfirmationTokenService):
            raise MaintenanceTaskRejected("维护令牌服务不可用", status_code=503, code="maintenance_task_unavailable")
        self.executor = executor
        self.transactions = store
        self.tokens = token_service
        self.store = task_store or TaskStore(workspace_root, recover=False)
        self.audit_sink = audit_sink
        self.recovery_gate = recovery_gate
        self._guard = threading.RLock()
        self._pending: dict[str, tuple[str, dict[str, Any]]] = {}
        self._submitted: set[str] = set()
        self._jobs: dict[str, tuple[Any, str, str]] = {}
        self._blocked = False
        self._shutdown = False
        self._pool = ThreadPoolExecutor(max_workers=max(1, int(max_workers)), thread_name_prefix="aikb-maintenance-task")

    @staticmethod
    def _metadata(transaction: MaintenanceChange) -> dict[str, Any]:
        """从严格事务模型提取任务/审计允许的安全摘要。"""
        return {
            "change_id": transaction.change_id,
            "target_id": transaction.target_id,
            "action_id": transaction.action_id,
            "risk_level": transaction.risk_level,
            "before_fingerprint": transaction.before_fingerprint,
            "after_fingerprint": transaction.after_fingerprint,
            "preview_digest": transaction.preview_digest,
            "rollback_status": transaction.rollback_status,
            "restart_required": transaction.restart_required,
        }

    def _load_prepared(self, change_id: str, confirmation_token: str) -> dict[str, Any]:
        """验证 change、prepared 状态和令牌绑定，但不消费令牌。"""
        try:
            validate_logical_id(change_id, "change_id")
        except (TypeError, ValueError) as error:
            raise MaintenanceTaskRejected("维护变更标识无效", code="maintenance_change_invalid") from error
        if not isinstance(confirmation_token, str) or not confirmation_token:
            raise MaintenanceTaskRejected("确认令牌无效", code="maintenance_confirmation_invalid")
        try:
            transaction = self.transactions.load(change_id)
        except Exception as error:
            raise MaintenanceTaskRejected("维护变更不存在", status_code=404, code="not_found") from error
        if not isinstance(transaction, MaintenanceChange):
            raise MaintenanceTaskRejected("维护变更不可用", status_code=503, code="maintenance_transaction_unavailable")
        if transaction.status != "prepared" or transaction.task_id is not None:
            raise MaintenanceTaskRejected("维护变更当前不可应用", code="maintenance_change_in_progress")
        metadata = self._metadata(transaction)
        try:
            self.tokens.validate(
                confirmation_token,
                action_id=transaction.action_id,
                parameters={"change_id": change_id},
                risk_level=transaction.risk_level,
                preview_digest=transaction.preview_digest,
            )
        except (ActionError, AttributeError) as error:
            raise MaintenanceTaskRejected("确认令牌无效或已消费", code="maintenance_confirmation_invalid") from error
        return metadata

    def prepare_apply(self, *, change_id: str, confirmation_token: str) -> Mapping[str, Any]:
        """执行非消费预检并暂存令牌；返回值只包含安全事务摘要。"""
        with self._guard:
            if self._blocked or self._shutdown:
                raise MaintenanceTaskRejected("维护写入暂时被阻断", status_code=503, code="maintenance_apply_blocked")
            if change_id in self._pending or change_id in self._submitted:
                raise MaintenanceTaskRejected("维护变更正在提交", code="maintenance_change_in_progress")
            metadata = self._load_prepared(change_id, confirmation_token)
            self._pending[change_id] = (confirmation_token, metadata)
            return dict(metadata)

    def submit_prepared(self, prepared: Mapping[str, Any]) -> Mapping[str, Any]:
        """创建只保存 change_id 的任务，并把令牌交给后台 worker 闭包。"""
        with self._guard:
            change_id = str(prepared.get("change_id") or "") if isinstance(prepared, Mapping) else ""
            pending = self._pending.get(change_id)
            if pending is None:
                raise MaintenanceTaskRejected("维护变更未准备", code="maintenance_change_invalid")
            token, metadata = pending
            if dict(prepared) != metadata:
                raise MaintenanceTaskRejected("维护变更摘要已变化", code="maintenance_change_invalid")
            target = MAINTENANCE_TARGET_REGISTRY.get(str(metadata["target_id"]))
            task_id = uuid.uuid4().hex
            try:
                task = self.store.create_task(
                    action_id=target.action_id,
                    parameters={"change_id": change_id},
                    risk_level=target.risk_level,
                    effects=list(target.effects),
                    timeout_seconds=120,
                    concurrency_group=target.action_id,
                    preview_digest=str(metadata["preview_digest"]),
                    invocation_id=change_id,
                    task_id=task_id,
                )
                registered = threading.Event()
                future = self._pool.submit(self._run, task_id, change_id, token, metadata, registered)
                self._jobs[task_id] = (future, change_id, token)
                registered.set()
                self._pending.pop(change_id, None)
                self._submitted.add(change_id)
            except MaintenanceTaskRejected:
                raise
            except Exception as error:
                self._pending.pop(change_id, None)
                raise MaintenanceTaskRejected("维护任务创建失败", status_code=503, code="maintenance_task_unavailable") from error
            return self._public_task(task, change_id)

    def apply(self, *, change_id: str, confirmation_token: str) -> dict[str, Any]:
        """串行完成令牌预检和任务入队；实际令牌消费仅在 worker 中发生。"""
        metadata = self.prepare_apply(change_id=change_id, confirmation_token=confirmation_token)
        task = self.submit_prepared(metadata)
        # 同时保留顶层 task_id 以兼容页面快速跳转；task 内仍是完整安全摘要。
        return {
            "change_id": change_id,
            "status": "submitted",
            "task_id": task.get("task_id"),
            "task": task,
        }

    def _semantic_output(self, task_id: str, text: str) -> None:
        """只记录固定阶段名，不把底层异常或正文写入任务。"""
        try:
            self.store.append_output(task_id, text + "\n")
        except TaskError:
            pass

    def _mark_prepared_recovery(self, transaction: MaintenanceChange, task_id: str) -> None:
        """把认领前失败的 prepared 收敛到 recovery_required 并保留现场。

        worker 已经创建了 task，或执行器已经写入开始审计；即使当前事务仍为
        prepared，也不能把它当作普通过期草稿清理。事务摘要先落为恢复态，后续
        启动恢复再依据平台和审计事实决定人工处置。
        """

        leaves = tuple(
            replace(leaf, progress="recovery_required") if index == 0 else leaf
            for index, leaf in enumerate(transaction.leaf_states)
        )
        recovery = transaction.transition(
            "recovery_required",
            task_id=task_id,
            updated_at=_utc_now(),
            leaf_states=leaves,
        )
        self.transactions.save(recovery)

    def _run(
        self,
        task_id: str,
        change_id: str,
        token: str,
        metadata: Mapping[str, Any],
        registered: threading.Event,
    ) -> None:
        """后台消费令牌、执行事务并把安全结果写入任务事实源。"""
        needs_recovery = False
        try:
            registered.wait()
            self.store.transition(task_id, "running")
            self._semantic_output(task_id, "校验")
            transaction = self.transactions.load(change_id)

            def consume_confirmation(_transaction: MaintenanceChange) -> None:
                """仅在执行器持锁完成材料和 preflight 校验后消费令牌。"""
                self.tokens.consume(
                    token,
                    action_id=str(metadata["action_id"]),
                    parameters={"change_id": change_id},
                    risk_level=str(metadata["risk_level"]),
                    preview_digest=str(metadata["preview_digest"]),
                )

            self._semantic_output(task_id, "应用")
            execute = self.executor.execute
            try:
                signature = inspect.signature(execute)
                supports_callback = "before_claim" in signature.parameters or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            except (TypeError, ValueError):
                supports_callback = True
            if supports_callback:
                result = execute(change_id, task_id, before_claim=consume_confirmation)
            else:
                # 仅兼容旧测试桩；生产 MaintenanceExecutor 必须支持 before_claim。
                consume_confirmation(transaction)
                result = execute(change_id, task_id)
            status = result.status if isinstance(result, MaintenanceChange) else ""
            if status == "succeeded":
                self._semantic_output(task_id, "复核")
                task_status, outcome = "succeeded", "succeeded"
            elif status in {"rolled_back", "recovery_required"}:
                self._semantic_output(task_id, "回滚")
                task_status, outcome = "failed", status
                needs_recovery = status == "recovery_required"
            else:
                raise MaintenanceTaskRejected("维护事务状态无效", status_code=503, code="maintenance_transaction_unavailable")
            self.store.finish(task_id, status=task_status, result={"outcome": outcome, "change_id": change_id})
        except Exception:
            try:
                current = self.transactions.load(change_id)
                if current.status == "prepared":
                    # 任务已存在但事务尚未 claim；这是异常现场，不得遗留 prepared。
                    self._mark_prepared_recovery(current, task_id)
                    needs_recovery = True
                else:
                    needs_recovery = needs_recovery or current.status in {"applying", "verifying", "rolling_back", "recovery_required"}
            except Exception:
                needs_recovery = True
            if needs_recovery:
                self._blocked = True
                if self.recovery_gate is not None:
                    self.recovery_gate.block()
            try:
                latest = self.store.get_task(task_id)
                if latest.get("status") in {"running", "cancelling"}:
                    self.store.finish(task_id, status="failed", result={"outcome": "recovery_required" if needs_recovery else "validation_rejected", "change_id": change_id})
            except Exception:
                self._blocked = True
        finally:
            with self._guard:
                self._jobs.pop(task_id, None)

    @staticmethod
    def _public_task(task: Mapping[str, Any], change_id: str) -> dict[str, Any]:
        """固定过滤任务字段，永不返回确认令牌。"""
        return {
            "task_id": task.get("task_id"), "status": task.get("status"),
            "action_id": task.get("action_id"), "preview_digest": task.get("preview_digest"),
            "invocation_id": task.get("invocation_id"), "change_id": change_id,
        }

    def get_change(self, change_id: str) -> dict[str, Any]:
        """读取事务、任务和恢复门禁安全状态，不读取私有材料。"""
        try:
            validate_logical_id(change_id, "change_id")
            transaction = self.transactions.load(change_id)
        except Exception as error:
            raise MaintenanceTaskRejected("维护变更不存在", status_code=404, code="not_found") from error
        related = next((item for item in self.store.list_tasks() if isinstance(item.get("parameters"), Mapping) and item["parameters"].get("change_id") == change_id), None)
        task = self._public_task(related, change_id) if related else None
        blocked = bool(self._blocked or (self.recovery_gate.blocked if self.recovery_gate is not None else False))
        return {
            "change": transaction.public_dict(),
            "task": task,
            "blocked": blocked,
            "recovery_required": blocked,
            "warning": "maintenance_recovery_required" if blocked else None,
            "recovery": self.recovery_gate.to_dict() if self.recovery_gate is not None else {"blocked": bool(self._blocked)},
        }

    def recover(self) -> None:
        """启动时对账遗留任务；不猜测写入结果，非终态统一标记 interrupted。"""
        try:
            for task in list(self.store.list_tasks()):
                task_id = str(task.get("task_id") or "")
                if task.get("status") in TERMINAL_STATES or not task_id:
                    continue
                change_id = task.get("parameters", {}).get("change_id") if isinstance(task.get("parameters"), Mapping) else None
                transaction = self.transactions.load(str(change_id)) if isinstance(change_id, str) else None
                if isinstance(transaction, MaintenanceChange) and transaction.status in {"succeeded", "rolled_back", "recovery_required"}:
                    self.store.transition(task_id, "running", reason="maintenance_terminal_reconcile") if task.get("status") == "queued" else None
                    self.store.finish(task_id, status="succeeded" if transaction.status == "succeeded" else "failed", result={"outcome": transaction.status, "change_id": transaction.change_id})
                    if transaction.status == "recovery_required":
                        self._blocked = True
                else:
                    self.store.transition(task_id, "interrupted", reason="service_restarted")
        except Exception:
            # 对账失败时保留维护写入阻断，由下一次启动扫描继续处理。
            self._blocked = True

    def shutdown(self) -> None:
        """停止 worker；不写入用户配置，不清理恢复材料。"""
        with self._guard:
            if self._shutdown:
                return
            self._shutdown = True
        self._pool.shutdown(wait=True, cancel_futures=True)


__all__ = ["MaintenanceAuditAdapter", "MaintenanceTaskCoordinator", "MaintenanceTaskRejected"]
