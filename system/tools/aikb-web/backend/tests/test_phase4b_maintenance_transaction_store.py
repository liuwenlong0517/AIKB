"""阶段 4B 事务事实源的边界、原子落盘和并发安全测试。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from aikb_web.core.maintenance_changes import MaintenanceChange, MaintenanceLeafState
from aikb_web.core.maintenance_transaction_store import (
    MaintenanceTransactionStore,
    MaintenanceTransactionStoreError,
)


def _change(status: str = "prepared") -> MaintenanceChange:
    """生成环境目标最小合法事实；测试不接触真实配置。"""
    leaves = (
        MaintenanceLeafState("user_environment.aikb_home", "present", "d" * 64, "e" * 64),
        MaintenanceLeafState("user_environment.aikb_knowledge_home", "present", "d" * 64, "e" * 64),
    )
    change = MaintenanceChange(
        change_id="maintenance-store-test-001",
        target_id="environment",
        action_id="maintenance.environment.update",
        risk_level="user_config_write",
        status="prepared",
        base_fingerprint="a" * 64,
        before_fingerprint="b" * 64,
        after_fingerprint="c" * 64,
        step_summary=("preflight", "backup", "write_environment", "verify"),
        preview_digest="f" * 64,
        created_at="2026-08-31T01:00:00Z",
        expires_at="2026-08-31T01:05:00Z",
        updated_at="2026-08-31T01:00:00Z",
        task_id=None,
        leaf_states=leaves,
    )
    if status == "prepared":
        return change
    return change.transition(status, task_id="task-store-test-001")


class MaintenanceTransactionStoreTests(unittest.TestCase):
    """验证事实源不会静默修复、越界或产生半 JSON。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.store = MaintenanceTransactionStore(self.workspace)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_constructor_is_read_only_and_create_is_explicit(self) -> None:
        """构造不创建运行面，create 才建立目录并可往返读取。"""
        self.assertFalse((self.workspace / "runtime").exists())
        transaction = _change()
        self.store.create(transaction)
        self.assertEqual(self.store.load(transaction.change_id), transaction)
        self.assertEqual(self.store.list_nonterminal(), (transaction,))

    def test_terminal_transactions_are_not_nonterminal(self) -> None:
        """终态事实保留但不进入待恢复扫描。"""
        transaction = _change()
        self.store.create(transaction)
        verified = tuple(replace(leaf, progress="verified") for leaf in transaction.leaf_states)
        succeeded = transaction.transition("applying", task_id="task-store-test-001").transition(
            "verifying", leaf_states=tuple(replace(leaf, progress="applied") for leaf in transaction.leaf_states)
        ).transition("succeeded", leaf_states=verified)
        self.store.save(succeeded)
        self.assertEqual(self.store.list_nonterminal(), ())

    def test_corrupt_unknown_or_oversized_json_is_rejected(self) -> None:
        """损坏、未知字段和超预算材料都必须显式报错。"""
        transaction = _change()
        self.store.create(transaction)
        path = self.workspace / "runtime" / "web" / "maintenance-transactions" / transaction.change_id / "transaction.json"
        payload = transaction.to_dict()
        payload["unknown"] = True
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(MaintenanceTransactionStoreError):
            self.store.load(transaction.change_id)

    def test_private_material_coexists_but_is_never_scanned_as_a_transaction(self) -> None:
        """执行器 private 材料可共存；scan/load 只读取事务根的 transaction.json。"""
        transaction = _change()
        self.store.create(transaction)
        directory = self.workspace / "runtime" / "web" / "maintenance-transactions" / transaction.change_id
        private = directory / "private"
        private.mkdir()
        (private / "transaction.json").write_text("{not-a-transaction}", encoding="utf-8")
        self.assertEqual(self.store.load(transaction.change_id), transaction)
        self.assertEqual(self.store.list_nonterminal(), (transaction,))

    def test_directory_and_payload_change_id_must_match(self) -> None:
        """拒绝把一个事务事实放入另一个 change_id 目录，避免恢复错事务。"""
        transaction = _change()
        self.store.create(transaction)
        path = self.workspace / "runtime" / "web" / "maintenance-transactions" / transaction.change_id / "transaction.json"
        path.write_text(json.dumps({**transaction.to_dict(), "change_id": "other-change"}), encoding="utf-8")
        with self.assertRaises(MaintenanceTransactionStoreError):
            self.store.load(transaction.change_id)
        path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(MaintenanceTransactionStoreError):
            self.store.load(transaction.change_id)
        path.write_bytes(b" " * (16 * 1024 + 1))
        with self.assertRaises(MaintenanceTransactionStoreError):
            self.store.load(transaction.change_id)

    def test_replace_failure_preserves_previous_json(self) -> None:
        """os.replace 失败时旧事实源仍完整，临时文件不会成为新事实。"""
        transaction = _change()
        self.store.create(transaction)
        original = (self.workspace / "runtime" / "web" / "maintenance-transactions" / transaction.change_id / "transaction.json").read_bytes()
        applying = transaction.transition("applying", task_id="task-store-test-001")
        with patch("aikb_web.core.maintenance_transaction_store.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(MaintenanceTransactionStoreError):
                self.store.save(applying)
        path = self.workspace / "runtime" / "web" / "maintenance-transactions" / transaction.change_id / "transaction.json"
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.store.load(transaction.change_id).status, "prepared")

    def test_partial_write_failure_does_not_leave_transaction_directory(self) -> None:
        """首次写入刷新失败时 create 清理本次新建目录，不留下半 JSON。"""
        transaction = _change()
        with patch("aikb_web.core.maintenance_transaction_store.os.fsync", side_effect=OSError("fsync failed")):
            with self.assertRaises(MaintenanceTransactionStoreError):
                self.store.create(transaction)
        self.assertFalse((self.workspace / "runtime" / "web" / "maintenance-transactions" / transaction.change_id).exists())

    def test_symlink_runtime_boundary_is_rejected(self) -> None:
        """运行面任一级为符号链接时拒绝读写；目标链接不应被跟随。"""
        runtime = self.workspace / "runtime"
        target = self.workspace / "outside"
        target.mkdir()
        try:
            runtime.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("当前环境不允许创建符号链接")
        with self.assertRaises(MaintenanceTransactionStoreError):
            self.store.create(_change())

    def test_concurrent_saves_always_leave_parseable_json(self) -> None:
        """并发更新使用 replace，读取者不会观察到半写 JSON。"""
        transaction = _change()
        self.store.create(transaction)
        failures: list[BaseException] = []

        def save(index: int) -> None:
            try:
                self.store.save(transaction.transition("applying", task_id=f"task-{index}"))
            except BaseException as error:  # 测试线程必须把异常带回主线程
                failures.append(error)

        threads = [threading.Thread(target=save, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(self.store.load(transaction.change_id).status, "applying")


if __name__ == "__main__":
    unittest.main()
