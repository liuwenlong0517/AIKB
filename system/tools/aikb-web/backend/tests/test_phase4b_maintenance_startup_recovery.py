"""启动恢复协调器的故障注入与安全门禁测试。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from aikb_web.core.maintenance_materials import (
    MaintenanceEnvironmentMaterial,
    MaintenanceLeafMaterial,
    MaintenanceMaterialManifest,
    MaintenanceMaterialStore,
)
from aikb_web.core.maintenance_transaction_store import MaintenanceScanResult
from aikb_web.core.maintenance_transaction_store import MaintenanceTransactionStore
from aikb_web.core.maintenance_recovery import CurrentLeafObservation
from aikb_web.core.maintenance_recovery_gate import MaintenanceRecoveryGate
from aikb_web.core.maintenance_startup_recovery import (
    MaintenanceStartupRecovery,
    MaintenanceTerminalEvidence,
)
from aikb_web.core.maintenance_lock import MaintenanceWriteLock
from aikb_web.platform.maintenance import MaintenanceStepResult

from tests.test_phase4b_maintenance_execution import _change


class _Store:
    """可注入事务扫描/保存故障的内存事实源。"""

    def __init__(self, transactions):
        self.transactions = {item.change_id: item for item in transactions}
        self.saved = []
        self.fail_save = False
        self.fail_status = None
        self.scan_error = False

    def scan(self):
        if self.scan_error:
            raise OSError("scan unavailable")
        return MaintenanceScanResult(tuple(self.transactions.values()), ())

    def load(self, change_id):
        return self.transactions[change_id]

    def save(self, transaction):
        if self.fail_save or (self.fail_status == transaction.status):
            self.fail_save = False
            self.fail_status = None
            raise OSError("injected save")
        self.transactions[transaction.change_id] = transaction
        self.saved.append(transaction)


class _Materials:
    """返回与事务摘要一致的安全 manifest。"""

    def __init__(self, transaction, fail=False):
        self.transaction = transaction
        self.fail = fail

    def load(self, change_id):
        if self.fail:
            raise ValueError("material unavailable")
        import hashlib
        leaves = tuple(
            MaintenanceLeafMaterial(
                leaf.leaf_id, leaf.existence,
                hashlib.sha256(b"before").hexdigest(), hashlib.sha256(b"expected").hexdigest(),
                0o600, b"before", b"expected",
            ) for leaf in self.transaction.leaf_states
        )
        environments = (
            MaintenanceEnvironmentMaterial("AIKB_HOME", "value", "old-root"),
            MaintenanceEnvironmentMaterial("AIKB_KNOWLEDGE_HOME", "value", "old-knowledge"),
        ) if self.transaction.target_id == "environment" else ()
        return MaintenanceMaterialManifest(self.transaction.change_id, self.transaction.target_id, leaves, environments, "f" * 64)


class _Platform:
    """只读观察与固定回滚步骤桩，不触碰真实路径。"""

    def __init__(self, transaction, *, current="before", fail_step=None):
        self.transaction = transaction
        self.current = current
        self.fail_step = fail_step
        self.calls = []

    def observe_leaf(self, change_id, target_id, leaf_id):
        leaf = next(item for item in self.transaction.leaf_states if item.leaf_id == leaf_id)
        if self.current == "expected":
            return CurrentLeafObservation("present", leaf.expected_hash)
        if self.current == "third_party":
            return CurrentLeafObservation("present", "0" * 64)
        if leaf.existence == "missing":
            return CurrentLeafObservation("missing", None)
        return CurrentLeafObservation("present", leaf.before_hash)

    def recover_step(self, change_id, target_id, step):
        self.calls.append(step.step_id)
        if step.step_id == self.fail_step:
            return MaintenanceStepResult(change_id, target_id, step.step_id, False, "failed")
        self.current = "before"
        return MaintenanceStepResult(change_id, target_id, step.step_id, True, "rolled_back")


class _Audit:
    def __init__(self, evidence="none", finish=True, evidence_error=False):
        self.evidence = evidence
        self.finish_ok = finish
        self.finished = []
        self.evidence_error = evidence_error

    def terminal_evidence(self, change_id):
        if self.evidence_error:
            raise OSError("audit unavailable")
        if isinstance(self.evidence, MaintenanceTerminalEvidence):
            return self.evidence
        if self.evidence.startswith("unique_"):
            transaction = self.transaction
            return MaintenanceTerminalEvidence(
                self.evidence, transaction.change_id, transaction.target_id,
                transaction.preview_digest, transaction.task_id,
            )
        return MaintenanceTerminalEvidence(self.evidence)

    def finish_recovery(self, transaction, outcome):
        self.finished.append(outcome)
        if hasattr(self, "processed"):
            self.processed.append(transaction.change_id)
        return self.finish_ok


def _verifying(target_id="environment"):
    transaction = _change(target_id)
    import hashlib
    before_hash = hashlib.sha256(b"before").hexdigest()
    expected_hash = hashlib.sha256(b"expected").hexdigest()
    transaction = replace(
        transaction,
        leaf_states=tuple(replace(leaf, before_hash=before_hash, expected_hash=expected_hash) for leaf in transaction.leaf_states),
    )
    applied_leaves = tuple(replace(leaf, progress="applied") for leaf in transaction.leaf_states)
    return transaction.transition("applying", task_id="recover-task").transition(
        "verifying", leaf_states=applied_leaves, task_id="recover-task"
    )


class MaintenanceStartupRecoveryTests(unittest.TestCase):
    """验证终态 finalize、三态判定、逆序回滚和重新扫描门禁。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _run(self, transaction, platform, audit, store=None):
        store = store or _Store([transaction])
        audit.transaction = transaction
        gate = MaintenanceRecoveryGate()
        coordinator = MaintenanceStartupRecovery(
            store, _Materials(transaction), platform, audit, gate, MaintenanceWriteLock(str(self.workspace))
        )
        return coordinator, store, gate

    def test_verifying_unique_successful_audit_finalizes_only_when_expected(self):
        transaction = _verifying()
        coordinator, store, gate = self._run(transaction, _Platform(transaction, current="expected"), _Audit("unique_succeeded"))
        self.assertEqual(coordinator.recover_all(), ())
        self.assertEqual(store.transactions[transaction.change_id].status, "succeeded")
        self.assertFalse(gate.blocked)

        transaction = _verifying()
        coordinator, store, gate = self._run(transaction, _Platform(transaction, current="before"), _Audit("unique_succeeded"))
        coordinator.recover_all()
        self.assertEqual(store.transactions[transaction.change_id].status, "recovery_required")
        self.assertTrue(gate.blocked)

    def test_no_evidence_rolls_back_in_reverse_order_and_rescans(self):
        transaction = _verifying("agent.codex")
        platform = _Platform(transaction, current="expected")
        coordinator, store, gate = self._run(transaction, platform, _Audit("none"))
        coordinator.recover_all()
        self.assertEqual(platform.calls, ["write_hooks", "write_mcp", "write_root_instructions"])
        self.assertEqual(store.transactions[transaction.change_id].status, "rolled_back")
        self.assertFalse(gate.blocked)

    def test_environment_mixed_leaf_decisions_reach_recover_step(self):
        """同一环境步骤可安全携带一个恢复和一个 NOOP 决策。"""
        transaction = _verifying()

        class Mixed(_Platform):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.mixed_pending = True

            def observe_leaf(self, change_id, target_id, leaf_id):
                if leaf_id.endswith("aikb_home") and self.mixed_pending:
                    self.mixed_pending = False
                    leaf = self.transaction.leaf_states[0]
                    return CurrentLeafObservation("present", leaf.expected_hash)
                return super().observe_leaf(change_id, target_id, leaf_id)

            def recover_step(self, change_id, target_id, step):
                self.asserted = tuple(item.decision.value for item in step.leaf_decisions)
                return super().recover_step(change_id, target_id, step)

        platform = Mixed(transaction, current="before")
        coordinator, store, gate = self._run(transaction, platform, _Audit("none"))
        coordinator.recover_all()
        self.assertEqual(platform.asserted, ("restore_before", "already_before/noop"))
        self.assertEqual(store.transactions[transaction.change_id].status, "rolled_back")
        self.assertFalse(gate.blocked)

    def test_third_party_or_audit_conflict_blocks_without_rollback(self):
        for audit, platform in ((_Audit("none"), None), (_Audit("conflict"), None), (_Audit("duplicate"), None)):
            transaction = _verifying()
            platform = _Platform(transaction, current="third_party")
            coordinator, store, gate = self._run(transaction, platform, audit)
            coordinator.recover_all()
            self.assertEqual(store.transactions[transaction.change_id].status, "recovery_required")
            self.assertTrue(gate.blocked)
            self.assertEqual(platform.calls, [])

    def test_audit_finish_or_save_failure_keeps_gate_blocked(self):
        transaction = _verifying("agent.codex")
        coordinator, store, gate = self._run(transaction, _Platform(transaction, current="expected"), _Audit("none", finish=False))
        coordinator.recover_all()
        self.assertTrue(gate.blocked)

    def test_terminal_save_failure_is_finalized_by_bound_audit_without_duplicate_finish(self):
        """首次 terminal save 失败保留 rolling_back，下一次仅依赖唯一审计 finalize。"""
        transaction = _verifying()
        store = _Store([transaction])
        store.fail_status = "rolled_back"
        audit = _Audit("none")
        coordinator, _, gate = self._run(transaction, _Platform(transaction, current="expected"), audit, store)
        coordinator.recover_all()
        self.assertEqual(store.transactions[transaction.change_id].status, "rolling_back")
        self.assertEqual(audit.finished, ["rolled_back"])
        audit.evidence = "unique_rolled_back"
        coordinator, _, gate = self._run(store.transactions[transaction.change_id], _Platform(transaction, current="before"), audit, store)
        coordinator.recover_all()
        self.assertEqual(store.transactions[transaction.change_id].status, "rolled_back")
        self.assertEqual(audit.finished, ["rolled_back"])
        self.assertFalse(gate.blocked)

    def test_scan_failure_reblocks_previously_clear_gate(self):
        """已解除门禁后再次扫描失败，必须主动恢复 scan_failed 阻断。"""
        transaction = _verifying()
        store = _Store([transaction])
        store.scan_error = True
        gate = MaintenanceRecoveryGate()
        gate.complete_scan((), ())
        self.assertFalse(gate.blocked)
        coordinator = MaintenanceStartupRecovery(
            store, _Materials(transaction), _Platform(transaction), _Audit("none"), gate,
            MaintenanceWriteLock(str(self.workspace)),
        )
        with self.assertRaises(Exception):
            coordinator.recover_all()
        self.assertTrue(gate.blocked)
        self.assertEqual(gate.to_dict()["reason_code"], "scan_issue")

    def test_evidence_unavailable_preserves_nonterminal_for_retry(self):
        """审计事实源暂不可读不得永久标记 recovery_required。"""
        transaction = _verifying()
        coordinator, store, gate = self._run(transaction, _Platform(transaction), _Audit("none", evidence_error=True))
        coordinator.recover_all()
        self.assertEqual(store.transactions[transaction.change_id].status, "verifying")
        self.assertTrue(gate.blocked)

    def test_invalid_dependency_is_rejected_at_construction(self):
        """恢复依赖缺少公开方法时构造即拒绝，gate 保持默认阻断。"""
        transaction = _verifying()
        gate = MaintenanceRecoveryGate()
        with self.assertRaises(Exception):
            MaintenanceStartupRecovery(
                object(), _Materials(transaction), _Platform(transaction), _Audit("none"), gate,
                MaintenanceWriteLock(str(self.workspace)),
            )
        self.assertTrue(gate.blocked)

    def test_multiple_transactions_use_stable_order_and_recovery_gate(self):
        """多笔事务按 created_at/change_id 顺序处理，恢复态仍保持 gate 阻断。"""
        first = replace(_verifying(), change_id="recover-order-b")
        second = replace(_verifying(), change_id="recover-order-a")
        store = _Store([first, second])

        class Materials:
            def load(self, change_id):
                return _Materials(store.transactions[change_id]).load(change_id)

        class Platform:
            def __init__(self):
                self.platforms = {item.change_id: _Platform(item, current="before") for item in (first, second)}

            def observe_leaf(self, change_id, target_id, leaf_id):
                return self.platforms[change_id].observe_leaf(change_id, target_id, leaf_id)

            def recover_step(self, change_id, target_id, step):
                return self.platforms[change_id].recover_step(change_id, target_id, step)

        audit = _Audit("none")
        audit.processed = []
        gate = MaintenanceRecoveryGate()
        coordinator = MaintenanceStartupRecovery(
            store, Materials(), Platform(), audit, gate, MaintenanceWriteLock(str(self.workspace)),
        )
        coordinator.recover_all()
        self.assertEqual(audit.processed, ["recover-order-a", "recover-order-b"])
        self.assertFalse(gate.blocked)

    def test_real_transaction_and_material_stores_are_recovered_together(self):
        """真实 transaction.json 与 private manifest 联动，事实源不被覆盖。"""
        import hashlib

        transaction = _verifying()
        transaction_store = MaintenanceTransactionStore(self.workspace)
        transaction_store.create(_change("environment"))
        transaction_store.save(transaction)
        runtime = self.workspace / "runtime" / "web" / "maintenance-transactions"
        material_store = MaintenanceMaterialStore(runtime)
        leaves = {
            leaf.leaf_id: MaintenanceLeafMaterial(
                leaf.leaf_id, leaf.existence,
                hashlib.sha256(b"before").hexdigest(), hashlib.sha256(b"expected").hexdigest(),
                0o600, b"before", b"expected",
            ) for leaf in transaction.leaf_states
        }
        material_store.prepare(
            transaction.change_id,
            transaction.target_id,
            leaves,
            {
                "AIKB_HOME": MaintenanceEnvironmentMaterial("AIKB_HOME", "value", "old-root"),
                "AIKB_KNOWLEDGE_HOME": MaintenanceEnvironmentMaterial("AIKB_KNOWLEDGE_HOME", "value", "old-knowledge"),
            },
        )
        audit = _Audit("unique_succeeded")
        audit.transaction = transaction
        gate = MaintenanceRecoveryGate()
        coordinator = MaintenanceStartupRecovery(
            transaction_store, material_store, _Platform(transaction, current="expected"), audit, gate,
            MaintenanceWriteLock(str(self.workspace)),
        )
        coordinator.recover_all()
        self.assertEqual(transaction_store.load(transaction.change_id).status, "succeeded")
        self.assertTrue((runtime / transaction.change_id / "transaction.json").is_file())
        self.assertTrue((runtime / transaction.change_id / "private" / "manifest.json").is_file())
        self.assertFalse(gate.blocked)


if __name__ == "__main__":
    unittest.main()
