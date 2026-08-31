"""Windows environment recovery platform 的隔离单元测试。"""

from __future__ import annotations

import hashlib
import sys
import types
import unittest
from unittest.mock import patch

from aikb_web.core.maintenance_materials import MaintenanceEnvironmentMaterial, MaintenanceLeafMaterial, MaintenanceMaterialStore
from aikb_web.core.maintenance_recovery import EnvironmentObservation, LeafRecoveryDecision, RecoveryDecision, RecoveryStep
from aikb_web.platform.windows import maintenance_recovery as module
from aikb_web.platform.windows.maintenance_readonly import WindowsMaintenanceAdapter


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class _Materials(MaintenanceMaterialStore):
    """仅内存材料桩；继承真实类型以验证适配器拒绝鸭子对象。"""

    def __init__(self):
        self.home = MaintenanceLeafMaterial("user_environment.aikb_home", "present", _hash(b"old"), _hash(b"new"), 0o600, b"old", b"new")
        self.knowledge = MaintenanceLeafMaterial("user_environment.aikb_knowledge_home", "present", _hash(b""), _hash(b"new-k"), 0o600, b"", b"new-k")

    def read_leaf(self, change_id, leaf_id):
        return self.home if leaf_id.endswith("aikb_home") else self.knowledge

    def read_environment(self, change_id, name):
        return MaintenanceEnvironmentMaterial(name, "value" if name == "AIKB_HOME" else "empty", "old" if name == "AIKB_HOME" else "")


