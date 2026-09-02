"""数据维护固定类别、保护边界、陈旧预览和隔离删除验收。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from aikb_web.core.actions import ConfirmationTokenService
from aikb_web.core.maintenance_lock import MaintenanceWriteLock
from aikb_web.core.workspace_cleanup import WorkspaceCleanupError, WorkspaceCleanupService
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
        return {"old_audit": old_audit, "recent_audit": recent_audit, "archived": archived, "task": task, "active_task": active_task}

    def test_preview_and_apply_delete_only_expired_terminal_candidates(self) -> None:
        items = self._fixtures()
        preview = self.service.preview(categories=["audit", "archived_work", "web_tasks"], retention_days={"audit": 90, "archived_work": 180, "web_tasks": 30})
        self.assertEqual(preview["candidate_count"], 3)
        self.assertNotIn(str(self.workspace), json.dumps(preview))
        result = self.service.apply(preview["plan_id"], preview["confirmation_token"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["deleted_count"], 3)
        self.assertFalse(items["old_audit"].exists())
        self.assertFalse(items["archived"].exists())
        self.assertFalse(items["task"].exists())
        self.assertTrue(items["recent_audit"].exists())
        self.assertTrue(items["active_task"].exists())

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


if __name__ == "__main__":
    unittest.main()
