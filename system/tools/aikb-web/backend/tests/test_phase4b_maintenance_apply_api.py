"""阶段 4B apply API 与任务闭环的隔离测试。

测试只使用内存事务和临时 TaskStore；不会访问 HKCU、Agent 配置或真实审计文件。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from aikb_web.core.actions import ConfirmationTokenService
from aikb_web.core.maintenance_materials import MaintenanceEnvironmentMaterial, MaintenanceLeafMaterial
from aikb_web.core.maintenance_execution import MaintenanceExecutionError, MaintenanceExecutor
from aikb_web.core.maintenance_preparation import MaintenancePreparationService
from aikb_web.core.maintenance_recovery_gate import MaintenanceRecoveryGate
from aikb_web.core.maintenance_targets import MAINTENANCE_TARGET_REGISTRY, MaintenanceTargetStatus
from aikb_web.platform.maintenance import MaintenancePlan, MaintenancePlatformCapabilities, MaintenanceStep
from aikb_web.core.maintenance_task import MaintenanceTaskCoordinator
from aikb_web.main import create_app
from tests.test_phase4b_maintenance_execution import _Adapter, _Audit, _Materials, _Store, _change


class _Transactions:
    """最小内存事务事实源；不创建维护材料目录。"""

    def __init__(self, value):
        self.value = value

    def load(self, change_id):
        if change_id != self.value.change_id:
            raise KeyError(change_id)
        return self.value

    def save(self, value):
        self.value = value


class _Executor:
    """返回固定 succeeded 事务，验证任务层只保存安全摘要。"""

    def __init__(self, transactions):
        self.transactions = transactions

    def execute(self, change_id, task_id):
        transaction = self.transactions.load(change_id)
        # 测试桩只证明协调器闭环；真实状态迁移由 MaintenanceExecutor 负责。
        applying = transaction.transition("applying", task_id=task_id, leaf_states=tuple(
            item.__class__(item.leaf_id, item.existence, item.before_hash, item.expected_hash, "applied")
            for item in transaction.leaf_states
        ))
        return applying.transition("verifying").transition(
            "succeeded",
            leaf_states=tuple(
                item.__class__(item.leaf_id, item.existence, item.before_hash, item.expected_hash, "verified")
                for item in transaction.leaf_states
            ),
        )


class _Gateway:
    settings = None


class _PreparationTransactions:
    """仅记录准备服务创建的事务，验证目标 ID 未被硬编码为 environment。"""

    def __init__(self):
        self.items = []

    def create(self, value):
        self.items.append(value)


class _PreparationMaterials:
    def __init__(self):
        self.target_id = None

    def prepare(self, change_id, target_id, leaves, environments):
        self.target_id = target_id
        return None


class _AgentProvider:
    """返回三叶子 Agent 私有材料，不返回环境正文。"""

    def capture_agent(self, plan):
        leaves = {}
        for leaf_id in plan.logical_leaves:
            expected = b"expected"
            leaves[leaf_id] = MaintenanceLeafMaterial(
                leaf_id, "missing", None,
                __import__("hashlib").sha256(expected).hexdigest(), None, None, expected,
            )
        return SimpleNamespace(target_id=plan.target_id, base_fingerprint=plan.before_fingerprint), leaves, {}

    def managed_fingerprint_part(self, target_id, leaf_id, raw):
        """测试提供器复现固定摘要接口，避免准备服务回退到整文件摘要。"""
        assert target_id.startswith("agent.")
        digest = __import__("hashlib").sha256(b"<missing>" if raw is None else raw).hexdigest()
        return f"{leaf_id}:{digest}"


class _MaterializeTransactions:
    """记录 apply 阶段才创建的事务，测试不使用正式事务目录。"""

    def __init__(self):
        self.items = []

    def create(self, value):
        self.items.append(value)


class _MaterializeMaterials:
    """记录私有材料准备调用，避免测试写入本机配置或运行面。"""

    def __init__(self):
        self.calls = 0

    def prepare(self, *args):
        self.calls += 1
        return None


class _EnvironmentProvider:
    """生成与固定 environment 缺失状态匹配的最小可信材料。"""

    def capture_environment(self, plan):
        expected = b"expected"
        expected_hash = __import__("hashlib").sha256(expected).hexdigest()
        leaves = {
            leaf_id: MaintenanceLeafMaterial(leaf_id, "missing", None, expected_hash, None, None, expected)
            for leaf_id in plan.logical_leaves
        }
        environments = {
            name: MaintenanceEnvironmentMaterial(name, "missing")
            for name in ("AIKB_HOME", "AIKB_KNOWLEDGE_HOME")
        }
        target = MAINTENANCE_TARGET_REGISTRY.get("environment")
        status = MaintenanceTargetStatus(
            "environment", "missing", target.logical_leaves, target.steps,
            "target_missing", plan.before_fingerprint,
        )
        return status, leaves, environments


class _PreviewOnlyAdapter:
    """仅提供 inspect/plan，证明 staged preview 不要求捕获或写入能力。"""

    def __init__(self):
        self.base = "a" * 64

    def inspect(self, target_id):
        target = MAINTENANCE_TARGET_REGISTRY.get(target_id)
        return MaintenanceTargetStatus(target_id, "drifted", target.logical_leaves, target.steps, "managed_content_drifted", self.base)

    def plan(self, target_id, _inspection):
        target = MAINTENANCE_TARGET_REGISTRY.get(target_id)
        return MaintenancePlan(
            target_id,
            tuple(MaintenanceStep(step) for step in target.steps),
            target.logical_leaves,
            self.base,
            "b" * 64,
            "c" * 64,
        )


class MaintenanceApplyApiTests(unittest.TestCase):
    """覆盖严格请求、单次令牌、任务关联和无敏感投影。"""

    def test_apply_and_change_status_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            change = _change("environment")
            transactions = _Transactions(change)
            tokens = ConfirmationTokenService()
            token = tokens.issue(
                action_id=change.action_id,
                parameters={"change_id": change.change_id},
                risk_level=change.risk_level,
                preview_digest=change.preview_digest,
            )
            coordinator = MaintenanceTaskCoordinator(
                _Executor(transactions),
                transactions=transactions,
                token_service=tokens,
                workspace_root=Path(directory),
            )
            app = create_app(_Gateway(), maintenance_task_coordinator=coordinator)
            headers = {
                "Content-Type": "application/json",
                "X-AIKB-Request": "1",
                "Host": "localhost:80",
                "Origin": "http://localhost:80",
            }
            try:
                with TestClient(app) as client:
                    response = client.post(
                        f"/api/v1/maintenance/changes/{change.change_id}/apply",
                        headers=headers,
                        json={"confirmation_token": token},
                    )
                    self.assertEqual(response.status_code, 200)
                    data = response.json()["data"]
                    self.assertEqual(data["change_id"], change.change_id)
                    self.assertTrue(data["task_id"])
                    self.assertNotIn("confirmation_token", response.text)
                    self.assertNotIn("private", response.text)
                    status = client.get(f"/api/v1/maintenance/changes/{change.change_id}")
                    self.assertEqual(status.status_code, 200)
                    envelope = status.json()["data"]
                    self.assertIn(envelope["change"]["status"], {"prepared", "succeeded"})
                    self.assertEqual(envelope["task"]["change_id"], change.change_id)
                    self.assertNotIn("confirmation_token", status.text)
                    duplicate = client.post(
                        f"/api/v1/maintenance/changes/{change.change_id}/apply",
                        headers=headers,
                        json={"confirmation_token": token},
                    )
                    self.assertEqual(duplicate.status_code, 409)
            finally:
                coordinator.shutdown()

    def test_worker_failure_before_claim_persists_recovery_required(self):
        """worker 已创建任务但认领前失败时不得遗留可重试的 prepared。"""
        class FailingExecutor:
            def execute(self, change_id, task_id):
                del change_id, task_id
                raise RuntimeError("injected scheduling failure")

        with tempfile.TemporaryDirectory() as directory:
            change = _change("environment")
            transactions = _Transactions(change)
            tokens = ConfirmationTokenService()
            token = tokens.issue(
                action_id=change.action_id,
                parameters={"change_id": change.change_id},
                risk_level=change.risk_level,
                preview_digest=change.preview_digest,
            )
            coordinator = MaintenanceTaskCoordinator(
                FailingExecutor(),
                transactions=transactions,
                token_service=tokens,
                workspace_root=Path(directory),
            )
            try:
                coordinator.apply(change_id=change.change_id, confirmation_token=token)
            finally:
                coordinator.shutdown()
            self.assertEqual(transactions.value.status, "recovery_required")
            self.assertTrue(transactions.value.task_id)

    def test_apply_extra_field_and_missing_service_fail_closed(self):
        app = create_app(_Gateway())
        headers = {
            "Content-Type": "application/json",
            "X-AIKB-Request": "1",
            "Host": "localhost:80",
            "Origin": "http://localhost:80",
        }
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/maintenance/changes/change-1/apply",
                headers=headers,
                json={"confirmation_token": "token", "path": "C:\\private"},
            )
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("private", response.text)

    def test_preparation_keeps_agent_target_and_material_boundary(self):
        """Agent 事务准备使用目标专属叶子并把正文留在材料存储。"""
        import hashlib

        target = "agent.codex"
        leaves = ("agent.codex.root_instructions", "agent.codex.mcp", "agent.codex.hooks")
        before_hash = hashlib.sha256(b"<missing>").hexdigest()
        expected_hash = hashlib.sha256(b"expected").hexdigest()
        before = hashlib.sha256("\n".join(f"{leaf}:{before_hash}" for leaf in leaves).encode()).hexdigest()
        after = hashlib.sha256("\n".join(f"{leaf}:{expected_hash}" for leaf in leaves).encode()).hexdigest()
        plan = SimpleNamespace(
            target_id=target,
            logical_leaves=leaves,
            before_fingerprint=before,
            after_fingerprint=after,
            preview_digest="f" * 64,
            steps=tuple(SimpleNamespace(step_id=item) for item in ("preflight", "backup", "write_root_instructions", "write_mcp", "write_hooks", "verify")),
        )
        status = SimpleNamespace(target_id=target, status="missing", base_fingerprint=before)
        transactions = _PreparationTransactions()
        materials = _PreparationMaterials()
        service = MaintenancePreparationService(transactions, lambda _: materials, ConfirmationTokenService())
        prepared = service.prepare(plan, status, _AgentProvider())
        self.assertEqual(prepared.change.target_id, target)
        self.assertEqual(materials.target_id, target)

    def test_stage_preview_has_no_transaction_or_material_side_effect(self):
        """preview 只暂存内存 plan，materialize 才创建事务和私有材料。"""
        import hashlib

        target = MAINTENANCE_TARGET_REGISTRY.get("environment")
        missing_hash = hashlib.sha256(b"<missing>").hexdigest()
        expected_hash = hashlib.sha256(b"expected").hexdigest()
        before = hashlib.sha256("\n".join(f"{leaf}:{missing_hash}" for leaf in target.logical_leaves).encode()).hexdigest()
        after = hashlib.sha256("\n".join(f"{leaf}:{expected_hash}" for leaf in target.logical_leaves).encode()).hexdigest()
        plan = MaintenancePlan(
            "environment", tuple(MaintenanceStep(step) for step in target.steps), target.logical_leaves,
            before, after, "f" * 64,
        )
        status = MaintenanceTargetStatus("environment", "missing", target.logical_leaves, target.steps, "target_missing", before)
        transactions = _MaterializeTransactions()
        materials = _MaterializeMaterials()
        service = MaintenancePreparationService(transactions, lambda _: materials, ConfirmationTokenService())
        staged = service.stage(plan, status)
        self.assertEqual(transactions.items, [])
        self.assertEqual(materials.calls, 0)
        service.materialize(staged, plan, status, _EnvironmentProvider(), staged.confirmation_token)
        self.assertEqual(len(transactions.items), 1)
        self.assertEqual(materials.calls, 1)

    def test_preflight_failure_does_not_consume_confirmation(self):
        """执行器在材料/preflight 失败时不触发令牌消费回调。"""
        transaction = _change("environment")
        tokens = ConfirmationTokenService()
        token = tokens.issue(
            action_id=transaction.action_id,
            parameters={"change_id": transaction.change_id},
            risk_level=transaction.risk_level,
            preview_digest=transaction.preview_digest,
        )
        store = _Store(transaction)
        adapter = _Adapter(fail_step="preflight")
        gate = MaintenanceRecoveryGate(); gate.complete_scan((), ())
        executor = MaintenanceExecutor(
            store, adapter, tempfile.gettempdir(), _Materials(transaction), _Audit(), gate,
        )
        with self.assertRaises(MaintenanceExecutionError):
            executor.execute(
                transaction.change_id,
                "task-preflight-token",
                before_claim=lambda _current: tokens.consume(
                    token,
                    action_id=transaction.action_id,
                    parameters={"change_id": transaction.change_id},
                    risk_level=transaction.risk_level,
                    preview_digest=transaction.preview_digest,
                ),
            )
        # 令牌仍可由测试消费，证明 preflight 失败路径没有提前消费。
        tokens.consume(
            token,
            action_id=transaction.action_id,
            parameters={"change_id": transaction.change_id},
            risk_level=transaction.risk_level,
            preview_digest=transaction.preview_digest,
        )

    def test_api_preview_stages_without_transaction_task_or_audit(self):
        """POST preview 前后不触碰事务、任务和审计事实源。"""
        adapter = _PreviewOnlyAdapter()
        transactions = _PreparationTransactions()
        service = MaintenancePreparationService(transactions, lambda _: _MaterializeMaterials(), ConfirmationTokenService())
        app = create_app(
            _Gateway(),
            maintenance_adapter=adapter,
            maintenance_preparation_service=service,
        )
        headers = {
            "Content-Type": "application/json",
            "X-AIKB-Request": "1",
            "Host": "localhost:80",
            "Origin": "http://localhost:80",
        }
        capabilities = MaintenancePlatformCapabilities("windows", "AMD64", True, "none", "windows-maintenance")
        with patch("aikb_web.api.v1.maintenance.maintenance_platform_capabilities", return_value=capabilities):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/maintenance/targets/agent.codex/preview",
                    headers=headers,
                    json={"base_fingerprint": adapter.base},
                )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["data"]["change_id"])
        self.assertEqual(transactions.items, [])


if __name__ == "__main__":
    unittest.main()
