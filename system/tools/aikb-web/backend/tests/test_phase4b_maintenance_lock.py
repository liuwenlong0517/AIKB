"""阶段 4B 波次 2 批次 1：全局维护锁和原子认领专项测试。"""

from __future__ import annotations

import multiprocessing
import tempfile
import threading
import unittest
from typing import Any
from pathlib import Path

from aikb_web.core.maintenance_changes import MaintenanceChange, MaintenanceLeafState
from aikb_web.core.maintenance_lock import (
    MaintenanceClaimCoordinator,
    MaintenanceClaimError,
    MaintenanceLockError,
    MaintenanceWriteLock,
)
from aikb_web.core.rule_transaction import RuleTransactionError, RuleTransactionExecutor


def _prepared_change() -> MaintenanceChange:
    """构造不接触真实配置的最小 prepared 事务。"""
    return MaintenanceChange(
        change_id="maintenance-change-001",
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
        leaf_states=(
            MaintenanceLeafState("user_environment.aikb_home", "present", "d" * 64, "e" * 64),
            MaintenanceLeafState("user_environment.aikb_knowledge_home", "present", "d" * 64, "e" * 64),
        ),
    )


class _MemoryStore:
    """线程安全测试存储；生产实现由后续事务层注入。"""

    def __init__(self, change: MaintenanceChange) -> None:
        self.change = change
        self.guard = threading.Lock()

    def load(self, change_id: str) -> MaintenanceChange | None:
        with self.guard:
            return self.change if self.change.change_id == change_id else None

    def save(self, change: MaintenanceChange) -> None:
        with self.guard:
            self.change = change


class _FailingStore(_MemoryStore):
    """模拟持久化失败，验证认领异常不会遗留全局锁。"""

    def save(self, change: MaintenanceChange) -> None:
        raise OSError("模拟存储故障")


def _hold_lock(root: str, ready: Any, release: Any) -> None:
    """独立进程持锁，供跨进程非阻塞语义测试使用。"""
    lock = MaintenanceWriteLock(root)
    with lock.held():
        ready.set()
        release.wait(5)


class MaintenanceLockTests(unittest.TestCase):
    """验证锁释放、跨实例互斥和 OS 崩溃回收边界。"""

    def test_two_instances_share_thread_lock_and_exception_releases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = MaintenanceWriteLock(directory)
            second = MaintenanceWriteLock(directory)
            with first.held():
                with self.assertRaises(MaintenanceLockError):
                    second.acquire()
            second.acquire()
            second.release()
            with self.assertRaises(RuntimeError):
                with first.held():
                    raise RuntimeError("业务失败")
            second.acquire()
            second.release()
            self.assertEqual(first.lock_name, "aikb-maintenance-write")

    def test_timeout_and_cancellation_fail_without_leaking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = MaintenanceWriteLock(directory)
            second = MaintenanceWriteLock(directory)
            event = threading.Event()
            with first.held():
                with self.assertRaises(MaintenanceLockError):
                    second.acquire(timeout=0.02)
                event.set()
                with self.assertRaises(MaintenanceLockError):
                    second.acquire(timeout=1, cancel_event=event)
            second.acquire()
            second.release()

    def test_two_independent_processes_observe_nonblocking_lock(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            ready = context.Event()
            release = context.Event()
            process = context.Process(target=_hold_lock, args=(directory, ready, release))
            process.start()
            self.assertTrue(ready.wait(5))
            contender = MaintenanceWriteLock(directory)
            with self.assertRaises(MaintenanceLockError):
                contender.acquire()
            release.set()
            process.join(5)
            self.assertEqual(process.exitcode, 0)
            contender.acquire()
            contender.release()

    def test_process_exit_releases_os_lock(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            ready = context.Event()
            release = context.Event()
            process = context.Process(target=_hold_lock, args=(directory, ready, release))
            process.start()
            self.assertTrue(ready.wait(5))
            process.terminate()
            process.join(5)
            contender = MaintenanceWriteLock(directory)
            contender.acquire()
            contender.release()

    def test_rule_executor_and_maintenance_share_same_semantic_lock(self) -> None:
        """通过规则执行器的实际临界区验证两类写入不能交错。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime" / "web"
            runtime.mkdir(parents=True)

            class _Store:
                def _runtime_root(self) -> Path:
                    return runtime / "rule-changes"

            executor = object.__new__(RuleTransactionExecutor)
            executor._store = _Store()
            executor._process_lock = MaintenanceWriteLock(root)
            maintenance = MaintenanceWriteLock(root)
            with executor._acquire_repository():
                with self.assertRaises(MaintenanceLockError):
                    maintenance.acquire()
            with maintenance.held():
                with self.assertRaises(RuleTransactionError):
                    with executor._acquire_repository():
                        pass


class MaintenanceClaimTests(unittest.TestCase):
    """验证同一 prepared 事务只有一个 task 可以进入 applying。"""

    def test_two_coordinators_only_one_claim_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _MemoryStore(_prepared_change())
            first = MaintenanceClaimCoordinator(store, MaintenanceWriteLock(directory))
            second = MaintenanceClaimCoordinator(store, MaintenanceWriteLock(directory))
            results: list[str] = []
            guard = threading.Lock()

            def claim(coordinator: MaintenanceClaimCoordinator, task_id: str) -> None:
                try:
                    change = coordinator.claim("maintenance-change-001", task_id)
                    value = change.status + ":" + (change.task_id or "")
                except (MaintenanceClaimError, MaintenanceLockError):
                    value = "rejected"
                with guard:
                    results.append(value)

            threads = [
                threading.Thread(target=claim, args=(first, "task-a")),
                threading.Thread(target=claim, args=(second, "task-b")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sum(item.startswith("applying:") for item in results), 1)
            self.assertEqual(results.count("rejected"), 1)
            self.assertEqual(store.change.status, "applying")

    def test_invalid_owner_and_task_ids_are_rejected_before_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _MemoryStore(_prepared_change())
            coordinator = MaintenanceClaimCoordinator(store, MaintenanceWriteLock(directory))
            for kwargs in (
                {"owner_id": "../private"},
                {"owner_id": "C:\\private"},
            ):
                with self.assertRaises(MaintenanceClaimError):
                    coordinator.claim("maintenance-change-001", "task-a", **kwargs)
            with self.assertRaises(MaintenanceClaimError):
                coordinator.claim("../private", "task-a")
            with self.assertRaises(MaintenanceClaimError):
                coordinator.claim("maintenance-change-001", "C:\\private\\task")
            self.assertEqual(store.change.status, "prepared")

    def test_store_failure_releases_global_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            failing = _FailingStore(_prepared_change())
            with self.assertRaises(MaintenanceClaimError):
                MaintenanceClaimCoordinator(
                    failing, MaintenanceWriteLock(directory)
                ).claim("maintenance-change-001", "task-a")
            lock = MaintenanceWriteLock(directory)
            lock.acquire()
            lock.release()


if __name__ == "__main__":
    unittest.main()
