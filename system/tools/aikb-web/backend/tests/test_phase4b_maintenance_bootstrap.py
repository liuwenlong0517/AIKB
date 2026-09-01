"""生产默认维护恢复组合根的惰性装配测试。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aikb_web.core.actions import ConfirmationTokenService
from aikb_web.core.maintenance_bootstrap import (
    _WindowsMaintenanceDispatchAdapter,
    build_default_maintenance_recovery,
    build_default_maintenance_services,
)
from aikb_web.core.maintenance_lock import MaintenanceWriteLock
from aikb_web.core.maintenance_preparation import MaintenancePreparationService
from aikb_web.core.maintenance_recovery_gate import MaintenanceRecoveryGate
from aikb_web.core.maintenance_materials import MaintenanceEnvironmentMaterial, MaintenanceLeafMaterial
from aikb_web.core.maintenance_targets import MAINTENANCE_TARGET_REGISTRY, MaintenanceTargetStatus
from aikb_web.platform.maintenance import MaintenancePlan, MaintenanceStep
from aikb_web.platform.windows.maintenance_readonly import WindowsMaintenanceAdapter


class _Audit:
    """真实 AuditStore 形状的无事件桩。"""

    def read_events(self):
        return {"items": [], "damaged": []}

    def write(self, record):
        return {"written": True}


class _ReadonlyCapture:
    """最小只读材料提供器；记录调用以验证分派边界。"""

    def __init__(self, result=None):
        self.calls = 0
        self.result = result

    def capture_environment(self, plan):
        self.calls += 1
        return self.result or ("readonly-capture", plan)


class _ExecutionCapture:
    """执行适配器桩；environment 捕获若被误调用会立即失败。"""

    def capture_environment(self, plan):
        raise AssertionError("environment 材料不得从执行适配器捕获")


class _PreparedTransactions:
    """只记录 prepared 事务，不创建正式运行目录。"""

    def __init__(self):
        self.items = []

    def create(self, value):
        self.items.append(value)


class _PreparedMaterials:
    """记录材料落盘调用，测试不触碰真实用户配置。"""

    def __init__(self):
        self.calls = []

    def prepare(self, *args):
        self.calls.append(args)


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

    def test_dispatch_environment_capture_uses_readonly_not_execution_adapter(self) -> None:
        """生产分派的 environment 材料必须来自只读适配器，执行器不得被调用。"""
        readonly = _ReadonlyCapture()
        execution = _ExecutionCapture()
        dispatch = _WindowsMaintenanceDispatchAdapter(readonly, execution, {})
        plan = SimpleNamespace(target_id="environment")
        self.assertEqual(dispatch.capture(plan), ("readonly-capture", plan))
        self.assertEqual(readonly.calls, 1)

    def test_dispatch_agent_managed_fingerprint_uses_agent_provider(self) -> None:
        """生产分派把受管正文摘要交给对应 Agent provider，不接受自由实现。"""
        calls = []

        class _Agent:
            def managed_fingerprint_part(self, target_id, leaf_id, raw):
                calls.append((target_id, leaf_id, raw))
                return f"{leaf_id}:missing"

        dispatch = _WindowsMaintenanceDispatchAdapter(_ReadonlyCapture(), _ExecutionCapture(), {"agent.codex": _Agent()})
        self.assertEqual(dispatch.managed_fingerprint_part("agent.codex", "agent.codex.mcp", None), "agent.codex.mcp:missing")
        self.assertEqual(calls, [("agent.codex", "agent.codex.mcp", None)])

    def test_default_services_wires_environment_capture_to_readonly_dispatch(self) -> None:
        """默认组合根把只读适配器和执行适配器分别注入，回归生产装配路径。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            readonly = _ReadonlyCapture()
            readonly._read_environment = lambda: {}
            execution = _ExecutionCapture()
            agent = SimpleNamespace(capture_agent=lambda plan: ("agent-capture", plan))
            settings = SimpleNamespace(workspace_root=workspace)
            gate = MaintenanceRecoveryGate()
            tokens = ConfirmationTokenService()
            services = None
            try:
                with patch("aikb_web.core.maintenance_bootstrap.WindowsEnvironmentMaintenanceAdapter", return_value=execution), \
                     patch("aikb_web.core.maintenance_bootstrap.WindowsAgentMaintenanceAdapter", return_value=agent), \
                     patch("aikb_web.core.maintenance_bootstrap.WindowsAgentProbeRunner", return_value=lambda _agent: True):
                    services = build_default_maintenance_services(
                        settings, readonly, SimpleNamespace(web_audit_write=None), gate,
                        MaintenanceWriteLock(workspace), tokens,
                    )
                self.assertIsNotNone(services)
                _preparation, executor, coordinator = services
                dispatch = executor._adapter
                self.assertIs(dispatch._readonly, readonly)
                self.assertIs(dispatch._environment, execution)
                plan = SimpleNamespace(target_id="environment")
                self.assertEqual(dispatch.capture(plan), ("readonly-capture", plan))
                self.assertEqual(readonly.calls, 1)
                agent_plan = SimpleNamespace(target_id="agent.codex")
                self.assertEqual(dispatch.capture(agent_plan), ("agent-capture", agent_plan))
            finally:
                if services is not None:
                    coordinator.shutdown()

    def test_dispatch_environment_capture_materializes_prepared_transaction(self) -> None:
        """真实准备服务可经生产分派捕获环境材料并完成 prepared。"""
        target = MAINTENANCE_TARGET_REGISTRY.get("environment")
        missing_hash = hashlib.sha256(b"<missing>").hexdigest()
        expected_hash = hashlib.sha256(b"expected").hexdigest()
        before = hashlib.sha256("\n".join(f"{leaf}:{missing_hash}" for leaf in target.logical_leaves).encode()).hexdigest()
        after = hashlib.sha256("\n".join(f"{leaf}:{expected_hash}" for leaf in target.logical_leaves).encode()).hexdigest()
        plan = MaintenancePlan(
            "environment", tuple(MaintenanceStep(step) for step in target.steps), target.logical_leaves,
            before, after, "c" * 64,
        )
        status = MaintenanceTargetStatus("environment", "missing", target.logical_leaves, target.steps, "target_missing", before)
        leaves = {
            leaf_id: MaintenanceLeafMaterial(leaf_id, "missing", None, expected_hash, None, None, b"expected")
            for leaf_id in target.logical_leaves
        }
        environments = {
            name: MaintenanceEnvironmentMaterial(name, "missing")
            for name in ("AIKB_HOME", "AIKB_KNOWLEDGE_HOME")
        }
        readonly = _ReadonlyCapture((status, leaves, environments))
        dispatch = _WindowsMaintenanceDispatchAdapter(readonly, _ExecutionCapture(), {})
        transactions = _PreparedTransactions()
        materials = _PreparedMaterials()
        service = MaintenancePreparationService(transactions, lambda _store: materials, ConfirmationTokenService())
        staged = service.stage(plan, status)
        prepared = service.materialize(staged, plan, status, dispatch, staged.confirmation_token)
        self.assertEqual(prepared.change.status, "prepared")
        self.assertEqual(prepared.change.target_id, "environment")
        self.assertEqual(readonly.calls, 1)
        self.assertEqual(len(transactions.items), 1)
        self.assertEqual(len(materials.calls), 1)


if __name__ == "__main__":
    unittest.main()
