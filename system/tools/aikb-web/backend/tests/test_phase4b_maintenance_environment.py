"""Windows environment MaintenanceExecutor 适配器的隔离测试。"""

from __future__ import annotations

import hashlib
import sys
import unittest
from dataclasses import replace
from unittest.mock import patch

from aikb_web.core.maintenance_materials import MaintenanceEnvironmentMaterial, MaintenanceLeafMaterial, MaintenanceMaterialManifest, MaintenanceMaterialStore
from aikb_web.core.maintenance_recovery import RecoveryDecision
from aikb_web.platform.maintenance import MaintenanceStep
from aikb_web.platform.windows import maintenance_environment as module
from tests.test_phase4b_maintenance_execution import _change


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class _Materials(MaintenanceMaterialStore):
    """真实材料 dataclass 的内存存储桩，不接触文件。"""

    def __init__(self, transaction, *, missing=False):
        self.transaction = transaction
        self.leaves = (
            MaintenanceLeafMaterial("user_environment.aikb_home", "missing" if missing else "present", None if missing else _hash(b"old"), _hash(b"new"), 0o600, None if missing else b"old", b"new"),
            MaintenanceLeafMaterial("user_environment.aikb_knowledge_home", "present", _hash(b""), _hash(b"new-k"), 0o600, b"", b"new-k"),
        )
        self.manifest = MaintenanceMaterialManifest(transaction.change_id, "environment", self.leaves, (
            MaintenanceEnvironmentMaterial("AIKB_HOME", "missing" if missing else "value", None if missing else "old"),
            MaintenanceEnvironmentMaterial("AIKB_KNOWLEDGE_HOME", "empty", ""),
        ), "f" * 64)

    def load(self, change_id):
        return self.manifest

    def read_leaf(self, change_id, leaf_id):
        return next(item for item in self.leaves if item.leaf_id == leaf_id)

    def read_environment(self, change_id, name):
        return next(item for item in self.manifest.environments if item.name == name)


class _Store:
    def __init__(self, transaction):
        self.transaction = transaction

    def load(self, change_id):
        return self.transaction


