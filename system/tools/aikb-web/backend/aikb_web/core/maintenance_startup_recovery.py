"""维护事务启动恢复协调器。

本模块只编排可信事务事实源、私有材料、平台逻辑观察、共享写锁和恢复门禁。
它不接收物理路径、配置正文或环境值，也不自行清除门禁；每次处理前后都重新
扫描并调用 ``complete_scan``，只有扫描事实证明没有遗留问题时门禁才会解除。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any, Mapping, Protocol

from ..platform.maintenance import MaintenanceStepResult
from .maintenance_changes import MaintenanceChange
from .maintenance_lock import MaintenanceLockError, MaintenanceWriteLock
from .maintenance_materials import MaintenanceMaterialManifest
from .maintenance_transaction_store import MaintenanceScanIssue, MaintenanceScanResult
from .maintenance_recovery import (
    CurrentLeafObservation,
    EnvironmentObservation,
    RecoveryDecision,
    RecoveryLeaf,
    RecoveryStep,
    build_recovery_plan,
)
from .maintenance_recovery_gate import MaintenanceRecoveryGate
from .maintenance_targets import MAINTENANCE_TARGET_REGISTRY, validate_logical_id

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MaintenanceStartupRecoveryError(RuntimeError):
    """启动恢复无法安全完成；底层异常不会进入公开消息。"""


class _RecoveryFinalizeFailure(MaintenanceStartupRecoveryError):
    """恢复终态审计或事实源落盘失败，调用方需保留原非终态。"""


class _EvidenceUnavailable(MaintenanceStartupRecoveryError):
    """审计事实源暂时不可读；保持原事务状态并等待重试。"""


@dataclass(frozen=True)
class MaintenanceTerminalEvidence:
    """与事务严格绑定的终态审计证据；自由字符串不能冒充审计事实。"""

    state: str
    change_id: str | None = None
    target_id: str | None = None
    before_fingerprint: str | None = None
    after_fingerprint: str | None = None
    task_id: str | None = None

    def __post_init__(self) -> None:
        """唯一终态必须携带固定绑定字段，其他状态不得携带它们。"""
        states = {"none", "unique_succeeded", "unique_rolled_back", "duplicate", "conflict", "binding_mismatch"}
        if self.state not in states:
            raise MaintenanceStartupRecoveryError("终态审计证据无效")
        values = (self.change_id, self.target_id, self.before_fingerprint, self.after_fingerprint, self.task_id)
        if self.state.startswith("unique_") and not all(isinstance(item, str) and item for item in values):
            raise MaintenanceStartupRecoveryError("终态审计绑定缺失")
        if self.state.startswith("unique_"):
            try:
                validate_logical_id(self.change_id, "change_id")
                validate_logical_id(self.task_id, "task_id")
            except Exception as error:
                raise MaintenanceStartupRecoveryError("终态审计逻辑绑定无效") from error
            if MAINTENANCE_TARGET_REGISTRY.get(self.target_id) is None or not all(_SHA256_RE.fullmatch(item) for item in (self.before_fingerprint, self.after_fingerprint)):
                raise MaintenanceStartupRecoveryError("终态审计目标绑定无效")
        if not self.state.startswith("unique_") and any(item is not None for item in values):
            raise MaintenanceStartupRecoveryError("终态审计非唯一证据不得携带绑定")


class MaintenanceRecoveryTransactionStore(Protocol):
    """启动扫描所需的逻辑事务存储。"""

    def scan(self) -> MaintenanceScanResult: ...

    def load(self, change_id: str) -> MaintenanceChange: ...

    def save(self, transaction: MaintenanceChange) -> None: ...


class MaintenanceRecoveryMaterials(Protocol):
    """只提供已绑定事务的私有材料 manifest。"""

    def load(self, change_id: str) -> MaintenanceMaterialManifest: ...


class MaintenanceRecoveryPlatform(Protocol):
    """只接受逻辑叶子观察和固定补偿步骤的平台接口。"""

    def observe_leaf(self, change_id: str, target_id: str, leaf_id: str) -> CurrentLeafObservation | EnvironmentObservation: ...

    def recover_step(self, change_id: str, target_id: str, step: RecoveryStep) -> MaintenanceStepResult: ...


class MaintenanceRecoveryEvidence(Protocol):
    """终态审计证据查询及恢复终态写入门禁。"""

    def terminal_evidence(self, change_id: str) -> MaintenanceTerminalEvidence: ...

    def finish_recovery(self, transaction: MaintenanceChange, outcome: str) -> bool: ...


class MaintenanceStartupRecovery:
    """在共享写锁内逐笔恢复非终态维护事务，并保持 fail-closed。"""

    def __init__(
        self,
        transactions: MaintenanceRecoveryTransactionStore,
        materials: MaintenanceRecoveryMaterials,
        platform: MaintenanceRecoveryPlatform,
        audit: MaintenanceRecoveryEvidence,
        gate: MaintenanceRecoveryGate,
        lock: MaintenanceWriteLock,
    ) -> None:
        """绑定注入组件；构造阶段不扫描、不读取材料、不改变门禁。"""
        self._transactions = transactions
        self._materials = materials
        self._platform = platform
        self._audit = audit
        self._gate = gate
        self._lock = lock
        if not all(callable(getattr(transactions, name, None)) for name in ("scan", "load", "save")):
            raise MaintenanceStartupRecoveryError("事务事实源接口无效")
        if not callable(getattr(materials, "load", None)):
            raise MaintenanceStartupRecoveryError("维护材料接口无效")
        if not all(callable(getattr(platform, name, None)) for name in ("observe_leaf", "recover_step")):
            raise MaintenanceStartupRecoveryError("恢复平台接口无效")
        if not all(callable(getattr(audit, name, None)) for name in ("terminal_evidence", "finish_recovery")):
            raise MaintenanceStartupRecoveryError("恢复审计接口无效")

    def recover_all(self, *, timeout: float = 0) -> tuple[MaintenanceScanIssue, ...]:
        """持锁扫描并按稳定顺序恢复，最后重新扫描决定 gate 是否解除。"""
        try:
            with self._lock.held(timeout=timeout):
                transactions, issues = self._scan_and_gate()
                for transaction in sorted(transactions, key=lambda item: (item.created_at, item.change_id)):
                    if transaction.status in {"prepared", "succeeded", "rolled_back"}:
                        continue
                    if transaction.status == "recovery_required":
                        continue
                    self._recover_one(transaction)
                _, final_issues = self._scan_and_gate()
                return tuple(final_issues)
        except MaintenanceStartupRecoveryError:
            raise
        except MaintenanceLockError as error:
            raise MaintenanceStartupRecoveryError("维护恢复锁不可用") from error
        except Exception as error:
            # 无法完成重新扫描也必须保持 gate 原有阻断状态。
            raise MaintenanceStartupRecoveryError("维护启动恢复失败") from error

    def _scan_and_gate(self) -> tuple[tuple[MaintenanceChange, ...], tuple[MaintenanceScanIssue, ...]]:
        """严格扫描并提交门禁事实，不调用 mark_recovered 或直接改 blocked。"""
        try:
            result = self._transactions.scan()
            if not isinstance(result, MaintenanceScanResult):
                raise MaintenanceStartupRecoveryError("维护扫描结果类型无效")
            self._gate.complete_scan(result.transactions, result.issues)
            return result.transactions, result.issues
        except MaintenanceStartupRecoveryError:
            try:
                self._gate.complete_scan((), (MaintenanceScanIssue(None, "scan_failed"),))
            except Exception:
                pass
            raise
        except Exception as error:
            try:
                self._gate.complete_scan((), (MaintenanceScanIssue(None, "scan_failed"),))
            except Exception:
                pass
            raise MaintenanceStartupRecoveryError("维护扫描失败") from error

    def _recover_one(self, transaction: MaintenanceChange) -> None:
        """绑定材料、审计证据和当前观察后执行最小安全恢复。"""
        try:
            manifest = self._materials.load(transaction.change_id)
            self._validate_material_binding(transaction, manifest)
            try:
                evidence = self._audit.terminal_evidence(transaction.change_id)
            except Exception as error:
                raise _EvidenceUnavailable("终态审计事实暂不可用") from error
            if not isinstance(evidence, MaintenanceTerminalEvidence):
                raise MaintenanceStartupRecoveryError("终态审计证据类型无效")
            if evidence.state.startswith("unique_") and (
                evidence.change_id != transaction.change_id
                or evidence.target_id != transaction.target_id
                or evidence.before_fingerprint != transaction.before_fingerprint
                or evidence.after_fingerprint != transaction.after_fingerprint
                or evidence.task_id != transaction.task_id
            ):
                self._mark_recovery(transaction)
                return
            observations = self._observe_all(transaction)
            if evidence.state in {"duplicate", "conflict", "binding_mismatch"}:
                self._mark_recovery(transaction)
                return
            if evidence.state == "unique_succeeded":
                if transaction.status == "verifying" and self._all_expected(transaction, observations):
                    self._finalize(transaction, "succeeded", audit_already=True)
                else:
                    self._mark_recovery(transaction)
                return
            if evidence.state == "unique_rolled_back":
                if transaction.status == "rolling_back" and self._all_before(transaction, observations):
                    self._finalize(transaction, "rolled_back", audit_already=True)
                else:
                    self._mark_recovery(transaction)
                return
            if evidence.state != "none":
                self._mark_recovery(transaction)
                return
            leaves = tuple(
                RecoveryLeaf(leaf.leaf_id, leaf.existence, leaf.before_hash, leaf.expected_hash, leaf.progress)
                for leaf in transaction.leaf_states
            )
            plans = build_recovery_plan(transaction.target_id, leaves, observations, transaction.step_summary)
            if any(plan.decision in {RecoveryDecision.MATERIAL_INVALID, RecoveryDecision.THIRD_PARTY_CHANGED} for plan in plans):
                self._mark_recovery(transaction)
                return
            self._rollback(transaction, plans)
        except MaintenanceStartupRecoveryError as error:
            # 终态审计或 terminal save 不确定时保留原非终态，等待下一次
            # 启动按审计证据重试；最终 rescan 会继续保持门禁阻断。
            if isinstance(error, _RecoveryFinalizeFailure):
                return
            if isinstance(error, _EvidenceUnavailable):
                return
            try:
                self._mark_recovery(transaction)
            except Exception as persist_error:
                raise MaintenanceStartupRecoveryError("恢复状态无法持久化") from persist_error
        except Exception as error:
            try:
                self._mark_recovery(transaction)
            except Exception as persist_error:
                raise MaintenanceStartupRecoveryError("恢复状态无法持久化") from persist_error

    def _validate_material_binding(self, transaction: MaintenanceChange, manifest: MaintenanceMaterialManifest) -> None:
        """确认私有 manifest 与事务逻辑摘要完全一致。"""
        if not isinstance(manifest, MaintenanceMaterialManifest):
            raise MaintenanceStartupRecoveryError("材料类型无效")
        if manifest.change_id != transaction.change_id or manifest.target_id != transaction.target_id:
            raise MaintenanceStartupRecoveryError("材料目标不匹配")
        if tuple(item.leaf_id for item in manifest.leaves) != tuple(item.leaf_id for item in transaction.leaf_states):
            raise MaintenanceStartupRecoveryError("材料叶子不完整")
        for material, leaf in zip(manifest.leaves, transaction.leaf_states):
            if (material.existence, material.before_hash, material.expected_hash) != (leaf.existence, leaf.before_hash, leaf.expected_hash):
                raise MaintenanceStartupRecoveryError("材料摘要不匹配")
        if transaction.target_id == "environment":
            expected_names = ("AIKB_HOME", "AIKB_KNOWLEDGE_HOME")
            if tuple(item.name for item in manifest.environments) != expected_names:
                raise MaintenanceStartupRecoveryError("环境材料名称不匹配")
            expected_states = tuple("missing" if leaf.existence == "missing" else "present" for leaf in transaction.leaf_states)
            actual_states = tuple("missing" if item.state == "missing" else "present" for item in manifest.environments)
            if actual_states != expected_states:
                raise MaintenanceStartupRecoveryError("环境材料存在语义不匹配")

    def _observe_all(self, transaction: MaintenanceChange) -> dict[str, CurrentLeafObservation | EnvironmentObservation]:
        """读取所有逻辑叶子；平台异常统一变为恢复阻断。"""
        observations: dict[str, CurrentLeafObservation | EnvironmentObservation] = {}
        for leaf in transaction.leaf_states:
            observations[leaf.leaf_id] = self._platform.observe_leaf(transaction.change_id, transaction.target_id, leaf.leaf_id)
        return observations

    @staticmethod
    def _observation(value: CurrentLeafObservation | EnvironmentObservation) -> tuple[str, str | None]:
        """统一文件和环境观察；环境 empty/value 仍以其摘要参与比较。"""
        if isinstance(value, EnvironmentObservation):
            return ("missing", None) if value.state == "missing" else ("present", value.value_hash)
        if isinstance(value, CurrentLeafObservation):
            return value.existence, value.current_hash
        return "invalid", None

    @staticmethod
    def _all_expected(transaction: MaintenanceChange, observations: Mapping[str, Any]) -> bool:
        """检查全部当前观察等于事务期望状态。"""
        return all(
            MaintenanceStartupRecovery._observation(observations[leaf.leaf_id]) == ("present", leaf.expected_hash)
            for leaf in transaction.leaf_states
        )

    @staticmethod
    def _all_before(transaction: MaintenanceChange, observations: Mapping[str, Any]) -> bool:
        """检查全部当前观察等于事务前状态，保留缺失语义。"""
        for leaf in transaction.leaf_states:
            observation = MaintenanceStartupRecovery._observation(observations[leaf.leaf_id])
            if leaf.existence == "missing":
                if observation != ("missing", None):
                    return False
            elif observation != ("present", leaf.before_hash):
                return False
        return True

    def _rollback(self, transaction: MaintenanceChange, plans: tuple[RecoveryStep, ...]) -> None:
        """按恢复计划逆序调用补偿步骤，每步成功后持久化叶子进度。"""
        try:
            rolling = transaction.transition("rolling_back", updated_at=transaction.updated_at)
            self._transactions.save(rolling)
            for plan in plans:
                if plan.decision == RecoveryDecision.NOOP:
                    # 已处于 before 的 pending 叶子无需改写；已写过的叶子
                    # 仍要留下 rolled_back 事实，满足事务终态校验。
                    leaves = tuple(
                        replace(leaf, progress="rolled_back")
                        if leaf.leaf_id in plan.leaf_ids and leaf.progress != "pending" else leaf
                        for leaf in rolling.leaf_states
                    )
                    rolling = replace(rolling, leaf_states=leaves, updated_at=transaction.updated_at)
                    self._transactions.save(rolling)
                    continue
                if plan.decision not in {RecoveryDecision.RESTORE_BEFORE, RecoveryDecision.REMOVE_CREATED}:
                    raise MaintenanceStartupRecoveryError("恢复计划不安全")
                result = self._platform.recover_step(rolling.change_id, rolling.target_id, plan)
                if (
                    not isinstance(result, MaintenanceStepResult)
                    or not result.succeeded
                    or result.outcome_code != "rolled_back"
                    or result.change_id != rolling.change_id
                    or result.target_id != rolling.target_id
                    or result.step_id != plan.step_id
                ):
                    raise MaintenanceStartupRecoveryError("补偿步骤未证明完成")
                leaves = tuple(replace(leaf, progress="rolled_back") if leaf.leaf_id in plan.leaf_ids and leaf.progress != "pending" else leaf for leaf in rolling.leaf_states)
                rolling = replace(rolling, leaf_states=leaves, updated_at=transaction.updated_at)
                self._transactions.save(rolling)
            if not self._all_before(transaction, self._observe_all(transaction)):
                raise MaintenanceStartupRecoveryError("恢复后状态未证明")
            self._finalize(rolling, "rolled_back")
        except Exception as error:
            if isinstance(error, _RecoveryFinalizeFailure):
                # 终态审计或 terminal save 不确定时，rolling_back 事实已存在；
                # 保持该非终态供下一次启动按审计证据重试，不伪造 recovery。
                raise
            self._mark_recovery(rolling if "rolling" in locals() else transaction)

    def _finalize(self, transaction: MaintenanceChange, outcome: str, *, audit_already: bool = False) -> None:
        """先完成恢复终态审计，再持久化 terminal transaction；失败保持非终态。"""
        try:
            if not audit_already and not self._audit.finish_recovery(transaction, outcome):
                raise MaintenanceStartupRecoveryError("恢复终态审计失败")
            leaves = transaction.leaf_states
            if outcome == "succeeded":
                leaves = tuple(replace(leaf, progress="verified") for leaf in leaves)
            elif outcome == "rolled_back":
                leaves = tuple(
                    replace(leaf, progress="rolled_back") if leaf.progress != "pending" else leaf
                    for leaf in leaves
                )
            terminal = transaction.transition(outcome, updated_at=transaction.updated_at, leaf_states=leaves)
            self._transactions.save(terminal)
        except Exception as error:
            raise _RecoveryFinalizeFailure("恢复终态无法持久化") from error

    def _mark_recovery(self, transaction: MaintenanceChange) -> None:
        """把一个固定叶子标为 recovery_required，并保留全局 gate 阻断。"""
        if transaction.status == "recovery_required":
            return
        leaves = tuple(
            replace(leaf, progress="recovery_required") if index == 0 else leaf
            for index, leaf in enumerate(transaction.leaf_states)
        )
        try:
            base = transaction
            # 状态图要求 applying/verifying 先持久化进入 rolling_back；不能
            # 越过中间态直接伪造 recovery_required。
            if base.status in {"applying", "verifying"}:
                base = base.transition("rolling_back", updated_at=base.updated_at)
                self._transactions.save(base)
            recovery = base.transition("recovery_required", leaf_states=leaves, updated_at=base.updated_at)
            self._transactions.save(recovery)
        except Exception as error:
            raise MaintenanceStartupRecoveryError("恢复状态无法持久化") from error


__all__ = [
    "MaintenanceRecoveryEvidence",
    "MaintenanceTerminalEvidence",
    "MaintenanceRecoveryMaterials",
    "MaintenanceRecoveryPlatform",
    "MaintenanceRecoveryTransactionStore",
    "MaintenanceStartupRecovery",
    "MaintenanceStartupRecoveryError",
]
