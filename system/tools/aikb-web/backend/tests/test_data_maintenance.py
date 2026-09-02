"""数据维护固定类别、保护边界、陈旧预览和隔离删除验收。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from aikb_web.core.actions import ConfirmationTokenService
from aikb_web.core.maintenance_changes import MaintenanceChange, MaintenanceLeafState
from aikb_web.core.maintenance_lock import MaintenanceWriteLock
from aikb_web.core.maintenance_targets import MAINTENANCE_TARGET_REGISTRY
from aikb_web.core.rule_changes import RuleChangeTransaction
from aikb_web.core.workspace_cleanup import (
    CATEGORY_DEFAULTS,
    WorkspaceCleanupError,
    WorkspaceCleanupScheduler,
    WorkspaceCleanupService,
)
from aikb_web.main import create_app


class _Gateway:
    """API 测试只需要稳定健康摘要；不连接真实知识或审计存储。"""

    settings = None

    def overview(self) -> dict[str, object]:
        return {"index": {"available": False}}


class DataMaintenanceTests(unittest.TestCase):
    """所有 apply 都在系统临时目录中执行，绝不接触真实 workspace。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="aikb-data-maintenance-")
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.now = datetime(2026, 9, 2, tzinfo=timezone.utc)
        self.service = WorkspaceCleanupService(
            self.workspace,
            token_service=ConfirmationTokenService(clock=lambda: self.now.timestamp()),
            write_lock=MaintenanceWriteLock(self.workspace),
            clock=lambda: self.now.timestamp(),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _old(self, path: Path, days: int = 400) -> None:
        timestamp = (self.now - timedelta(days=days)).timestamp()
        os.utime(path, (timestamp, timestamp))

    def _fixtures(self) -> dict[str, Path]:
        old_audit = self.workspace / "audit" / "events" / "2025" / "01" / "2025-01-01.jsonl"
        old_audit.parent.mkdir(parents=True)
        old_audit.write_text('{"status":"succeeded"}\n', encoding="utf-8")
        self._old(old_audit)
        recent_audit = old_audit.parent / "2026-09-02.jsonl"
        recent_audit.write_text('{"status":"succeeded"}\n', encoding="utf-8")

        archived = self.workspace / "archive" / "2025" / "project" / "old-work"
        archived.mkdir(parents=True)
        work = archived / "work.md"
        work.write_text('---\nwork_id: "old-work"\nstatus: "completed"\n---\n', encoding="utf-8")
        self._old(work)
        checkpoint = archived / "checkpoints" / "one.md"
        checkpoint.parent.mkdir()
        checkpoint.write_text("old", encoding="utf-8")
        self._old(checkpoint)

        task = self.workspace / "runtime" / "web" / "tasks" / "2025" / "01" / "task-old"
        task.mkdir(parents=True)
        (task / "snapshot.json").write_text(json.dumps({"status": "succeeded", "updated_at": "2025-01-01T00:00:00+00:00"}), encoding="utf-8")
        (task / "events.jsonl").write_text("{}\n", encoding="utf-8")
        active_task = task.parent / "task-active"
        active_task.mkdir()
        (active_task / "snapshot.json").write_text(json.dumps({"status": "running", "updated_at": "2025-01-01T00:00:00+00:00"}), encoding="utf-8")
        old_time = "2025-01-01T00:00:00Z"
        rule_id = "change-" + "1" * 32
        rule_dir = self.workspace / "runtime" / "web" / "rule-changes" / "2025" / "01" / rule_id
        rule_dir.mkdir(parents=True)
        rule = RuleChangeTransaction(
            change_id=rule_id,
            rule_id="user",
            action_id="rule.user.update",
            risk_level="source_write",
            status="succeeded",
            before_hash="a" * 64,
            after_hash="b" * 64,
            diff_hash="c" * 64,
            preview_digest="d" * 64,
            validator_version="v1",
            repository_revision="e" * 40,
            created_at=old_time,
            expires_at="2025-01-01T01:00:00Z",
            updated_at="2025-01-02T00:00:00Z",
            task_id="task-rule-old",
            rollback_status="not_applicable",
        )
        (rule_dir / "transaction.json").write_text(json.dumps(rule.to_dict()), encoding="utf-8")

        maintenance_id = "maintenance-old"
        maintenance_dir = self.workspace / "runtime" / "web" / "maintenance-transactions" / maintenance_id
        maintenance_dir.mkdir(parents=True)
        target = MAINTENANCE_TARGET_REGISTRY.get("environment")
        maintenance = MaintenanceChange(
            change_id=maintenance_id,
            target_id="environment",
            action_id=target.action_id,
            risk_level=target.risk_level,
            status="succeeded",
            base_fingerprint="a" * 64,
            before_fingerprint="b" * 64,
            after_fingerprint="c" * 64,
            step_summary=target.steps,
            preview_digest="d" * 64,
            created_at=old_time,
            expires_at="2025-01-01T01:00:00Z",
            updated_at="2025-01-02T00:00:00Z",
            task_id="task-maintenance-old",
            rollback_status="not_applicable",
            leaf_states=tuple(
                MaintenanceLeafState(leaf_id, "present", "e" * 64, "f" * 64, "verified")
                for leaf_id in target.logical_leaves
            ),
        )
        (maintenance_dir / "transaction.json").write_text(json.dumps(maintenance.to_dict()), encoding="utf-8")
        return {
            "old_audit": old_audit,
            "recent_audit": recent_audit,
            "archived": archived,
            "task": task,
            "active_task": active_task,
            "rule_transaction": rule_dir,
            "maintenance_transaction": maintenance_dir,
        }

    def test_preview_and_apply_delete_only_expired_terminal_candidates(self) -> None:
        items = self._fixtures()
        preview = self.service.preview(categories=list(CATEGORY_DEFAULTS), retention_days=dict(CATEGORY_DEFAULTS))
        self.assertEqual(preview["candidate_count"], 5)
        self.assertNotIn(str(self.workspace), json.dumps(preview))
        result = self.service.apply(preview["plan_id"], preview["confirmation_token"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["deleted_count"], 5)
        self.assertFalse(items["old_audit"].exists())
        self.assertFalse(items["archived"].exists())
        self.assertFalse(items["task"].exists())
        self.assertFalse(items["rule_transaction"].exists())
        self.assertFalse(items["maintenance_transaction"].exists())
        self.assertTrue(items["recent_audit"].exists())
        self.assertTrue(items["active_task"].exists())

    def test_automatic_cleanup_uses_fixed_defaults_without_confirmation_token(self) -> None:
        """自动周期只使用服务端默认策略，并清理五类安全过期对象。"""

        items = self._fixtures()
        result = self.service.run_automatic()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["deleted_count"], 5)
        self.assertFalse(items["old_audit"].exists())
        self.assertFalse(items["archived"].exists())
        self.assertFalse(items["task"].exists())
        self.assertFalse(items["rule_transaction"].exists())
        self.assertFalse(items["maintenance_transaction"].exists())
        self.assertTrue(items["recent_audit"].exists())
        self.assertTrue(items["active_task"].exists())

    def test_automatic_cleanup_retries_known_terminal_private_materials(self) -> None:
        """终态材料首次清理失败后，周期任务会重试，但仍保留期内摘要。"""

        items = self._fixtures()
        transaction_path = items["maintenance_transaction"] / "transaction.json"
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        transaction["updated_at"] = "2026-09-01T00:00:00Z"
        transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
        private = items["maintenance_transaction"] / "private"
        private.mkdir()
        (private / "manifest.json").write_text("{}", encoding="utf-8")

        result = self.service.run_automatic()

        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(items["maintenance_transaction"].exists())
        self.assertFalse(private.exists())

    def test_transaction_recovery_and_unknown_material_are_never_auto_deleted(self) -> None:
        """恢复态、非终态和含私有目录的事务均保留，不因自动清理扩大删除面。"""

        items = self._fixtures()
        rule_path = items["rule_transaction"] / "transaction.json"
        rule = json.loads(rule_path.read_text(encoding="utf-8"))
        rule.update(status="recovery_required", rollback_status="recovery_required")
        rule_path.write_text(json.dumps(rule), encoding="utf-8")

        maintenance_path = items["maintenance_transaction"] / "transaction.json"
        maintenance = json.loads(maintenance_path.read_text(encoding="utf-8"))
        maintenance.update(status="recovery_required", rollback_status="recovery_required")
        maintenance_path.write_text(json.dumps(maintenance), encoding="utf-8")
        private = items["maintenance_transaction"] / "private"
        private.mkdir()
        (private / "unknown.bin").write_bytes(b"keep")
        result = self.service.run_automatic()
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(items["rule_transaction"].exists())
        self.assertTrue(items["maintenance_transaction"].exists())
        self.assertTrue((private / "unknown.bin").exists())

    def test_automatic_cleanup_skips_when_shared_write_lock_is_busy(self) -> None:
        """自动清理不得等待或绕过规则、维护写入共用的全局锁。"""

        items = self._fixtures()
        competing_lock = MaintenanceWriteLock(self.workspace)
        competing_lock.acquire()
        try:
            result = self.service.run_automatic()
        finally:
            competing_lock.release()
        self.assertEqual(result, {
            "status": "skipped",
            "reason": "maintenance_busy",
            "deleted_count": 0,
            "deleted_bytes": 0,
        })
        self.assertTrue(items["old_audit"].exists())
        self.assertTrue(items["rule_transaction"].exists())
        self.assertTrue(items["maintenance_transaction"].exists())

    def test_changed_candidate_rejects_stale_preview_without_deleting(self) -> None:
        items = self._fixtures()
        preview = self.service.preview(categories=["audit"], retention_days={"audit": 90})
        items["old_audit"].write_text('{"changed":true}\n', encoding="utf-8")
        self._old(items["old_audit"])
        with self.assertRaises(WorkspaceCleanupError) as raised:
            self.service.apply(preview["plan_id"], preview["confirmation_token"])
        self.assertEqual(raised.exception.code, "DATA_MAINTENANCE_STALE_PREVIEW")
        self.assertTrue(items["old_audit"].exists())

    def test_directory_with_reparse_like_entry_is_protected(self) -> None:
        items = self._fixtures()
        unsafe_entry = items["archived"] / "linked-data"
        unsafe_entry.write_text("placeholder", encoding="utf-8")
        module = __import__("aikb_web.core.workspace_cleanup", fromlist=["_is_reparse"])
        original = module._is_reparse
        with patch(
            "aikb_web.core.workspace_cleanup._is_reparse",
            side_effect=lambda path: path.name == unsafe_entry.name or original(path),
        ):
            overview = self.service.overview()
        archived = next(item for item in overview["categories"] if item["id"] == "archived_work")
        self.assertEqual(archived["candidate_count"], 0)
        self.assertGreaterEqual(archived["protected_count"], 1)
        self.assertTrue(unsafe_entry.parent.exists())

    def test_reparse_like_runtime_parent_never_moves_cleanup_boundary(self) -> None:
        """runtime/web 父链疑似 junction 时，所有运行面候选都必须保持原状。"""

        items = self._fixtures()
        module = __import__("aikb_web.core.workspace_cleanup", fromlist=["_is_reparse"])
        original = module._is_reparse
        with patch(
            "aikb_web.core.workspace_cleanup._is_reparse",
            side_effect=lambda path: path.name == "runtime" or original(path),
        ):
            overview = self.service.overview()
        protected = {item["id"]: item["protected_count"] for item in overview["categories"]}
        self.assertGreaterEqual(protected["web_tasks"], 1)
        self.assertGreaterEqual(protected["rule_transactions"], 1)
        self.assertGreaterEqual(protected["maintenance_transactions"], 1)
        self.assertTrue(items["task"].exists())
        self.assertTrue(items["rule_transaction"].exists())
        self.assertTrue(items["maintenance_transaction"].exists())

    def test_api_rejects_paths_and_requires_mutation_header(self) -> None:
        self._fixtures()
        client = TestClient(create_app(_Gateway(), data_maintenance_service=self.service))
        headers = {"Content-Type": "application/json", "X-AIKB-Request": "1", "Host": "localhost:80", "Origin": "http://localhost:80"}
        self.assertEqual(client.get("/api/v1/data-maintenance").status_code, 200)
        no_header = client.post("/api/v1/data-maintenance/preview", json={"categories": ["audit"], "retention_days": {"audit": 90}})
        self.assertEqual(no_header.status_code, 400)
        path_input = client.post(
            "/api/v1/data-maintenance/preview",
            headers=headers,
            json={"categories": ["audit"], "retention_days": {"audit": 90}, "path": str(self.workspace)},
        )
        self.assertEqual(path_input.status_code, 422)
        preview = client.post(
            "/api/v1/data-maintenance/preview",
            headers=headers,
            json={"categories": ["audit"], "retention_days": {"audit": 90}},
        )
        self.assertEqual(preview.status_code, 200)
        data = preview.json()["data"]
        applied = client.post(
            f"/api/v1/data-maintenance/plans/{data['plan_id']}/apply",
            headers=headers,
            json={"confirmation_token": data["confirmation_token"]},
        )
        self.assertEqual(applied.status_code, 200)
        self.assertNotIn(str(self.workspace), applied.text)

    def test_scheduler_delays_first_run_and_stops_cleanly(self) -> None:
        """后台调度不在构造时扫描，启动后周期执行并可由 lifespan 及时停止。"""

        called = threading.Event()

        class Service(WorkspaceCleanupService):
            def run_automatic(inner_self) -> dict[str, object]:
                called.set()
                return {"status": "succeeded"}

        service = Service(self.workspace, write_lock=MaintenanceWriteLock(self.workspace))
        scheduler = WorkspaceCleanupScheduler(service, initial_delay=0.01, interval=60)
        self.assertFalse(called.is_set())
        scheduler.start()
        self.assertTrue(called.wait(1))
        scheduler.stop()

    def test_scheduler_stop_is_bounded_when_a_scan_does_not_return(self) -> None:
        """底层文件系统卡住时，后台线程不能无限阻塞应用退出。"""

        entered = threading.Event()
        release = threading.Event()

        class Service(WorkspaceCleanupService):
            def run_automatic(inner_self) -> dict[str, object]:
                entered.set()
                release.wait(1)
                return {"status": "succeeded"}

        service = Service(self.workspace, write_lock=MaintenanceWriteLock(self.workspace))
        scheduler = WorkspaceCleanupScheduler(service, initial_delay=0, interval=60, stop_timeout=0.01)
        scheduler.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        scheduler.stop()
        self.assertLess(time.monotonic() - started, 0.5)
        release.set()
        scheduler.stop()

    def test_app_lifespan_starts_and_stops_cleanup_scheduler(self) -> None:
        """应用只在 lifespan 内启动调度，并在退出前停止后台线程。"""

        class Scheduler:
            def __init__(inner_self) -> None:
                inner_self.started = 0
                inner_self.stopped = 0

            def start(inner_self) -> None:
                inner_self.started += 1

            def stop(inner_self) -> None:
                inner_self.stopped += 1

        scheduler = Scheduler()
        app = create_app(
            _Gateway(),
            data_maintenance_service=self.service,
            data_maintenance_scheduler=scheduler,
        )
        self.assertEqual(scheduler.started, 0)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/v1/data-maintenance").status_code, 200)
            self.assertEqual(scheduler.started, 1)
            self.assertEqual(scheduler.stopped, 0)
        self.assertEqual(scheduler.stopped, 1)

    def test_app_does_not_start_cleanup_when_maintenance_recovery_is_blocked(self) -> None:
        """启动恢复失败时保持清理停用，避免与未收敛事务并行。"""

        class Recovery:
            def recover_all(inner_self) -> None:
                raise RuntimeError("recovery failed")

        class Scheduler:
            def __init__(inner_self) -> None:
                inner_self.started = 0
                inner_self.stopped = 0

            def start(inner_self) -> None:
                inner_self.started += 1

            def stop(inner_self) -> None:
                inner_self.stopped += 1

        scheduler = Scheduler()
        app = create_app(
            _Gateway(),
            maintenance_startup_recovery=Recovery(),
            data_maintenance_service=self.service,
            data_maintenance_scheduler=scheduler,
        )
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/v1/data-maintenance").status_code, 200)
        self.assertEqual(scheduler.started, 0)
        self.assertEqual(scheduler.stopped, 1)


if __name__ == "__main__":
    unittest.main()
