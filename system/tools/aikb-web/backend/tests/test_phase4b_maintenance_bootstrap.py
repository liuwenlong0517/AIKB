"""生产默认维护恢复组合根的惰性装配测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aikb_web.core.maintenance_bootstrap import build_default_maintenance_recovery
from aikb_web.core.maintenance_lock import MaintenanceWriteLock
from aikb_web.core.maintenance_recovery_gate import MaintenanceRecoveryGate
from aikb_web.platform.windows.maintenance_readonly import WindowsMaintenanceAdapter


class _Audit:
    """真实 AuditStore 形状的无事件桩。"""

    def read_events(self):
        return {"items": [], "damaged": []}

    def write(self, record):
        return {"written": True}


class MaintenanceBootstrapTests(unittest.TestCase):
    """验证无事务目录零副作用、依赖损坏 fail-closed 与非 Windows 降级。"""

    def test_empty_runtime_uses_memory_scan_without_creating_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            settings = SimpleNamespace(workspace_root=workspace)
            readonly = object.__new__(WindowsMaintenanceAdapter)
            gate = MaintenanceRecoveryGate()
            lock = MaintenanceWriteLock(workspace)
            gateway = SimpleNamespace(_audit=lambda: _Audit())
            recovery = build_default_maintenance_recovery(settings, readonly, gateway, gate, lock)
            self.assertIsNotNone(recovery)
            self.assertFalse((workspace / "runtime" / "web" / "maintenance-transactions").exists())
            recovery.recover_all()
            self.assertFalse(gate.blocked)
            # 启动恢复必须取得共享写锁，因此允许创建固定锁文件；空扫描不能
            # 创建事务或私有材料目录。
            self.assertFalse((workspace / "runtime" / "web" / "maintenance-transactions").exists())

    def test_existing_runtime_without_audit_dependency_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "runtime" / "web" / "maintenance-transactions"
            root.mkdir(parents=True)
            settings = SimpleNamespace(workspace_root=workspace)
            readonly = object.__new__(WindowsMaintenanceAdapter)
            gate = MaintenanceRecoveryGate()
            recovery = build_default_maintenance_recovery(settings, readonly, object(), gate, MaintenanceWriteLock(workspace))
            self.assertIsNotNone(recovery)
            with self.assertRaises(RuntimeError):
                recovery.recover_all()
            self.assertTrue(gate.blocked)

    def test_non_windows_or_untrusted_readonly_does_not_claim_support(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            gate = MaintenanceRecoveryGate()
            result = build_default_maintenance_recovery(
                SimpleNamespace(workspace_root=workspace), object(), object(), gate, MaintenanceWriteLock(workspace)
            )
            self.assertIsNone(result)
            self.assertTrue(gate.blocked)


if __name__ == "__main__":
    unittest.main()
