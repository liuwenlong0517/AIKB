"""阶段 4B 通用补偿执行器的顺序、持久化和并发认领测试。"""

from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from aikb_web.core.maintenance_changes import MaintenanceChange, MaintenanceLeafState
from aikb_web.core.maintenance_execution import MaintenanceExecutionError, MaintenanceExecutor
from aikb_web.core.maintenance_lock import MaintenanceClaimCoordinator, MaintenanceClaimError, MaintenanceWriteLock
from aikb_web.core.maintenance_materials import MaintenanceMaterialManifest
from aikb_web.core.maintenance_recovery_gate import MaintenanceRecoveryGate
from aikb_web.core.maintenance_targets import MAINTENANCE_WRITE_LEAVES_BY_TARGET
from aikb_web.platform.maintenance import MaintenanceStepResult, MaintenanceVerification


class _Store:
    """内存事实源：记录每次 save，模拟真实 Store 的原子接口。"""

    def __init__(self, transaction: MaintenanceChange, fail_status: str | None = None, fail_count: int = 1) -> None:
        self.current = transaction
        self.saved: list[MaintenanceChange] = []
        self.guard = threading.Lock()
        self.fail_status = fail_status
        self.fail_count = fail_count

    def load(self, change_id: str) -> MaintenanceChange:
        with self.guard:
            if self.current.change_id != change_id:
                raise ValueError("missing")
            return self.current

    def save(self, transaction: MaintenanceChange) -> None:
        with self.guard:
            if transaction.status == self.fail_status and self.fail_count > 0:
                self.fail_count -= 1
                raise OSError("injected save failure")
            self.current = transaction
            self.saved.append(transaction)


class _Adapter:
    """可控 fake 适配器；不访问文件、环境或任何真实配置。"""

    def __init__(self, *, fail_step: str | None = None, fail_verify: bool = False, fail_rollback: str | None = None, raise_step: str | None = None, raise_rollback: str | None = None) -> None:
        self.fail_step = fail_step
        self.fail_verify = fail_verify
        self.fail_rollback = fail_rollback
        self.raise_step = raise_step
        self.raise_rollback = raise_rollback
        self.calls: list[tuple[str, str]] = []

    def apply_step(self, change_id: str, target_id: str, step: object) -> MaintenanceStepResult:
        step_id = getattr(step, "step_id")
        self.calls.append(("apply", step_id))
        if step_id == self.raise_step:
            raise OSError("injected write exception")
        failed = step_id == self.fail_step
        return MaintenanceStepResult(change_id, target_id, step_id, not failed, "failed" if failed else "applied")

    def verify(self, change_id: str, target_id: str) -> MaintenanceVerification:
        self.calls.append(("verify", "verify"))
        if self.fail_verify:
            return MaintenanceVerification(change_id, target_id, "ready", after_fingerprint="0" * 64)
        return MaintenanceVerification(change_id, target_id, "ready", after_fingerprint="c" * 64)

    def rollback_step(self, change_id: str, target_id: str, step: object) -> MaintenanceStepResult:
        step_id = getattr(step, "step_id")
        self.calls.append(("rollback", step_id))
        if step_id == self.raise_rollback:
            raise OSError("injected rollback exception")
        failed = step_id == self.fail_rollback
        return MaintenanceStepResult(change_id, target_id, step_id, not failed, "failed" if failed else "rolled_back")

    def recover(self, change_id: str) -> object:
        raise AssertionError("本批不应调用启动恢复")


class _Materials:
    """只返回与事务摘要绑定的安全 manifest，不保存任何真实正文。"""

    def __init__(self, transaction: MaintenanceChange, fail: bool = False, cleanup_fail: bool = False) -> None:
        self.transaction = transaction
        self.fail = fail
        self.cleanup_fail = cleanup_fail
        self.cleanup_calls: list[str] = []

    def load(self, change_id: str) -> MaintenanceMaterialManifest:
        if self.fail:
            raise ValueError("private material unavailable")
        leaves = tuple(
            type("Material", (), {"leaf_id": leaf.leaf_id, "existence": leaf.existence, "before_hash": leaf.before_hash, "expected_hash": leaf.expected_hash})()
            for leaf in self.transaction.leaf_states
        )
        environments = ()
        if self.transaction.target_id == "environment":
            environments = tuple(type("Environment", (), {"state": "value"})() for _ in self.transaction.leaf_states)
        return MaintenanceMaterialManifest(change_id, self.transaction.target_id, leaves, environments, "f" * 64)  # type: ignore[arg-type]

    def cleanup(self, change_id: str) -> None:
        """记录终态材料清理；可注入失败验证事务结果不被反转。"""

        self.cleanup_calls.append(change_id)
        if self.cleanup_fail:
            raise OSError("injected cleanup failure")