class WindowsMaintenanceRecoveryTests(unittest.TestCase):
    """验证固定环境键的 missing/empty/value 观察和混合补偿。"""

    def _adapter(self, values, *, fail_name=None, broadcast=True):
        readonly = object.__new__(WindowsMaintenanceAdapter)
        readonly._environment = values
        readonly._environment_reader = None
        readonly._codex_home = None
        readonly._claude_home = None
        readonly._claude_user_config = None
        materials = _Materials()
        written = []

        def write(name, value):
            written.append((name, value))
            if name == fail_name:
                raise OSError("injected write failure")
            values[name] = value

        with patch.object(module.os, "name", "nt"):
            adapter = module.WindowsMaintenanceRecoveryPlatform(readonly, materials, environment_writer=write, environment_broadcaster=lambda: broadcast)
        return adapter, written

    def test_observe_distinguishes_missing_empty_and_value(self):
        adapter, _ = self._adapter({"AIKB_HOME": None, "AIKB_KNOWLEDGE_HOME": ""})
        self.assertEqual(adapter.observe_leaf("change-1", "environment", "user_environment.aikb_home"), EnvironmentObservation("missing", None))
        self.assertEqual(adapter.observe_leaf("change-1", "environment", "user_environment.aikb_knowledge_home"), EnvironmentObservation("empty", _hash(b"")))

    def test_mixed_environment_recovery_writes_only_fixed_keys(self):
        values = {"AIKB_HOME": "new", "AIKB_KNOWLEDGE_HOME": "new-k"}
        adapter, written = self._adapter(values)
        step = RecoveryStep(
            "write_environment",
            RecoveryDecision.RESTORE_BEFORE,
            ("user_environment.aikb_home", "user_environment.aikb_knowledge_home"),
            (
                LeafRecoveryDecision("user_environment.aikb_home", RecoveryDecision.RESTORE_BEFORE),
                LeafRecoveryDecision("user_environment.aikb_knowledge_home", RecoveryDecision.RESTORE_BEFORE),
            ),
        )
        result = adapter.recover_step("change-1", "environment", step)
        self.assertTrue(result.succeeded)
        self.assertEqual(written, [("AIKB_HOME", "old"), ("AIKB_KNOWLEDGE_HOME", "")])

    def test_agent_targets_and_non_windows_are_rejected(self):
        readonly = object.__new__(WindowsMaintenanceAdapter)
        readonly._environment = {"AIKB_HOME": None, "AIKB_KNOWLEDGE_HOME": None}
        readonly._environment_reader = None
        readonly._codex_home = readonly._claude_home = readonly._claude_user_config = None
        with patch.object(module.os, "name", "posix"):
            with self.assertRaises(module.WindowsMaintenanceRecoveryError):
                module.WindowsMaintenanceRecoveryPlatform(readonly, _Materials())
        with patch.object(module.os, "name", "nt"):
            adapter = module.WindowsMaintenanceRecoveryPlatform(readonly, _Materials(), environment_writer=lambda *_: None, environment_broadcaster=lambda: True)
            with self.assertRaises(module.WindowsMaintenanceRecoveryError):
                adapter.observe_leaf("change-1", "agent.codex", "agent.codex.mcp")

    def test_broadcast_failure_is_reported_after_registry_write(self):
        values = {"AIKB_HOME": "new", "AIKB_KNOWLEDGE_HOME": "new-k"}
        adapter, written = self._adapter(values, broadcast=False)
        step = RecoveryStep(
            "write_environment", RecoveryDecision.RESTORE_BEFORE,
            ("user_environment.aikb_home", "user_environment.aikb_knowledge_home"),
            (LeafRecoveryDecision("user_environment.aikb_home", RecoveryDecision.RESTORE_BEFORE),
             LeafRecoveryDecision("user_environment.aikb_knowledge_home", RecoveryDecision.RESTORE_BEFORE)),
        )
        with self.assertRaises(module.WindowsMaintenanceRecoveryError):
            adapter.recover_step("change-1", "environment", step)
        self.assertEqual(
            written,
            [("AIKB_HOME", "old"), ("AIKB_KNOWLEDGE_HOME", ""), ("AIKB_HOME", "new"), ("AIKB_KNOWLEDGE_HOME", "new-k")],
        )
        self.assertEqual(values, {"AIKB_HOME": "new", "AIKB_KNOWLEDGE_HOME": "new-k"})

    def test_second_variable_write_failure_restores_group_to_expected(self):
        values = {"AIKB_HOME": "new", "AIKB_KNOWLEDGE_HOME": "new-k"}
        adapter, _ = self._adapter(values, fail_name="AIKB_KNOWLEDGE_HOME")
        step = RecoveryStep(
            "write_environment", RecoveryDecision.RESTORE_BEFORE,
            ("user_environment.aikb_home", "user_environment.aikb_knowledge_home"),
            (LeafRecoveryDecision("user_environment.aikb_home", RecoveryDecision.RESTORE_BEFORE),
             LeafRecoveryDecision("user_environment.aikb_knowledge_home", RecoveryDecision.RESTORE_BEFORE)),
        )
        with self.assertRaises(module.WindowsMaintenanceRecoveryError):
            adapter.recover_step("change-1", "environment", step)
        self.assertEqual(values, {"AIKB_HOME": "new", "AIKB_KNOWLEDGE_HOME": "new-k"})

    def test_invalid_step_and_leaf_are_rejected(self):
        adapter, _ = self._adapter({"AIKB_HOME": "new", "AIKB_KNOWLEDGE_HOME": "new-k"})
        with self.assertRaises(module.WindowsMaintenanceRecoveryError):
            adapter.recover_step("change-1", "environment", object())
        with self.assertRaises(module.WindowsMaintenanceRecoveryError):
            adapter.observe_leaf("change-1", "environment", "agent.codex.mcp")

    def test_broadcast_uses_function_return_not_lpdwresult(self):
        """WM_SETTINGCHANGE 的 lpdwResult 可为 0，函数返回值才是成功信号。"""
        class Result:
            value = 0

        class User32:
            def __init__(self, returned):
                self.returned = returned

            def SendMessageTimeoutW(self, *args):
                return self.returned

        fake_ctypes = types.SimpleNamespace(
            c_ulong=Result,
            byref=lambda value: value,
            windll=types.SimpleNamespace(user32=User32(1)),
        )
        with patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            self.assertTrue(module.WindowsMaintenanceRecoveryPlatform._broadcast_environment())
            fake_ctypes.windll.user32.returned = 0
            self.assertFalse(module.WindowsMaintenanceRecoveryPlatform._broadcast_environment())


if __name__ == "__main__":
    unittest.main()
