"""维护恢复终态审计证据适配器测试。"""
import json
from concurrent.futures import ThreadPoolExecutor
import unittest
from aikb_web.core.maintenance_recovery_evidence import MaintenanceRecoveryEvidenceAdapter, MaintenanceRecoveryEvidenceError
from types import SimpleNamespace


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.transaction = SimpleNamespace(change_id="change-1", target_id="environment", before_fingerprint="a" * 64, after_fingerprint="b" * 64, task_id="task-1")
        class Audit:
            def read_events(inner):
                return {"items": list(self.events), "damaged": list(self.damaged)}
            def write(inner, record):
                if not self.write_succeeds:
                    return {"written": False}
                self.events.append(record)
                return {"written": True}
        self.events = []
        self.damaged = []
        self.write_succeeds = True
        self.audit = Audit()
        self.adapter = MaintenanceRecoveryEvidenceAdapter(SimpleNamespace(load=lambda _: self.transaction), self.audit)
    def test_none_unique_and_duplicate(self):
        self.assertEqual(self.adapter.terminal_evidence("change-1").state, "none")
        self.assertTrue(self.adapter.finish_recovery(self.transaction, "succeeded"))
        self.assertEqual(self.adapter.terminal_evidence("change-1").state, "unique_succeeded")
        self.assertFalse(self.adapter.finish_recovery(self.transaction, "succeeded"))
    def test_binding_conflict_and_bad_line_fail_closed(self):
        self.events.append({"record_type": "invocation_finished", "operation": "maintenance.recover", "change_id": "change-1", "maintenance_target_id": "other", "before_fingerprint": "a"*64, "after_fingerprint": "b"*64, "task_id": "task-1", "status": "succeeded"})
        self.assertEqual(self.adapter.terminal_evidence("change-1").state, "binding_mismatch")
        self.events.append("{bad")
        with self.assertRaises(MaintenanceRecoveryEvidenceError): self.adapter.terminal_evidence("change-1")
    def test_unknown_binding_rejected(self):
        with self.assertRaises(MaintenanceRecoveryEvidenceError): self.adapter.terminal_evidence("../x")

    def test_damaged_or_unconfirmed_write_fails_closed(self):
        self.damaged.append("damaged-event")
        with self.assertRaises(MaintenanceRecoveryEvidenceError):
            self.adapter.terminal_evidence("change-1")
        self.damaged.clear()
        self.write_succeeds = False
        with self.assertRaises(MaintenanceRecoveryEvidenceError):
            self.adapter.finish_recovery(self.transaction, "rolled_back")
        self.assertEqual(self.events, [])

    def test_concurrent_finish_writes_exactly_one_terminal_event(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.adapter.finish_recovery(self.transaction, "rolled_back"), range(8)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 7)
        self.assertEqual(len(self.events), 1)

if __name__ == "__main__": unittest.main()