class _Audit:
    """注入式审计门禁 fake；测试可控其终态记录结果。"""

    def __init__(self, finish_ok: bool = True, start_ok: bool = True, raise_finish: bool = False) -> None:
        self.finish_ok = finish_ok
        self.start_ok = start_ok
        self.raise_finish = raise_finish
        self.events: list[str] = []

    def start(self, transaction: MaintenanceChange, task_id: str) -> bool:
        self.events.append("start")
        return self.start_ok

    def finish(self, transaction: MaintenanceChange, outcome: str) -> bool:
        self.events.append(outcome)
        if self.raise_finish:
            raise OSError("injected audit exception")
        return self.finish_ok


def _change(target_id: str) -> MaintenanceChange:
    if target_id == "environment":
        action = "maintenance.environment.update"
        steps = ("preflight", "backup", "write_environment", "verify")
        leaf_ids = ("user_environment.aikb_home", "user_environment.aikb_knowledge_home")
    elif target_id == "agent.codex":
        action = "maintenance.agent.codex.repair"
        steps = ("preflight", "backup", "write_root_instructions", "write_mcp", "write_hooks", "verify")
        leaf_ids = ("agent.codex.root_instructions", "agent.codex.mcp", "agent.codex.hooks")
    else:
        action = "maintenance.agent.claude-code.repair"
        steps = ("preflight", "backup", "write_root_instructions", "write_mcp", "write_hooks", "verify")
        leaf_ids = ("agent.claude-code.root_instructions", "agent.claude-code.mcp", "agent.claude-code.hooks")
    leaves = tuple(MaintenanceLeafState(item, "present", "d" * 64, "e" * 64) for item in leaf_ids)
    return MaintenanceChange(
        change_id=f"execution-{target_id.replace('.', '-')}", target_id=target_id, action_id=action,
        risk_level="user_config_write", status="prepared", base_fingerprint="a" * 64,
        before_fingerprint="b" * 64, after_fingerprint="c" * 64, step_summary=steps,
        preview_digest="f" * 64, created_at="2026-08-31T01:00:00Z", expires_at="2099-08-31T02:00:00Z",
        updated_at="2026-08-31T01:00:00Z", leaf_states=leaves,
    )


