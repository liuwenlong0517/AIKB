"""阶段 4B 全量扫描与恢复阻断门禁测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aikb_web.core.maintenance_recovery_gate import MaintenanceRecoveryGate, MaintenanceRecoveryGateError
from aikb_web.core.maintenance_transaction_store import MaintenanceTransactionStore
from aikb_web.core.maintenance_transaction_store import MaintenanceScanIssue
from tests.test_phase4b_maintenance_transaction_store import _change


class MaintenanceRecoveryGateTests(unittest.TestCase):
    """验证初始阻断、问题投影和协调器证明后的解除。"""

    def test_gate_starts_blocked_and_clears_only_after_clean_scan(self) -> None:
        gate = MaintenanceRecoveryGate()
        with self.assertRaises(MaintenanceRecoveryGateError):
            gate.assert_allowed()
        gate.complete_scan((), ())
        gate.assert_allowed()
        self.assertEqual(gate.to_dict(), {"blocked": False, "reason_code": "none", "change_ids": []})

    def test_nonterminal_or_issue_keeps_gate_blocked(self) -> None:
        gate = MaintenanceRecoveryGate()
        gate.complete_scan((_change().transition("applying", task_id="task-recover"),), ())
        self.assertTrue(gate.blocked)
        self.assertEqual(gate.to_dict()["reason_code"], "recovery_pending")
        gate.complete_scan((), (MaintenanceScanIssue(None, "scan_failed"),))
        self.assertTrue(gate.blocked)

    def test_projection_contains_no_path_or_exception(self) -> None:
        gate = MaintenanceRecoveryGate()
        gate.complete_scan((_change().transition("applying", task_id="task-recover"),), ())
        projected = json.dumps(gate.to_dict())
        self.assertNotIn("path", projected)
        self.assertNotIn("exception", projected)

    def test_scan_reports_bad_entry_and_keeps_valid_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = MaintenanceTransactionStore(Path(temp))
            transaction = _change()
            store.create(transaction)
            root = Path(temp) / "runtime" / "web" / "maintenance-transactions"
            (root / "bad@").write_text("not-a-directory", encoding="utf-8")
            result = store.scan()
            self.assertEqual(tuple(item.change_id for item in result.transactions), (transaction.change_id,))
            self.assertTrue(any(item.reason_code == "invalid_change_id" for item in result.issues))

    def test_scan_root_boundary_failure_becomes_scan_issue(self) -> None:
        """运行根解析或存在性读取异常只产生 scan_failed，不向上泄露底层错误。"""
        with tempfile.TemporaryDirectory() as temp:
            store = MaintenanceTransactionStore(Path(temp))
            with patch.object(store, "_runtime_root", side_effect=OSError("boundary")):
                result = store.scan()
            self.assertEqual(result.transactions, ())
            self.assertEqual(result.issues[0].reason_code, "scan_failed")


if __name__ == "__main__":
    unittest.main()
