"""阶段 4B 维护审计字段、兼容和安全投影的独立契约测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from aikb.audit import AuditStore, combine_invocations, describe_action, describe_result, web_audit_item
from aikb.config import Settings


class Phase4BAuditFixture:
    """提供不接触真实用户配置的最小审计运行面。"""

    def __init__(self) -> None:
        """创建合法知识仓契约和隔离 workspace。"""
        self.temp = tempfile.TemporaryDirectory(prefix="aikb phase4b audit-")
        self.root = Path(self.temp.name)
        (self.root / "ENTRY_RULES.md").write_text("# fixture\n", encoding="utf-8")
        (self.root / "system").mkdir()
        content = self.root / "content"
        content.mkdir()
        (content / ".aikb-knowledge.json").write_text(
            json.dumps({"kind": "aikb-knowledge", "contract_version": 1}), encoding="utf-8"
        )
        self.settings = Settings.load(self.root, self.root / "workspace")

    def close(self) -> None:
        """释放隔离目录。"""
        self.temp.cleanup()


class Phase4BAuditContractTests(unittest.TestCase):
    """验证维护审计扩展不破坏旧记录，且不形成敏感材料通道。"""

    def setUp(self) -> None:
        """建立独立审计存储。"""
        self.fixture = Phase4BAuditFixture()
        self.store = AuditStore(self.fixture.settings)

    def tearDown(self) -> None:
        """清理测试夹具。"""
        self.fixture.close()

    def test_legacy_v1_v4_records_keep_new_fields_null(self) -> None:
        """旧记录没有维护扩展字段时仍可投影，且不伪造目标或指纹。"""
        for version in (1, 2, 3, 4):
            item = web_audit_item({"schema_version": version, "event_id": f"legacy-{version}", "status": "succeeded"})
            self.assertEqual(item["schema_version"], version)
            self.assertIsNone(item["maintenance_target_id"])
            self.assertIsNone(item["before_fingerprint"])
            self.assertIsNone(item["after_fingerprint"])
            self.assertIsNone(item["restart_required"])

    def test_valid_maintenance_fields_round_trip_and_web_projection(self) -> None:
        """合法静态目标、指纹和 bool 可随开始/完成事件合并并安全公开。"""
        before = "A" * 64
        after = "b" * 64
        invocation = self.store.start(
            source="web", agent="codex", operation="maintenance.apply",
            action={"step": "write_mcp", "path": r"C:\private\config.toml", "config": "secret"},
            action_id="maintenance.agent.codex.repair",
            change_id="change-4b-1", maintenance_target_id="agent.codex",
            before_fingerprint=before, restart_required="true",  # type: ignore[arg-type]
        )
        self.store.finish(
            invocation, source="web", agent="codex", operation="maintenance.apply", status="succeeded",
            outcome_code="ok", result_summary={"path": r"C:\private\config.toml", "secret": "token"},
            change_id="change-4b-1",
            maintenance_target_id="agent.codex", before_fingerprint=before, after_fingerprint=after,
            rollback_status="not_applicable", restart_required=True,
        )
        events = self.store.read_events()["events"]
        combined = combine_invocations(events)
        item = combined[-1]
        self.assertEqual(item["maintenance_target_id"], "agent.codex")
        self.assertEqual(item["before_fingerprint"], before.lower())
        self.assertEqual(item["after_fingerprint"], after)
        self.assertEqual(item["rollback_status"], "not_applicable")
        self.assertEqual(item["action_id"], "maintenance.agent.codex.repair")
        self.assertTrue(item["restart_required"])
        self.assertIn("维护", item["action_text"])
        self.assertNotIn("生命周期", item["action_text"])
        self.assertEqual(item["result_text"], "维护已完成")
        self.assertEqual(item["action"], {"step": "write_mcp"})
        self.assertIsNone(item["result_summary"])
        projected = web_audit_item(item)
        self.assertEqual(projected["maintenance_target_id"], "agent.codex")
        self.assertEqual(projected["before_fingerprint"], before.lower())
        self.assertEqual(projected["after_fingerprint"], after)
        self.assertEqual(projected["rollback_status"], "not_applicable")
        self.assertTrue(projected["restart_required"])
        self.assertNotIn("action", projected)
        serialized = json.dumps(events + [projected], ensure_ascii=False)
        for secret in ("private", "config.toml", "secret", "token", "pwsh"):
            self.assertNotIn(secret, serialized)

    def test_invalid_maintenance_values_are_dropped_without_material_leak(self) -> None:
        """非法目标、摘要、bool 和维护对象只降级为 null/固定值，不写入敏感正文。"""
        self.store.write({
            "record_type": "invocation_finished", "source": "web", "agent": "codex",
            "operation": r"maintenance.apply:C:\private\config.toml", "status": "failed",
            "action_id": r"C:\private\action-id",
            "outcome_code": r"C:\private\failure", "action": {
                "step": "write_mcp", "path": r"C:\private\config.toml", "command": "secret command",
                "authorization": "Bearer super-secret",
            },
            "result_summary": {"environment_value": "AIKB_SECRET=top-secret", "backup": "private backup"},
            "maintenance_target_id": r"C:\private\target", "change_id": r"C:\private\change",
            "before_fingerprint": "not-a-hash", "after_fingerprint": "f" * 63,
            "rollback_status": "success", "restart_required": "false",
        })
        event = self.store.read_events()["events"][-1]
        self.assertEqual(event["operation"], "maintenance")
        for field in (
            "maintenance_target_id", "before_fingerprint", "after_fingerprint", "change_id",
            "rollback_status", "restart_required", "action", "result_summary",
        ):
            self.assertIsNone(event[field])
        self.assertIsNone(event["action_id"])
        serialized = json.dumps(event, ensure_ascii=False)
        for secret in ("C:\\private", "config.toml", "super-secret", "top-secret", "backup", "secret command"):
            self.assertNotIn(secret, serialized)

    def test_schema_declares_strict_maintenance_fields_and_legacy_versions(self) -> None:
        """Schema 固定三项目标、SHA-256 指纹和 JSON bool，并继续接受 v1-v4。"""
        schema_path = TOOL_ROOT.parents[1] / "schemas" / "audit-event.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        properties = schema["properties"]
        self.assertEqual(properties["maintenance_target_id"]["enum"], ["environment", "agent.codex", "agent.claude-code", None])
        for field in ("before_fingerprint", "after_fingerprint"):
            self.assertEqual(properties[field]["type"], ["string", "null"])
            self.assertEqual(properties[field]["pattern"], "^[0-9a-f]{64}$")
        self.assertEqual(properties["restart_required"]["type"], ["boolean", "null"])
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(properties["schema_version"]["enum"], [1, 2, 3, 4])

    def test_maintenance_descriptions_use_only_fixed_labels(self) -> None:
        """维护说明只来自固定 operation/step/status，不拼接路径或 outcome 原文。"""
        action_text = describe_action(
            "maintenance.agent.codex.repair",
            {"step": "write_mcp", "path": r"C:\private\config.toml"},
        )
        self.assertEqual(action_text, "修复 Codex 安装：写入 MCP")
        self.assertEqual(
            describe_result("maintenance.apply", "failed", r"C:\private\failure", {"secret": "value"}),
            "维护失败",
        )
        self.assertNotIn("private", action_text)

    def test_web_projection_drops_invalid_target_hash_and_boolean(self) -> None:
        """读取历史脏记录时，Web 投影仍只公开静态目标、合法摘要和真 bool。"""
        projected = web_audit_item({
            "schema_version": 4, "operation": "maintenance.preview",
            "maintenance_target_id": "agent.unknown", "before_fingerprint": "x",
            "after_fingerprint": "f" * 64, "restart_required": 1,
            "path": r"C:\private\config", "action": {"config": "secret"},
        })
        self.assertIsNone(projected["maintenance_target_id"])
        self.assertIsNone(projected["before_fingerprint"])
        self.assertIsNone(projected["after_fingerprint"])
        self.assertIsNone(projected["restart_required"])
        self.assertNotIn("path", projected)
        self.assertNotIn("action", projected)

    def test_maintenance_action_id_is_a_static_enum(self) -> None:
        """维护 action_id 只接受三个静态动作，任意文本和路径均不得落盘。"""
        valid = web_audit_item({
            "schema_version": 4, "operation": "maintenance.preview",
            "maintenance_target_id": "environment", "action_id": "maintenance.environment.update",
        })
        self.assertEqual(valid["action_id"], "maintenance.environment.update")
        for value in (r"C:\private\action", "user-provided-action", {"action": "secret"}):
            invalid = web_audit_item({
                "schema_version": 4, "operation": "maintenance.preview",
                "maintenance_target_id": "environment", "action_id": value,
            })
            self.assertIsNone(invalid["action_id"])


if __name__ == "__main__":
    unittest.main()
