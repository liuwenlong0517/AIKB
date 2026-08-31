"""维护启动恢复组合根接线的安全契约测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from aikb_web.core.maintenance_lock import MaintenanceWriteLock
from aikb_web.core.maintenance_recovery_gate import MaintenanceRecoveryGate
from aikb_web.main import create_app


class _Recovery:
    """可注入启动恢复桩，只模拟 recover_all 协议。"""

    def __init__(self, gate: MaintenanceRecoveryGate, fail: bool = False) -> None:
        self.gate = gate
        self.fail = fail
        self.calls = 0
        self._gate = gate

    def recover_all(self) -> tuple[object, ...]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("底层扫描细节不应外泄")
        self.gate.complete_scan((), ())
        return ()


class MaintenanceLifespanTests(unittest.TestCase):
    """验证启动恢复只影响写门禁，不影响只读健康接口。"""

    def test_startup_calls_injected_recovery_once_and_shares_gate_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gate = MaintenanceRecoveryGate()
            recovery = _Recovery(gate)
            lock = MaintenanceWriteLock(Path(directory))
            app = create_app(
                gateway=object(),
                maintenance_startup_recovery=recovery,
                maintenance_recovery_gate=gate,
                maintenance_write_lock=lock,
            )
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/v1/health").status_code, 200)
                self.assertFalse(app.state.maintenance_recovery_gate.blocked)
            self.assertEqual(recovery.calls, 1)
            self.assertIs(app.state.maintenance_recovery_gate, gate)
            self.assertIs(app.state.maintenance_write_lock, lock)

    def test_recovery_failure_keeps_gate_blocked_and_read_only_alive(self) -> None:
        gate = MaintenanceRecoveryGate()
        recovery = _Recovery(gate, fail=True)
        app = create_app(gateway=object(), maintenance_startup_recovery=recovery, maintenance_recovery_gate=gate)
        with TestClient(app) as client:
            response = client.get("/api/v1/health")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(app.state.maintenance_recovery_gate.blocked)
            self.assertTrue(app.state.maintenance_recovery_error)
            self.assertNotIn("底层", response.text)

    def test_unknown_platform_without_recovery_keeps_default_gate_blocked(self) -> None:
        app = create_app(gateway=object())
        self.assertTrue(app.state.maintenance_recovery_gate.blocked)
        self.assertFalse(app.state.maintenance_recovery_started)

    def test_arbitrary_gate_reason_is_not_accepted(self) -> None:
        gate = MaintenanceRecoveryGate()
        with self.assertRaises(TypeError):
            gate.block("arbitrary")  # type: ignore[call-arg]
        self.assertEqual(gate.to_dict()["reason_code"], "scan_pending")

    def test_split_brain_gate_or_lock_dependencies_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gate = MaintenanceRecoveryGate()
            recovery = _Recovery(gate)
            with self.assertRaises(ValueError):
                create_app(
                    gateway=object(),
                    maintenance_startup_recovery=recovery,
                    maintenance_recovery_gate=MaintenanceRecoveryGate(),
                )
            recovery._lock = MaintenanceWriteLock(Path(directory) / "internal")
            with self.assertRaises(ValueError):
                create_app(
                    gateway=object(),
                    maintenance_startup_recovery=recovery,
                    maintenance_write_lock=MaintenanceWriteLock(Path(directory)),
                )


if __name__ == "__main__":
    unittest.main()
