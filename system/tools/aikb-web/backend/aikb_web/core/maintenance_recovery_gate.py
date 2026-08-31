"""阶段 4B 启动恢复阻断门禁。

门禁只保存扫描所得逻辑 ID 和固定原因码，不执行恢复、不调用适配器，也不暴露
路径或异常。服务启动默认阻断；只有协调器逐事务证明收敛并完成无问题扫描后才
允许新的维护写入。
"""

from __future__ import annotations

import threading
from typing import Iterable, Protocol

from .maintenance_changes import MaintenanceChange
from .maintenance_transaction_store import MaintenanceScanIssue


class MaintenanceRecoveryGateError(RuntimeError):
    """恢复门禁状态或调用顺序无效。"""


class MaintenanceRecoveryCoordinator(Protocol):
    """预留的单事务恢复证明回调；本模块不自行调用 adapter.recover。"""

    def __call__(self, change_id: str) -> str:
        """返回固定终态证明码。"""


class MaintenanceRecoveryGate:
    """线程安全的恢复阻断状态机和安全投影。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._blocked = True
        self._reason_code = "scan_pending"
        self._change_ids: set[str] = set()
        self._issues: tuple[MaintenanceScanIssue, ...] = ()
        self._scan_complete = False
        self._rescan_required = False

    def complete_scan(self, transactions: Iterable[MaintenanceChange], issues: Iterable[MaintenanceScanIssue]) -> None:
        """提交一次全量扫描；终态以外事务和任何 issue 都继续阻断。"""
        with self._lock:
            self._blocked, self._reason_code = True, "scan_invalid"
            transaction_list = tuple(transactions)
            issue_list = tuple(issues)
            if not all(isinstance(item, MaintenanceChange) for item in transaction_list):
                raise MaintenanceRecoveryGateError("扫描事务结果无效")
            if not all(isinstance(item, MaintenanceScanIssue) for item in issue_list):
                raise MaintenanceRecoveryGateError("扫描问题结果无效")
            self._change_ids = {item.change_id for item in transaction_list if item.status in {"applying", "verifying", "rolling_back", "recovery_required"}}
            self._issues = issue_list
            self._scan_complete = True
            self._rescan_required = False
            if issue_list:
                self._blocked, self._reason_code = True, "scan_issue"
            elif self._change_ids:
                self._blocked, self._reason_code = True, "recovery_pending"
            else:
                self._blocked, self._reason_code = False, "none"

    def reconcile(self, coordinator: MaintenanceRecoveryCoordinator) -> None:
        """消费外部协调器的固定证明；异常、冲突或恢复态继续阻断。"""
        with self._lock:
            pending = tuple(self._change_ids)
        for change_id in pending:
            try:
                outcome = coordinator(change_id)
            except Exception as error:
                raise MaintenanceRecoveryGateError("恢复协调失败") from error
            if outcome in {"finalized_succeeded", "finalized_rolled_back", "rolled_back"}:
                with self._lock:
                    self._change_ids.discard(change_id)
                    self._rescan_required = True
                    self._blocked, self._reason_code = True, "rescan_required"
            elif outcome == "recovery_required":
                continue
            else:
                raise MaintenanceRecoveryGateError("恢复证明结果无效")

    def assert_allowed(self) -> None:
        """在写锁内再次确认门禁已解除；阻断时 fail-closed。"""
        with self._lock:
            if self._blocked:
                raise MaintenanceRecoveryGateError("维护恢复门禁仍阻断")

    def block(self) -> None:
        """以固定原因码保持启动恢复阻断，不接受自由文本。"""
        with self._lock:
            self._blocked = True
            self._reason_code = "recovery_failed"
            self._scan_complete = False
            self._rescan_required = True

    @property
    def blocked(self) -> bool:
        """返回当前阻断状态。"""
        with self._lock:
            return self._blocked

    def to_dict(self) -> dict[str, object]:
        """生成只含固定状态、原因码和安全 change_id 的投影。"""
        with self._lock:
            return {"blocked": self._blocked, "reason_code": self._reason_code, "change_ids": sorted(self._change_ids)}


__all__ = ["MaintenanceRecoveryCoordinator", "MaintenanceRecoveryGate", "MaintenanceRecoveryGateError"]
