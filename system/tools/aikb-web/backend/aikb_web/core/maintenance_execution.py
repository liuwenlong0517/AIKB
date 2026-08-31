"""阶段 4B 维护事务的通用补偿执行核心。

本模块只编排已经创建的 ``prepared`` 事务、全局维护锁和固定平台适配器；路径、
配置正文、备份材料及环境值均由适配器/材料存储私有持有。执行顺序和叶子映射
完全来自静态目标契约，底层异常统一收敛为安全错误，避免把路径或正文带入任务
和事务事实源。真实审计后端未接入，本批仅定义注入式审计门禁协议。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Mapping, Protocol

from ..platform.maintenance import (
    MaintenancePlatformAdapter,
    MaintenanceStep,
    MaintenanceStepResult,
    MaintenanceVerification,
)
from .maintenance_changes import MaintenanceChange, MaintenanceChangeError
from .maintenance_lock import MaintenanceClaimCoordinator, MaintenanceLockError, MaintenanceWriteLock
from .maintenance_materials import MaintenanceMaterialManifest, MaintenanceMaterialStore
from .maintenance_recovery_gate import MaintenanceRecoveryGate, MaintenanceRecoveryGateError
from .maintenance_targets import MAINTENANCE_WRITE_LEAVES_BY_TARGET, validate_logical_id


class MaintenanceExecutionError(RuntimeError):
    """维护执行被拒绝、步骤失败或无法安全补偿。"""


class _AuditGateFailure(MaintenanceExecutionError):
    """终态审计失败；必须保留当前非终态以阻止新的维护写入。"""


class MaintenanceExecutionStore(Protocol):
    """执行器所需的最小事务事实源协议。"""

    def load(self, change_id: str) -> MaintenanceChange:
        """读取严格事务模型。"""

    def save(self, transaction: MaintenanceChange) -> None:
        """原子持久化事务状态。"""


class MaintenanceExecutionAudit(Protocol):
    """本批注入式审计门禁；实现只返回成功与否，不携带底层异常。"""

    def start(self, transaction: MaintenanceChange, task_id: str) -> bool:
        """首次写步骤前记录开始事实。"""

    def finish(self, transaction: MaintenanceChange, outcome: str) -> bool:
        """终态落盘前确认终态事实已成功记录。"""


_WRITE_STEPS = frozenset({"write_environment", "write_root_instructions", "write_mcp", "write_hooks"})


def _utc_now() -> str:
    """生成单调事务时间；时间本身不包含路径或执行细节。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class MaintenanceExecutor:
    """在全局锁内执行固定步骤，并按已完成写步骤实施逆序补偿。"""

    def __init__(
        self,
        store: MaintenanceExecutionStore,
        adapter: MaintenancePlatformAdapter,
        workspace_root: str,
        material_store: MaintenanceMaterialStore,
        audit: MaintenanceExecutionAudit,
        recovery_gate: MaintenanceRecoveryGate,
        *,
        lock: MaintenanceWriteLock | None = None,
        now_provider: Callable[[], str] | None = None,
    ) -> None:
        """绑定事务事实源、固定适配器和锁；不读取事务或创建材料。"""
        # 事实源使用最小 duck-typed 协议，便于注入持久化 Store 或测试桩，且不
        # 让执行器耦合某一个存储实现。
        if not callable(getattr(store, "load", None)) or not callable(getattr(store, "save", None)):
            raise MaintenanceExecutionError("事务事实源接口无效")
        if not all(callable(getattr(adapter, name, None)) for name in ("apply_step", "verify", "rollback_step")):
            raise MaintenanceExecutionError("维护适配器接口无效")
        if not callable(getattr(material_store, "load", None)):
            raise MaintenanceExecutionError("维护材料接口无效")
        if not all(callable(getattr(audit, name, None)) for name in ("start", "finish")):
            raise MaintenanceExecutionError("审计门禁接口无效")
        if not callable(getattr(recovery_gate, "assert_allowed", None)):
            raise MaintenanceExecutionError("恢复门禁接口无效")
        self._store = store
        self._adapter = adapter
        self._lock = lock or MaintenanceWriteLock(workspace_root)
        self._material_store = material_store
        self._audit = audit
        self._recovery_gate = recovery_gate
        self._claim_coordinator = MaintenanceClaimCoordinator(store, self._lock)
        self._now_provider = now_provider or _utc_now

    def execute(self, change_id: str, task_id: str, *, timeout: float = 0) -> MaintenanceChange:
        """认领并执行 prepared 事务；失败时补偿并返回 rolled_back 或恢复态。"""
        try:
            with self._lock.held(timeout=timeout):
                try:
                    self._recovery_gate.assert_allowed()
                except MaintenanceRecoveryGateError as error:
                    raise MaintenanceExecutionError("维护恢复门禁仍阻断") from error
                current = self._store.load(change_id)
                self._validate_material_binding(current)
                self._validate_before_claim(current, task_id)
                preflight = self._call_step(current, "preflight")
                if not preflight.succeeded:
                    raise MaintenanceExecutionError("维护预检未成功")
                try:
                    if not self._audit.start(current, task_id):
                        raise MaintenanceExecutionError("维护开始审计未成功")
                except MaintenanceExecutionError:
                    raise
                except Exception as error:
                    raise MaintenanceExecutionError("维护开始审计未成功") from error
                current = self._claim_coordinator.claim_held(change_id, task_id)
                attempted_writes: list[str] = []
                try:
                    for step_id in current.step_summary:
                        if step_id == "preflight":
                            continue
                        if step_id == "verify":
                            current = self._begin_verifying(current)
                            current = self._verify(current)
                            return current
                        if step_id in _WRITE_STEPS:
                            attempted_writes.append(step_id)
                        current = self._run_step(current, step_id)
                    raise MaintenanceExecutionError("事务缺少 verify 步骤")
                except _AuditGateFailure:
                    # 验证成功但终态审计失败时，当前事实仍为 verifying；禁止回滚
                    # 已验证内容，也不产生任何终态声明。
                    raise
                except Exception as error:
                    return self._compensate(current, attempted_writes, error)
        except MaintenanceExecutionError:
            raise
        except MaintenanceLockError as error:
            raise MaintenanceExecutionError("维护写锁不可用") from error
        except Exception as error:
            raise MaintenanceExecutionError("维护事务执行失败") from error

    def _validate_before_claim(self, current: MaintenanceChange, task_id: str) -> None:
        """认领前校验 prepared、安全 task ID 和 UTC 过期时间，不调用审计。"""
        if current.status != "prepared" or current.task_id is not None:
            raise MaintenanceExecutionError("维护事务不可执行")
        try:
            validate_logical_id(task_id, "task_id")
            expires = datetime.fromisoformat(current.expires_at[:-1] + "+00:00")
            now = datetime.fromisoformat(self._now_provider()[:-1] + "+00:00")
            if now >= expires:
                raise MaintenanceExecutionError("维护事务已过期")
        except MaintenanceExecutionError:
            raise
        except Exception as error:
            raise MaintenanceExecutionError("维护事务认领参数无效") from error

    def _validate_material_binding(self, transaction: MaintenanceChange) -> None:
        """在认领前完整读取 manifest，精确绑定目标、存在语义和前后摘要。"""
        try:
            manifest = self._material_store.load(transaction.change_id)
            if not isinstance(manifest, MaintenanceMaterialManifest) or manifest.target_id != transaction.target_id:
                raise MaintenanceExecutionError("维护材料目标不匹配")
            if tuple(item.leaf_id for item in manifest.leaves) != tuple(leaf.leaf_id for leaf in transaction.leaf_states):
                raise MaintenanceExecutionError("维护材料叶子不完整")
            for item, leaf in zip(manifest.leaves, transaction.leaf_states):
                if item.existence != leaf.existence or item.before_hash != leaf.before_hash or item.expected_hash != leaf.expected_hash:
                    raise MaintenanceExecutionError("维护材料摘要不匹配")
            if transaction.target_id == "environment":
                expected_states = tuple("missing" if leaf.existence == "missing" else "present" for leaf in transaction.leaf_states)
                actual_states = tuple(item.state if item.state == "missing" else "present" for item in manifest.environments)
                if actual_states != expected_states:
                    raise MaintenanceExecutionError("维护环境材料状态不匹配")
        except MaintenanceExecutionError:
            raise
        except Exception as error:
            raise MaintenanceExecutionError("维护材料不可用") from error

    def _run_step(self, current: MaintenanceChange, step_id: str) -> MaintenanceChange:
        """执行 preflight、backup 或固定写步骤，并即时持久化叶子进度。"""
        result = self._call_step(current, step_id)
        if not result.succeeded:
            raise MaintenanceExecutionError("维护步骤未成功")
        if step_id in _WRITE_STEPS:
            leaf_ids = MAINTENANCE_WRITE_LEAVES_BY_TARGET[current.target_id][step_id]
            leaves = tuple(
                replace(leaf, progress="applied") if leaf.leaf_id in leaf_ids else leaf
                for leaf in current.leaf_states
            )
            updated = replace(current, leaf_states=leaves, updated_at=_utc_now())
            self._store.save(updated)
            return updated
        return current

    def _call_step(self, current: MaintenanceChange, step_id: str) -> MaintenanceStepResult:
        """调用适配器并验证返回结果只属于当前事务和目标。"""
        try:
            result = self._adapter.apply_step(
                current.change_id,
                current.target_id,
                MaintenanceStep(step_id),
            )
            if (
                not isinstance(result, MaintenanceStepResult)
                or result.change_id != current.change_id
                or result.target_id != current.target_id
                or result.step_id != step_id
            ):
                raise MaintenanceExecutionError("维护步骤返回结果无效")
            return result
        except MaintenanceExecutionError:
            raise
        except Exception as error:
            raise MaintenanceExecutionError("维护步骤执行失败") from error

    def _begin_verifying(self, current: MaintenanceChange) -> MaintenanceChange:
        """所有写步骤完成后进入验证态；不伪造 verified 叶子进度。"""
        try:
            verifying = current.transition("verifying", updated_at=_utc_now())
            self._store.save(verifying)
            return verifying
        except (MaintenanceChangeError, ValueError) as error:
            raise MaintenanceExecutionError("维护事务无法进入验证态") from error

    def _verify(self, current: MaintenanceChange) -> MaintenanceChange:
        """仅接受当前目标且 after_fingerprint 一致的 ready/restart_required 结果。"""
        try:
            verification = self._adapter.verify(current.change_id, current.target_id)
            if not isinstance(verification, MaintenanceVerification):
                raise MaintenanceExecutionError("维护验证结果无效")
            if (
                verification.change_id != current.change_id
                or verification.target_id != current.target_id
                or verification.status not in {"ready", "restart_required"}
                or verification.after_fingerprint != current.after_fingerprint
            ):
                raise MaintenanceExecutionError("维护验证未证明期望状态")
            verified_leaves = tuple(replace(leaf, progress="verified") for leaf in current.leaf_states)
            verified = replace(
                current,
                leaf_states=verified_leaves,
                restart_required=verification.status == "restart_required",
                updated_at=_utc_now(),
            )
            self._finish_audit(verified, "succeeded")
            succeeded = verified.transition("succeeded", updated_at=_utc_now(), leaf_states=verified_leaves)
            try:
                self._store.save(succeeded)
            except Exception as error:
                raise _AuditGateFailure("维护成功终态保存不确定") from error
            return succeeded
        except MaintenanceExecutionError:
            raise
        except Exception as error:
            raise MaintenanceExecutionError("维护验证失败") from error

    def _finish_audit(self, transaction: MaintenanceChange, outcome: str) -> None:
        """终态审计门禁；失败统一视为不确定，不触发补偿。"""
        try:
            if not self._audit.finish(transaction, outcome):
                raise _AuditGateFailure("维护终态审计未成功")
        except _AuditGateFailure:
            raise
        except Exception as error:
            raise _AuditGateFailure("维护终态审计未成功") from error

    def _compensate(
        self,
        current: MaintenanceChange,
        attempted_writes: list[str],
        original_error: Exception,
    ) -> MaintenanceChange:
        """进入 rolling_back，按逆序补偿；任一不确定结果都进入 recovery_required。"""
        del original_error  # 底层异常不得进入事务事实源或公开错误文本。
        try:
            if current.status == "verifying":
                rolling = current.transition("rolling_back", updated_at=_utc_now())
            elif current.status == "applying":
                rolling = current.transition("rolling_back", updated_at=_utc_now())
            else:
                raise MaintenanceExecutionError("维护事务无法进入回滚态")
            self._store.save(rolling)
            failed_rollback_step: str | None = None
            for step_id in reversed(attempted_writes):
                failed_rollback_step = step_id
                result = self._adapter.rollback_step(
                    rolling.change_id,
                    rolling.target_id,
                    MaintenanceStep(step_id),
                )
                if (
                    not isinstance(result, MaintenanceStepResult)
                    or not result.succeeded
                    or result.change_id != rolling.change_id
                    or result.target_id != rolling.target_id
                    or result.step_id != step_id
                ):
                    raise MaintenanceExecutionError("维护回滚未能证明完成")
                leaf_ids = MAINTENANCE_WRITE_LEAVES_BY_TARGET[rolling.target_id][step_id]
                leaves = tuple(
                    replace(leaf, progress="rolled_back") if leaf.leaf_id in leaf_ids else leaf
                    for leaf in rolling.leaf_states
                )
                rolling = replace(rolling, leaf_states=leaves, updated_at=_utc_now())
                self._store.save(rolling)
            self._finish_audit(rolling, "rolled_back")
            rolled_back = rolling.transition("rolled_back", updated_at=_utc_now())
            try:
                self._store.save(rolled_back)
            except Exception as error:
                raise _AuditGateFailure("维护回滚终态保存不确定") from error
            return rolled_back
        except Exception as error:
            if isinstance(error, _AuditGateFailure):
                # 终态事实未记录；rolling_back 已持久化，不能伪造 rolled_back。
                raise
            try:
                base = locals().get("rolling", current)
                failed_step = locals().get("failed_rollback_step")
                failed_leaf_ids = set(MAINTENANCE_WRITE_LEAVES_BY_TARGET[base.target_id].get(failed_step, ()))
                recovery_leaves = tuple(
                    replace(leaf, progress="recovery_required") if leaf.leaf_id in failed_leaf_ids else leaf
                    for leaf in base.leaf_states
                )
                if base.status != "rolling_back":
                    base = current.transition("rolling_back", updated_at=_utc_now())
                recovery = base.transition(
                    "recovery_required",
                    updated_at=_utc_now(),
                    leaf_states=recovery_leaves,
                )
                self._store.save(recovery)
                return recovery
            except Exception as recovery_error:
                raise MaintenanceExecutionError("维护回滚状态无法持久化") from recovery_error


__all__ = ["MaintenanceExecutionError", "MaintenanceExecutor"]
