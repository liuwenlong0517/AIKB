"""维护启动恢复的生产默认组合根。

本模块只装配已经验证的 Windows 只读适配器、事务/材料事实源、审计证据适配器、
共享 gate 与共享写锁。材料仅在扫描发现事务后惰性绑定，因此没有事务运行面时
仍执行真实空扫描且不创建目录；依赖不完整时返回 fail-closed 占位协调器。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .maintenance_lock import MaintenanceWriteLock
from .maintenance_materials import MaintenanceMaterialStore
from .maintenance_recovery_evidence import MaintenanceRecoveryEvidenceAdapter
from .maintenance_recovery_gate import MaintenanceRecoveryGate
from .maintenance_startup_recovery import MaintenanceStartupRecovery
from .maintenance_transaction_store import MaintenanceTransactionStore
from ..platform.windows.maintenance_recovery import WindowsMaintenanceRecoveryPlatform
from ..platform.windows.maintenance_readonly import WindowsMaintenanceAdapter


class _UnavailableStartupRecovery:
    """生产依赖缺失时的 fail-closed 占位。"""

    def __init__(self, gate: MaintenanceRecoveryGate) -> None:
        self._gate = gate

    def recover_all(self) -> tuple[object, ...]:
        """固定失败，交由 lifespan 调用 gate.block 保持写入阻断。"""
        raise RuntimeError("维护启动恢复依赖不可用")


class _LazyMaintenanceMaterials:
    """在真实事务被扫描到后才校验固定材料根，空启动保持零副作用。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _store(self) -> MaintenanceMaterialStore:
        """每次读取重新验证普通目录与重解析点边界，拒绝启动后替换。"""
        return MaintenanceMaterialStore(self._root)

    def load(self, change_id: str) -> Any:
        return self._store().load(change_id)

    def read_leaf(self, change_id: str, leaf_id: str) -> Any:
        return self._store().read_leaf(change_id, leaf_id)

    def read_environment(self, change_id: str, name: str) -> Any:
        return self._store().read_environment(change_id, name)


def build_default_maintenance_recovery(
    settings: Any,
    readonly: Any,
    gateway: Any,
    gate: MaintenanceRecoveryGate,
    lock: MaintenanceWriteLock,
) -> Any | None:
    """按可信依赖装配默认恢复器；非 Windows/不完整依赖返回 None。"""
    if not isinstance(gate, MaintenanceRecoveryGate) or not isinstance(lock, MaintenanceWriteLock):
        return _UnavailableStartupRecovery(gate)
    if not isinstance(readonly, WindowsMaintenanceAdapter):
        return None
    workspace = getattr(settings, "workspace_root", None)
    if not isinstance(workspace, Path) or not workspace.is_absolute():
        return _UnavailableStartupRecovery(gate)
    transaction_root = workspace / "runtime" / "web" / "maintenance-transactions"
    try:
        transactions = MaintenanceTransactionStore(workspace)
        materials = _LazyMaintenanceMaterials(transaction_root)
        audit_store = gateway._audit() if callable(getattr(gateway, "_audit", None)) else None
        if audit_store is None:
            return _UnavailableStartupRecovery(gate)
        evidence = MaintenanceRecoveryEvidenceAdapter(transactions, audit_store)
        platform = WindowsMaintenanceRecoveryPlatform(readonly, materials)
        return MaintenanceStartupRecovery(transactions, materials, platform, evidence, gate, lock)
    except Exception:
        return _UnavailableStartupRecovery(gate)


__all__ = ["build_default_maintenance_recovery"]