class MaintenanceExecutionTests(unittest.TestCase):
    """验证三目标固定顺序、逐步落盘、补偿回滚和唯一认领。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _executor(
        self,
        store: _Store,
        adapter: _Adapter,
        audit: _Audit | None = None,
        materials: _Materials | None = None,
    ) -> MaintenanceExecutor:
        gate = MaintenanceRecoveryGate(); gate.complete_scan((), ())
        return MaintenanceExecutor(store, adapter, str(self.workspace), materials or _Materials(store.current), audit or _Audit(), gate)

    def test_all_targets_follow_declared_order_and_succeed(self) -> None:
        """环境、Codex、Claude Code 均按静态步骤执行并最终 succeeded。"""
        for target_id in ("environment", "agent.codex", "agent.claude-code"):
            store = _Store(_change(target_id))
            adapter = _Adapter()
            result = self._executor(store, adapter).execute(store.current.change_id, "task-exec")
            self.assertEqual(result.status, "succeeded")
            self.assertEqual([name for kind, name in adapter.calls if kind == "apply"], list(store.current.step_summary[:-1]))
            self.assertEqual([kind for kind, _ in adapter.calls], ["apply"] * (len(store.current.step_summary) - 1) + ["verify"])

    def test_terminal_transactions_cleanup_private_materials_without_reversing_outcome(self) -> None:
        """成功和已补偿回滚均清材料；清理失败仍保留已确认事务终态。"""

        succeeded_store = _Store(_change("environment"))
        succeeded_materials = _Materials(succeeded_store.current)
        succeeded = self._executor(
            succeeded_store, _Adapter(), materials=succeeded_materials,
        ).execute(succeeded_store.current.change_id, "task-exec")
        self.assertEqual(succeeded.status, "succeeded")
        self.assertEqual(succeeded_materials.cleanup_calls, [succeeded.change_id])

        rolled_store = _Store(_change("agent.codex"))
        rolled_materials = _Materials(rolled_store.current, cleanup_fail=True)
        rolled = self._executor(
            rolled_store, _Adapter(fail_step="write_mcp"), materials=rolled_materials,
        ).execute(rolled_store.current.change_id, "task-exec")
        self.assertEqual(rolled.status, "rolled_back")
        self.assertEqual(rolled_materials.cleanup_calls, [rolled.change_id])

    def test_static_write_leaf_mapping_matches_each_target(self) -> None:
        """执行器使用目标注册表唯一提供的步骤—叶子映射。"""
        self.assertEqual(tuple(MAINTENANCE_WRITE_LEAVES_BY_TARGET["environment"]["write_environment"]), ("user_environment.aikb_home", "user_environment.aikb_knowledge_home"))
        self.assertEqual(tuple(MAINTENANCE_WRITE_LEAVES_BY_TARGET["agent.codex"]), ("write_root_instructions", "write_mcp", "write_hooks"))
        self.assertEqual(tuple(MAINTENANCE_WRITE_LEAVES_BY_TARGET["agent.claude-code"]), ("write_root_instructions", "write_mcp", "write_hooks"))

    def test_second_or_third_write_failure_rolls_back_in_reverse_order(self) -> None:
        """第二/第三写步骤失败时，只补偿已成功写入的步骤且逆序持久化。"""
        for failed_step in ("write_mcp", "write_hooks"):
            store = _Store(_change("agent.codex"))
            adapter = _Adapter(fail_step=failed_step)
            result = self._executor(store, adapter).execute(store.current.change_id, "task-exec")
            self.assertEqual(result.status, "rolled_back")
            rollbacks = [name for kind, name in adapter.calls if kind == "rollback"]
            expected = ["write_mcp", "write_root_instructions"] if failed_step == "write_mcp" else ["write_hooks", "write_mcp", "write_root_instructions"]
            self.assertEqual(rollbacks, expected)

    def test_verify_failure_is_compensated(self) -> None:
        """验证结果不是事务期望指纹时进入回滚，不得宣称成功。"""
        store = _Store(_change("environment"))
        result = self._executor(store, _Adapter(fail_verify=True)).execute(store.current.change_id, "task-exec")
        self.assertEqual(result.status, "rolled_back")

    def test_rollback_failure_marks_recovery_required(self) -> None:
        """无法证明逆序恢复时标记恢复叶子并阻断成功终态。"""
        store = _Store(_change("agent.codex"))
        result = self._executor(store, _Adapter(fail_step="write_mcp", fail_rollback="write_root_instructions")).execute(store.current.change_id, "task-exec")
        self.assertEqual(result.status, "recovery_required")
        self.assertIn("recovery_required", {leaf.progress for leaf in result.leaf_states})

    def test_every_write_and_state_transition_is_persisted(self) -> None:
        """认领、验证态、每个写叶子和终态均写回事实源。"""
        store = _Store(_change("agent.codex"))
        result = self._executor(store, _Adapter()).execute(store.current.change_id, "task-exec")
        statuses = [item.status for item in store.saved]
        self.assertEqual(statuses[0], "applying")
        self.assertIn("verifying", statuses)
        self.assertEqual(statuses[-1], "succeeded")
        self.assertEqual(result.status, "succeeded")

    def test_only_one_concurrent_executor_can_claim(self) -> None:
        """共享维护锁内只有一方能把 prepared 事务认领为 applying。"""
        store = _Store(_change("environment"))
        outcomes: list[str] = []
        guard = threading.Lock()

        def run() -> None:
            try:
                result = self._executor(store, _Adapter()).execute(store.current.change_id, "task-exec")
                value = result.status
            except MaintenanceExecutionError:
                value = "rejected"
            with guard:
                outcomes.append(value)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["rejected", "succeeded"])

    def test_material_or_audit_start_failure_does_not_claim(self) -> None:
        """材料绑定或开始审计失败必须保持 prepared，不能先认领再补救。"""
        for audit in (_Audit(start_ok=False),):
            transaction = _change("environment")
            store = _Store(transaction)
            gate = MaintenanceRecoveryGate(); gate.complete_scan((), ())
            executor = MaintenanceExecutor(store, _Adapter(), str(self.workspace), _Materials(transaction), audit, gate)
            with self.assertRaises(MaintenanceExecutionError):
                executor.execute(transaction.change_id, "task-exec")
            self.assertEqual(store.current.status, "prepared")
        transaction = _change("environment")
        store = _Store(transaction)
        gate = MaintenanceRecoveryGate(); gate.complete_scan((), ())
        executor = MaintenanceExecutor(store, _Adapter(), str(self.workspace), _Materials(transaction, fail=True), _Audit(), gate)
        with self.assertRaises(MaintenanceExecutionError):
            executor.execute(transaction.change_id, "task-exec")
        self.assertEqual(store.current.status, "prepared")

    def test_failed_rollback_audit_keeps_rollback_nonterminal(self) -> None:
        """终态回滚审计失败时保留 rolling_back，不能伪造 rolled_back。"""
        transaction = _change("environment")
        store = _Store(transaction)
        result_audit = _Audit(finish_ok=False)
        gate = MaintenanceRecoveryGate(); gate.complete_scan((), ())
        executor = MaintenanceExecutor(store, _Adapter(fail_step="write_environment"), str(self.workspace), _Materials(transaction), result_audit, gate)
        with self.assertRaises(MaintenanceExecutionError):
            executor.execute(transaction.change_id, "task-exec")
        self.assertEqual(store.current.status, "rolling_back")

    def test_success_audit_failure_keeps_verifying_without_rollback(self) -> None:
        """验证已成功但成功审计失败时保持 verifying，绝不补偿已验证内容。"""
        transaction = _change("environment")
        store = _Store(transaction)
        adapter = _Adapter()
        with self.assertRaises(MaintenanceExecutionError):
            self._executor(store, adapter, _Audit(finish_ok=False)).execute(transaction.change_id, "task-exec")
        self.assertEqual(store.current.status, "verifying")
        self.assertNotIn("rollback", [kind for kind, _ in adapter.calls])

    def test_constructor_rejects_incomplete_audit_gate(self) -> None:
        """缺失 start/finish 门禁不能构造执行器。"""
        transaction = _change("environment")
        with self.assertRaises(MaintenanceExecutionError):
            gate = MaintenanceRecoveryGate(); gate.complete_scan((), ())
            MaintenanceExecutor(_Store(transaction), _Adapter(), str(self.workspace), _Materials(transaction), object(), gate)

    def test_preflight_failure_or_exception_stays_prepared_without_audit(self) -> None:
        """预检返回失败或抛异常都不得认领、审计或开始写入。"""
        for adapter in (_Adapter(fail_step="preflight"), _Adapter(raise_step="preflight")):
            transaction = _change("environment")
            store = _Store(transaction)
            audit = _Audit()
            with self.assertRaises(MaintenanceExecutionError):
                self._executor(store, adapter, audit).execute(transaction.change_id, "task-exec")
            self.assertEqual(store.current.status, "prepared")
            self.assertEqual(audit.events, [])

    def test_invalid_expired_or_nonprepared_never_calls_audit(self) -> None:
        """过期、非法 task_id 和非 prepared 事务均在审计门禁前拒绝。"""
        for transaction, task_id in ((_change("environment"), "bad/task"),):
            audit = _Audit()
            with self.assertRaises(MaintenanceExecutionError):
                self._executor(_Store(transaction), _Adapter(), audit).execute(transaction.change_id, task_id)
            self.assertEqual(audit.events, [])
        expired = replace(_change("environment"), created_at="2019-01-01T00:00:00Z", expires_at="2020-01-01T00:00:00Z", updated_at="2019-01-01T00:00:00Z")
        audit = _Audit()
        with self.assertRaises(MaintenanceExecutionError):
            self._executor(_Store(expired), _Adapter(), audit).execute(expired.change_id, "task-exec")
        self.assertEqual(audit.events, [])
        nonprepared = _change("environment").transition("applying", task_id="old-task")
        audit = _Audit()
        with self.assertRaises(MaintenanceExecutionError):
            self._executor(_Store(nonprepared), _Adapter(), audit).execute(nonprepared.change_id, "task-exec")
        self.assertEqual(audit.events, [])

    def test_success_audit_exception_and_final_save_failure_stay_verifying(self) -> None:
        """成功审计抛异常或最终保存失败都不触发回滚。"""
        for audit, store in ((_Audit(raise_finish=True), _Store(_change("environment"))), (_Audit(), _Store(_change("environment")))):
            if not audit.raise_finish:
                store.fail_status = "succeeded"
            adapter = _Adapter()
            with self.assertRaises(MaintenanceExecutionError):
                self._executor(store, adapter, audit).execute(store.current.change_id, "task-exec")
            self.assertEqual(store.current.status, "verifying")
            self.assertNotIn("rollback", [kind for kind, _ in adapter.calls])

    def test_rollback_final_save_failure_stays_rolling_back(self) -> None:
        """回滚审计成功但最终保存失败时保持 rolling_back，不进入恢复态。"""
        transaction = _change("environment")
        store = _Store(transaction, fail_status="rolled_back")
        adapter = _Adapter(fail_step="write_environment")
        with self.assertRaises(MaintenanceExecutionError):
            self._executor(store, adapter).execute(transaction.change_id, "task-exec")
        self.assertEqual(store.current.status, "rolling_back")

    def test_failed_write_exception_is_also_rolled_back(self) -> None:
        """写步骤抛异常也视为已尝试，必须调用对应 rollback。"""
        transaction = _change("agent.codex")
        adapter = _Adapter(raise_step="write_mcp")
        result = self._executor(_Store(transaction), adapter).execute(transaction.change_id, "task-exec")
        self.assertEqual(result.status, "rolled_back")
        self.assertEqual([name for kind, name in adapter.calls if kind == "rollback"], ["write_mcp", "write_root_instructions"])

    def test_second_rollback_failure_marks_only_its_leaf(self) -> None:
        """第二个逆序回滚失败时，仅该叶子恢复态，已回滚和未尝试叶子保持原进度。"""
        transaction = _change("agent.codex")
        adapter = _Adapter(fail_step="write_hooks", fail_rollback="write_mcp")
        result = self._executor(_Store(transaction), adapter).execute(transaction.change_id, "task-exec")
        progress = {leaf.leaf_id: leaf.progress for leaf in result.leaf_states}
        self.assertEqual(result.status, "recovery_required")
        self.assertEqual(progress["agent.codex.mcp"], "recovery_required")
        self.assertEqual(progress["agent.codex.root_instructions"], "applied")
        self.assertEqual(progress["agent.codex.hooks"], "rolled_back")

    def test_claim_held_requires_current_thread_owner(self) -> None:
        """无锁或借用其他线程的 held 状态都不能绕过锁内认领门禁。"""
        transaction = _change("environment")
        store = _Store(transaction)
        lock = MaintenanceWriteLock(str(self.workspace))
        coordinator = MaintenanceClaimCoordinator(store, lock)
        with self.assertRaises(MaintenanceClaimError):
            coordinator.claim_held(transaction.change_id, "task-exec")
        lock.acquire()
        try:
            outcome: list[str] = []
            def borrow() -> None:
                try:
                    coordinator.claim_held(transaction.change_id, "task-other")
                except MaintenanceClaimError:
                    outcome.append("rejected")
            thread = threading.Thread(target=borrow)
            thread.start()
            thread.join()
            self.assertEqual(outcome, ["rejected"])
        finally:
            lock.release()


if __name__ == "__main__":
    unittest.main()
