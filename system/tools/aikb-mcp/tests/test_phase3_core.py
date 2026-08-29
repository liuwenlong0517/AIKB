"""阶段 3 共享索引只读检查与审计 v3 回归测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from aikb.audit import AUDIT_SCHEMA_VERSION, FINISHED_STATUS, AuditStore, combine_invocations, web_audit_item, web_audit_query
from aikb.config import Settings
from aikb.frontmatter import render_frontmatter
from aikb.indexer import inspect_knowledge_index, rebuild_knowledge_index


class Phase3Fixture:
    """创建最小知识仓和审计运行面，隔离真实控制仓文件。"""

    def __init__(self) -> None:
        """准备带一个合法 verified 文档的知识根。"""
        # 路径故意含空格和中文，覆盖 Windows SQLite URI 的编码边界。
        self.temp = tempfile.TemporaryDirectory(prefix="aikb phase3 中文-")
        self.root = Path(self.temp.name)
        (self.root / "ENTRY_RULES.md").write_text("# fixture\n", encoding="utf-8")
        (self.root / "system").mkdir()
        content = self.root / "content"
        (content / "knowledge").mkdir(parents=True)
        (content / ".aikb-knowledge.json").write_text(json.dumps({"kind": "aikb-knowledge", "contract_version": 1}), encoding="utf-8")
        metadata = {
            "id": "aikb:knowledge:phase3:item", "type": "knowledge", "status": "verified", "tags": [], "relations": [],
            "applicable_versions": "not-version-specific", "last_verified": "2026-08-29", "review_when": "never",
        }
        (content / "knowledge" / "item.md").write_text(render_frontmatter(metadata) + "\n\n# 阶段 3\n\n正文。\n", encoding="utf-8")
        self.settings = Settings.load(self.root, self.root / "workspace")

    def close(self) -> None:
        """清理隔离运行面。"""
        self.temp.cleanup()


class InspectIndexTests(unittest.TestCase):
    """确认索引检查不产生任何派生写入。"""

    def setUp(self) -> None:
        """建立独立夹具。"""
        self.fixture = Phase3Fixture()

    def tearDown(self) -> None:
        """释放夹具。"""
        self.fixture.close()

    def test_missing_ready_stale_and_damaged_are_read_only(self) -> None:
        """依次识别缺失、可用、过期和损坏，且检查过程不替换数据库。"""
        settings = self.fixture.settings
        self.assertEqual(inspect_knowledge_index(settings)["status"], "missing")
        rebuild_knowledge_index(settings)
        database = settings.knowledge_db
        ready_bytes = database.read_bytes()
        sidecars = [database.with_name(database.name + suffix) for suffix in ("-journal", "-wal", "-shm")]
        self.assertEqual(inspect_knowledge_index(settings)["status"], "ready")
        self.assertEqual(ready_bytes, database.read_bytes())
        self.assertTrue(all(not sidecar.exists() for sidecar in sidecars))
        source = settings.content_root / "knowledge" / "item.md"
        source.write_text(source.read_text(encoding="utf-8") + "\n新增。\n", encoding="utf-8")
        self.assertEqual(inspect_knowledge_index(settings)["status"], "stale")
        self.assertEqual(ready_bytes, database.read_bytes())
        self.assertTrue(all(not sidecar.exists() for sidecar in sidecars))
        database.write_bytes(b"not sqlite")
        self.assertEqual(inspect_knowledge_index(settings)["status"], "damaged")
        self.assertEqual(database.read_bytes(), b"not sqlite")
        self.assertTrue(all(not sidecar.exists() for sidecar in sidecars))


class AuditV3Tests(unittest.TestCase):
    """验证 v3 有限任务关联字段、新状态和旧版本投影兼容。"""

    def setUp(self) -> None:
        """准备审计存储。"""
        self.fixture = Phase3Fixture()
        self.store = AuditStore(self.fixture.settings)

    def tearDown(self) -> None:
        """释放审计夹具。"""
        self.fixture.close()

    def test_task_fields_and_terminal_statuses_are_safe(self) -> None:
        """开始/完成 API 写入有限任务字段，Web 投影保留新增状态但不带原始对象。"""
        invocation = self.store.start(
            source="web", agent="codex", operation="task.run", task_id="task-1", action_id="action-1", target_task_id="target-1",
        )
        self.store.finish(
            invocation, source="web", agent="codex", operation="task.run", status="timed_out", outcome_code="deadline",
            task_id="task-1", action_id="action-1", target_task_id="target-1",
        )
        loaded = self.store.read_events()
        combined = combine_invocations(loaded["events"])
        projected = web_audit_query(self.store, source="web", status="timed_out")["items"]
        self.assertEqual(AUDIT_SCHEMA_VERSION, 3)
        self.assertTrue({"cancelled", "timed_out", "interrupted"}.issubset(FINISHED_STATUS))
        self.assertEqual(projected[0]["status"], "timed_out")
        self.assertEqual(projected[0]["task_id"], "task-1")
        self.assertEqual(projected[0]["action_id"], "action-1")
        self.assertEqual(projected[0]["target_task_id"], "target-1")
        self.assertEqual(combined[0]["task_id"], "task-1")
        self.assertNotIn("action", projected[0])

    def test_v1_v2_records_remain_valid_public_models(self) -> None:
        """旧版本事件缺少 v3 字段时仍可安全读取，新增字段保持 null。"""
        for version in (1, 2):
            projected = web_audit_item({"schema_version": version, "event_id": f"event-v{version}", "status": "cancelled"})
            self.assertEqual(projected["schema_version"], version)
            self.assertIsNone(projected["task_id"])
            self.assertIsNone(projected["action_id"])
            self.assertIsNone(projected["target_task_id"])

    def test_invalid_source_is_safely_normalized_to_schema_value(self) -> None:
        """非法来源采用固定安全降级，审计 JSONL 始终只出现契约允许的来源。"""
        self.store.write({
            "record_type": "wrapper_failure", "source": "unexpected-agent", "agent": "codex",
            "operation": "task.run", "status": "failed", "outcome_code": "wrapper_error",
        })
        loaded = self.store.read_events()["events"]
        self.assertEqual(loaded[-1]["source"], "web")
        self.assertTrue(all(item.get("source") in {"mcp", "hook", "web"} for item in loaded))


if __name__ == "__main__":
    unittest.main()
