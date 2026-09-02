"""Working State 归档 Web 只读模型的最小边界测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aikb.config import Settings
from aikb.workstate import WorkStateStore


class RuntimeHistoryStoreTests(unittest.TestCase):
    """验证归档列表、详情和检查点读取不会混入活动任务或泄漏路径。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="aikb-runtime-history-")
        root = Path(self.temp.name)
        project = root / "project"
        project.mkdir()
        workspace = root / "workspace"
        settings = Settings(
            repo_root=project,
            knowledge_root=project,
            content_root=project,
            workspace_root=workspace,
            knowledge_db=workspace / "db" / "knowledge.db",
            work_db=workspace / "db" / "work.db",
        )
        self.store = WorkStateStore(settings)
        self.project = project

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create_archived(self, status: str = "completed") -> str:
        created = self.store.checkpoint({
            "project_path": str(self.project), "goal": "历史任务目标",
            "current_state": "已完成", "agent": "codex", "session_id": "history-session-123",
        })
        work_id = created["work_id"]
        self.store.close(work_id, status=status, agent="codex", session_id="history-session-123", note="终态")
        return work_id

    def test_archived_model_is_separate_and_safe(self) -> None:
        work_id = self._create_archived()
        active = self.store.checkpoint({
            "project_path": str(self.project), "work_id": "active-task", "goal": "仍在运行",
            "agent": "codex", "session_id": "active-session-123",
        })
        history = self.store.web_archived_work_states(page=1, page_size=20)
        self.assertEqual([item["work_id"] for item in history["items"]], [work_id])
        self.assertEqual(history["items"][0]["lifecycle"], "archived")
        self.assertEqual(self.store.web_active_work_states(page=1, page_size=20)["items"][0]["work_id"], active["work_id"])

        detail = self.store.web_archived_work_state(work_id)["item"]
        serialized = json.dumps(detail, ensure_ascii=False)
        self.assertEqual(detail["status"], "completed")
        self.assertEqual(detail["author_agent"], "codex")
        self.assertNotIn(str(self.store.settings.workspace_root), serialized)
        self.assertNotIn("work.md", serialized)

        checkpoints = self.store.web_archived_checkpoints(work_id, page=1, page_size=20)
        self.assertGreaterEqual(checkpoints["pagination"]["total"], 2)
        checkpoint_id = checkpoints["items"][0]["checkpoint_id"]
        checkpoint = self.store.web_archived_checkpoint(work_id, checkpoint_id)
        self.assertEqual(checkpoint["lifecycle"], "archived")
        self.assertNotIn(str(self.store.settings.workspace_root), json.dumps(checkpoint, ensure_ascii=False))

    def test_web_history_uses_index_without_workspace_fingerprint_or_recursive_lookup(self) -> None:
        work_id = self._create_archived()
        with (
            patch.object(self.store, "_work_fingerprint", side_effect=AssertionError("unexpected fingerprint")),
            patch.object(Path, "rglob", side_effect=AssertionError("unexpected recursive scan")),
        ):
            history = self.store.web_archived_work_states(page=1, page_size=1)
            checkpoints = self.store.web_archived_checkpoints(work_id, page=1, page_size=1)
        self.assertEqual(history["items"][0]["work_id"], work_id)
        self.assertEqual(checkpoints["work_id"], work_id)

    def test_archived_filter_rejects_open_states(self) -> None:
        with self.assertRaises(ValueError):
            self.store.web_archived_work_states(status="active")


if __name__ == "__main__":
    unittest.main()
