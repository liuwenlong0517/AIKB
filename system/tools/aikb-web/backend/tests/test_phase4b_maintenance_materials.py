"""阶段 4B 波次 2 批次 1 私有维护材料存储安全测试。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from aikb_web.core.maintenance_changes import MaintenanceChange, MaintenanceLeafState
from aikb_web.core.maintenance_materials import (
    MaintenanceEnvironmentMaterial,
    MaintenanceLeafMaterial,
    MaintenanceMaterialError,
    MaintenanceMaterialStore,
)
from aikb_web.core.maintenance_transaction_store import MaintenanceTransactionStore


def _transaction(change_id: str) -> MaintenanceChange:
    """构造仅含安全元数据的已准备事务，供材料集成测试使用。"""

    leaves = (
        MaintenanceLeafState("user_environment.aikb_home", "present", "d" * 64, "e" * 64),
        MaintenanceLeafState("user_environment.aikb_knowledge_home", "present", "d" * 64, "e" * 64),
    )
    return MaintenanceChange(
        change_id=change_id,
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


def _store_for(workspace: Path, change_id: str, *, permission_hardener=None) -> MaintenanceMaterialStore:
    """先通过事务事实源创建目录，再绑定材料存储运行根。"""

    MaintenanceTransactionStore(workspace).create(_transaction(change_id))
    return MaintenanceMaterialStore(
        workspace / "runtime" / "web" / "maintenance-transactions",
        permission_hardener=permission_hardener,
    )


def _leaf(leaf_id: str, *, missing: bool = False) -> MaintenanceLeafMaterial:
    """构造含可追踪私有正文的隔离叶子材料。"""

    expected = ("expected-" + leaf_id).encode("utf-8")
    before = None if missing else ("private-before-" + leaf_id).encode("utf-8")
    return MaintenanceLeafMaterial(
        leaf_id=leaf_id,
        existence="missing" if missing else "present",
        before_hash=None if before is None else hashlib.sha256(before).hexdigest(),
        expected_hash=hashlib.sha256(expected).hexdigest(),
        file_mode=0o600,
        before_bytes=before,
        expected_bytes=expected,
    )


class MaintenanceMaterialStoreTests(unittest.TestCase):
    """验证材料只进入隔离目录，且公开/manifest 永不带正文或环境值。"""

    def test_prepare_load_and_read_roundtrip_keeps_private_material_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls: list[tuple[Path, bool]] = []
            store = _store_for(
                root,
                "change-opaque-001",
                permission_hardener=lambda path, is_dir: calls.append((path, is_dir)),
            )
            leaves = {
                "user_environment.aikb_home": _leaf("user_environment.aikb_home"),
                "user_environment.aikb_knowledge_home": _leaf("user_environment.aikb_knowledge_home", missing=True),
            }
            environments = {
                "AIKB_HOME": MaintenanceEnvironmentMaterial("AIKB_HOME", "value", "SECRET-CONTROL-ROOT"),
                "AIKB_KNOWLEDGE_HOME": MaintenanceEnvironmentMaterial("AIKB_KNOWLEDGE_HOME", "empty", ""),
            }
            manifest = store.prepare("change-opaque-001", "environment", leaves, environments)
            loaded = store.load("change-opaque-001")
            self.assertEqual(loaded.public_dict(), manifest.public_dict())
            private_leaf = store.read_leaf("change-opaque-001", "user_environment.aikb_home")
            self.assertEqual(private_leaf.before_bytes, b"private-before-user_environment.aikb_home")
            self.assertEqual(store.read_environment("change-opaque-001", "AIKB_HOME").value, "SECRET-CONTROL-ROOT")
            public = json.dumps(manifest.public_dict(), ensure_ascii=False)
            self.assertNotIn("private-before", public)
            self.assertNotIn("SECRET-CONTROL-ROOT", public)
            manifest_json = (
                root / "runtime" / "web" / "maintenance-transactions" / "change-opaque-001" / "private" / "manifest.json"
            ).read_text(encoding="utf-8")
            self.assertNotIn("private-before", manifest_json)
            self.assertNotIn("SECRET-CONTROL-ROOT", manifest_json)
            self.assertTrue(calls)
            transaction_dir = root / "runtime" / "web" / "maintenance-transactions" / "change-opaque-001"
            self.assertTrue((transaction_dir / "transaction.json").is_file())
            self.assertTrue((transaction_dir / "private" / "manifest.json").is_file())

    def test_missing_empty_and_concrete_environment_states_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = _store_for(root, "change-env-states")
            leaves = {
                "user_environment.aikb_home": _leaf("user_environment.aikb_home", missing=True),
                "user_environment.aikb_knowledge_home": _leaf("user_environment.aikb_knowledge_home", missing=True),
            }
            environments = {
                "AIKB_HOME": MaintenanceEnvironmentMaterial("AIKB_HOME", "missing"),
                "AIKB_KNOWLEDGE_HOME": MaintenanceEnvironmentMaterial("AIKB_KNOWLEDGE_HOME", "empty", ""),
            }
            store.prepare("change-env-states", "environment", leaves, environments)
            loaded = store.load("change-env-states")
            self.assertEqual([item.state for item in loaded.environments], ["missing", "empty"])
            self.assertIsNone(store.read_environment("change-env-states", "AIKB_HOME").value)
            self.assertEqual(store.read_environment("change-env-states", "AIKB_KNOWLEDGE_HOME").value, "")
            private = root / "runtime" / "web" / "maintenance-transactions" / "change-env-states" / "private"
            self.assertFalse((private / "environment-00.old").exists())
            self.assertTrue((private / "environment-01.old").exists())

    def test_requires_existing_transaction_and_private_is_single_use(self) -> None:
        """材料层不能创建/覆盖事务事实源，也不能复用既有私有目录。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            runtime = workspace / "runtime" / "web" / "maintenance-transactions"
            runtime.mkdir(parents=True)
            store = MaintenanceMaterialStore(runtime)
            leaves = {
                "user_environment.aikb_home": _leaf("user_environment.aikb_home", missing=True),
                "user_environment.aikb_knowledge_home": _leaf("user_environment.aikb_knowledge_home", missing=True),
            }
            environments = {
                "AIKB_HOME": MaintenanceEnvironmentMaterial("AIKB_HOME", "missing"),
                "AIKB_KNOWLEDGE_HOME": MaintenanceEnvironmentMaterial("AIKB_KNOWLEDGE_HOME", "missing"),
            }
            with self.assertRaises(MaintenanceMaterialError):
                store.prepare("without-transaction", "environment", leaves, environments)
            transaction_dir = runtime / "change-single-use"
            transaction_dir.mkdir()
            (transaction_dir / "transaction.json").write_text("{}", encoding="utf-8")
            (transaction_dir / "private").mkdir()
            with self.assertRaises(MaintenanceMaterialError):
                store.prepare("change-single-use", "environment", leaves, environments)
            self.assertEqual((transaction_dir / "transaction.json").read_text(encoding="utf-8"), "{}")

    def test_material_boundaries_hashes_budget_and_path_ids_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = _store_for(root, "change-boundaries")
            leaves = {
                "user_environment.aikb_home": _leaf("user_environment.aikb_home"),
                "user_environment.aikb_knowledge_home": _leaf("user_environment.aikb_knowledge_home"),
            }
            environments = {
                "AIKB_HOME": MaintenanceEnvironmentMaterial("AIKB_HOME", "missing"),
                "AIKB_KNOWLEDGE_HOME": MaintenanceEnvironmentMaterial("AIKB_KNOWLEDGE_HOME", "missing"),
            }
            with self.assertRaises(MaintenanceMaterialError):
                store.prepare("../escape", "environment", leaves, environments)
            with self.assertRaises(MaintenanceMaterialError):
                store.prepare("change-boundaries", "environment", {"C:\\secret": leaves[next(iter(leaves))]}, environments)
            with self.assertRaises(MaintenanceMaterialError):
                MaintenanceLeafMaterial(
                    "user_environment.aikb_home", "present", "a" * 64, "b" * 64, 0o600, b"actual", b"expected"
                )
            with self.assertRaises(MaintenanceMaterialError):
                MaintenanceEnvironmentMaterial("AIKB_HOME", "missing", "")
            huge = b"x" * (8 * 1024 * 1024 + 1)
            with self.assertRaises(MaintenanceMaterialError):
                MaintenanceLeafMaterial(
                    "user_environment.aikb_home", "missing", None, hashlib.sha256(huge).hexdigest(), 0o600, None, huge
                )

    def test_manifest_tampering_or_material_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = _store_for(root, "change-integrity")
            leaves = {
                "user_environment.aikb_home": _leaf("user_environment.aikb_home"),
                "user_environment.aikb_knowledge_home": _leaf("user_environment.aikb_knowledge_home"),
            }
            environments = {
                "AIKB_HOME": MaintenanceEnvironmentMaterial("AIKB_HOME", "value", "old-root"),
                "AIKB_KNOWLEDGE_HOME": MaintenanceEnvironmentMaterial("AIKB_KNOWLEDGE_HOME", "value", "old-knowledge"),
            }
            store.prepare("change-integrity", "environment", leaves, environments)
            manifest_path = root / "runtime" / "web" / "maintenance-transactions" / "change-integrity" / "private" / "manifest.json"
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["target_id"] = "agent.codex"
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(MaintenanceMaterialError):
                store.load("change-integrity")

    def test_cleanup_removes_only_private_material_and_keeps_transaction_summary(self) -> None:
        """安全终态清理私有正文时保留 transaction.json 事实摘要。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            change_id = "change-cleanup"
            store = _store_for(root, change_id)
            leaves = {
                "user_environment.aikb_home": _leaf("user_environment.aikb_home"),
                "user_environment.aikb_knowledge_home": _leaf("user_environment.aikb_knowledge_home", missing=True),
            }
            environments = {
                "AIKB_HOME": MaintenanceEnvironmentMaterial("AIKB_HOME", "value", "old-root"),
                "AIKB_KNOWLEDGE_HOME": MaintenanceEnvironmentMaterial("AIKB_KNOWLEDGE_HOME", "missing"),
            }
            store.prepare(change_id, "environment", leaves, environments)
            store.cleanup(change_id)
            transaction_dir = root / "runtime" / "web" / "maintenance-transactions" / change_id
            self.assertTrue((transaction_dir / "transaction.json").is_file())
            self.assertFalse((transaction_dir / "private").exists())
            store.cleanup(change_id)

    def test_cleanup_validates_all_private_entries_before_deleting_any_file(self) -> None:
        """未知材料使清理整体拒绝，已经声明的恢复字节也必须原样保留。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            change_id = "change-cleanup-unknown"
            store = _store_for(root, change_id)
            leaves = {
                "user_environment.aikb_home": _leaf("user_environment.aikb_home"),
                "user_environment.aikb_knowledge_home": _leaf("user_environment.aikb_knowledge_home", missing=True),
            }
            environments = {
                "AIKB_HOME": MaintenanceEnvironmentMaterial("AIKB_HOME", "value", "old-root"),
                "AIKB_KNOWLEDGE_HOME": MaintenanceEnvironmentMaterial("AIKB_KNOWLEDGE_HOME", "missing"),
            }
            store.prepare(change_id, "environment", leaves, environments)
            private = root / "runtime" / "web" / "maintenance-transactions" / change_id / "private"
            expected = {path.name: path.read_bytes() for path in private.iterdir()}
            (private / "unknown.bin").write_bytes(b"unknown")

            with self.assertRaises(MaintenanceMaterialError):
                store.cleanup(change_id)

            self.assertEqual(
                {name: (private / name).read_bytes() for name in expected},
                expected,
            )

    def test_reparse_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            link = root / "link"
            try:
                link.symlink_to(root, target_is_directory=True)
            except OSError as error:
                if getattr(error, "winerror", None) == 1314:
                    self.skipTest("当前 Windows 测试账户未启用创建符号链接权限")
                raise
            with self.assertRaises(MaintenanceMaterialError):
                MaintenanceMaterialStore(link)


if __name__ == "__main__":
    unittest.main()