class WindowsEnvironmentAdapterTests(unittest.TestCase):
    """验证环境成组写入、补偿和安全拒绝。"""

    def setUp(self):
        self.transaction = _change("environment")
        self.transaction = replace(self.transaction, leaf_states=(
            replace(self.transaction.leaf_states[0], before_hash=_hash(b"old"), expected_hash=_hash(b"new")),
            replace(self.transaction.leaf_states[1], before_hash=_hash(b""), expected_hash=_hash(b"new-k")),
        ))
        self.transaction = replace(
            self.transaction,
            after_fingerprint=module.WindowsEnvironmentMaintenanceAdapter._fingerprint(
                {"AIKB_HOME": "new", "AIKB_KNOWLEDGE_HOME": "new-k"}
            ),
        )
        self.values = {"AIKB_HOME": "old", "AIKB_KNOWLEDGE_HOME": ""}
        self.writes = []
        self.fail_name = None
        self.broadcast_ok = True
        readonly = object()
        self.materials = _Materials(self.transaction)

        def writer(name, value):
            self.writes.append((name, value))
            if name == self.fail_name:
                raise OSError("write failure")
            self.values[name] = value

        with patch.object(module.os, "name", "nt"):
            self.adapter = module.WindowsEnvironmentMaintenanceAdapter(
                _Store(self.transaction), self.materials,
                environment_reader=lambda: self.values,
                environment_writer=writer,
                environment_broadcaster=lambda: self.broadcast_ok,
            )

    def test_apply_verify_and_idempotent_apply(self):
        step = MaintenanceStep("write_environment")
        result = self.adapter.apply_step(self.transaction.change_id, "environment", step)
        self.assertTrue(result.succeeded)
        self.assertEqual(self.values, {"AIKB_HOME": "new", "AIKB_KNOWLEDGE_HOME": "new-k"})
        self.assertEqual(self.adapter.verify(self.transaction.change_id, "environment").status, "restart_required")
        writes = len(self.writes)
        self.adapter.apply_step(self.transaction.change_id, "environment", step)
        self.assertEqual(len(self.writes), writes)

    def test_second_write_or_broadcast_failure_restores_before_group(self):
        self.fail_name = "AIKB_KNOWLEDGE_HOME"
        with self.assertRaises(module.WindowsEnvironmentExecutionError):
            self.adapter.apply_step(self.transaction.change_id, "environment", MaintenanceStep("write_environment"))
        self.assertEqual(self.values, {"AIKB_HOME": "old", "AIKB_KNOWLEDGE_HOME": ""})
        self.fail_name = None
        self.broadcast_ok = False
        with self.assertRaises(module.WindowsEnvironmentExecutionError):
            self.adapter.apply_step(self.transaction.change_id, "environment", MaintenanceStep("write_environment"))
        self.assertEqual(self.values, {"AIKB_HOME": "old", "AIKB_KNOWLEDGE_HOME": ""})

    def test_rollback_preserves_missing_and_empty_semantics_and_rejects_drift(self):
        self.values.update({"AIKB_HOME": "new", "AIKB_KNOWLEDGE_HOME": "new-k"})
        missing_transaction = replace(self.transaction, leaf_states=(
            replace(self.transaction.leaf_states[0], existence="missing", before_hash=None),
            self.transaction.leaf_states[1],
        ))
        self.materials = _Materials(missing_transaction, missing=True)
        self.adapter._materials = self.materials
        self.adapter._transactions = _Store(missing_transaction)
        self.adapter.rollback_step(missing_transaction.change_id, "environment", MaintenanceStep("write_environment"))
        self.assertEqual(self.values, {"AIKB_HOME": None, "AIKB_KNOWLEDGE_HOME": ""})
        self.values["AIKB_HOME"] = "third-party"
        with self.assertRaises(module.WindowsEnvironmentExecutionError):
            self.adapter.rollback_step(self.transaction.change_id, "environment", MaintenanceStep("write_environment"))

    def test_agent_target_and_non_environment_step_are_rejected(self):
        with self.assertRaises(module.WindowsEnvironmentExecutionError):
            self.adapter.apply_step(self.transaction.change_id, "agent.codex", MaintenanceStep("write_mcp"))
        with self.assertRaises(module.WindowsEnvironmentExecutionError):
            self.adapter.apply_step(self.transaction.change_id, "environment", MaintenanceStep("verify"))

    def test_tampered_expected_material_or_after_fingerprint_is_rejected(self):
        # 通过低层故障注入模拟磁盘材料被第三方篡改；正常 dataclass 构造本身
        # 会拒绝 bytes/hash 不一致，因此这里绕过构造校验验证执行时仍会复核。
        tampered = self.materials.leaves[0]
        object.__setattr__(tampered, "expected_bytes", b"tampered")
        self.materials.leaves = (tampered, self.materials.leaves[1])
        self.materials.manifest = replace(self.materials.manifest, leaves=self.materials.leaves)
        with self.assertRaises(module.WindowsEnvironmentExecutionError):
            self.adapter.apply_step(self.transaction.change_id, "environment", MaintenanceStep("write_environment"))
        self.setUp()
        # 即使正文未变，expected_hash 被篡改也必须在执行前拒绝，不能把事务摘要
        # 当作可直接写入的值来源。
        tampered_hash = self.materials.leaves[0]
        object.__setattr__(tampered_hash, "expected_hash", "0" * 64)
        self.materials.leaves = (tampered_hash, self.materials.leaves[1])
        with self.assertRaises(module.WindowsEnvironmentExecutionError):
            self.adapter.apply_step(self.transaction.change_id, "environment", MaintenanceStep("write_environment"))
        self.setUp()
        self.adapter._transactions = _Store(replace(self.transaction, after_fingerprint="c" * 64))
        self.values.update({"AIKB_HOME": "new", "AIKB_KNOWLEDGE_HOME": "new-k"})
        with self.assertRaises(module.WindowsEnvironmentExecutionError):
            self.adapter.verify(self.transaction.change_id, "environment")

    def test_registry_write_preserves_existing_string_value_types_and_defaults_missing(self):
        """写入固定变量时保留 REG_SZ/REG_EXPAND_SZ，缺失值默认 REG_EXPAND_SZ。"""
        class _Key:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        class _Winreg:
            HKEY_CURRENT_USER = object()
            KEY_QUERY_VALUE = 0x0001
            KEY_SET_VALUE = 0x0002
            REG_SZ = 1
            REG_EXPAND_SZ = 2
            REG_MULTI_SZ = 7

            def __init__(self, value):
                self.values = {} if value is None else {"AIKB_KNOWLEDGE_HOME": ("old", value)}
                self.open_access = []
                self.writes = []

            def CreateKeyEx(self, _root, subkey, _reserved, access):
                self.open_access.append((subkey, access))
                return _Key()

            def QueryValueEx(self, _key, name):
                try:
                    return self.values[name]
                except KeyError:
                    raise FileNotFoundError(name) from None

            def SetValueEx(self, _key, name, _reserved, value_type, value):
                self.writes.append((name, value_type, value))
                self.values[name] = (value, value_type)

            def DeleteValue(self, _key, name):
                self.values.pop(name, None)

        for existing_type in (1, 2, None):
            with self.subTest(existing_type=existing_type):
                fake_winreg = _Winreg(existing_type)
                with patch.dict(sys.modules, {"winreg": fake_winreg}):
                    module.WindowsEnvironmentMaintenanceAdapter._write_user_environment(
                        "AIKB_KNOWLEDGE_HOME", "new"
                    )
                expected_type = existing_type or _Winreg.REG_EXPAND_SZ
                self.assertEqual(fake_winreg.writes, [("AIKB_KNOWLEDGE_HOME", expected_type, "new")])
                self.assertEqual(fake_winreg.open_access, [("Environment", _Winreg.KEY_QUERY_VALUE | _Winreg.KEY_SET_VALUE)])

    def test_registry_write_rejects_unexpected_type_and_supports_delete(self):
        """意外类型 fail-closed；删除仍只作用于固定目标名称。"""
        class _Key:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        class _Winreg:
            HKEY_CURRENT_USER = object()
            KEY_QUERY_VALUE = 1
            KEY_SET_VALUE = 2
            REG_SZ = 1
            REG_EXPAND_SZ = 2
            REG_DWORD = 4

            def __init__(self):
                self.deleted = []

            def CreateKeyEx(self, *_args):
                return _Key()

            def QueryValueEx(self, _key, _name):
                return 1, self.REG_DWORD

            def SetValueEx(self, *_args):
                raise AssertionError("意外类型不得写入")

            def DeleteValue(self, _key, name):
                self.deleted.append(name)

        fake_winreg = _Winreg()
        with patch.dict(sys.modules, {"winreg": fake_winreg}):
            with self.assertRaises(module.WindowsEnvironmentExecutionError):
                module.WindowsEnvironmentMaintenanceAdapter._write_user_environment("UNRELATED", "new")
            with self.assertRaises(module.WindowsEnvironmentExecutionError):
                module.WindowsEnvironmentMaintenanceAdapter._write_user_environment("AIKB_HOME", "new")
            module.WindowsEnvironmentMaintenanceAdapter._write_user_environment("AIKB_HOME", None)
        self.assertEqual(fake_winreg.deleted, ["AIKB_HOME"])


if __name__ == "__main__":
    unittest.main()
