"""审计 Web 安全读模型的独立回归测试。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from aikb.audit import _redact_web_text, web_audit_detail, web_audit_item, web_audit_query, web_audit_summary


class FakeAuditStore:
    """提供包含 v1/v2、incomplete、fallback 和损坏记录标志的只读审计替身。"""

    def __init__(self) -> None:
        self.loaded = {
            "events": [
                {
                    "schema_version": 1,
                    "record_type": "invocation_started",
                    "event_id": "start-v1",
                    "invocation_id": "invoke-incomplete",
                    "timestamp": "2026-08-29T09:00:00+08:00",
                    "source": "hook",
                    "agent": "codex",
                    "operation": "checkpoint_work_state",
                    "action": {"prompt": "raw prompt must not be returned"},
                },
                {
                    "schema_version": 2,
                    "record_type": "invocation_started",
                    "event_id": "start-v2",
                    "invocation_id": "invoke-finished",
                    "timestamp": "2026-08-29T09:01:00+08:00",
                    "source": "mcp",
                    "agent": "codex",
                    "session_id": "session-1",
                    "session_label": "Codex · 会话 001",
                    "project_id": "project-safe-123",
                    "operation": "search_knowledge",
                    "action": {"query": "secret raw query", "authorization": "Bearer top-secret"},
                },
                {
                    "schema_version": 2,
                    "record_type": "invocation_finished",
                    "event_id": "finish-v2",
                    "invocation_id": "invoke-finished",
                    "timestamp": "2026-08-29T09:01:01+08:00",
                    "source": "mcp",
                    "agent": "codex",
                    "operation": "search_knowledge",
                    "status": "succeeded",
                    "outcome_code": "results_returned",
                    "result_summary": {"count": 1, "raw_result": "must not be returned"},
                    "result_text": "检索完成，返回 1 条结果",
                    "duration_ms": 1000,
                    "capture_level": "diagnostic",
                    "_fallback": True,
                },
            ],
            "damaged": [r"C:\private\workspace\audit\events\2026-08-29.jsonl:88"],
            "fallback_count": 1,
        }

    def read_events(self) -> dict[str, object]:
        """返回稳定替身数据；查询实现不得修改事实源。"""
        return self.loaded


class WebAuditModelTests(unittest.TestCase):
    """验证 Web 查询仅公开有限字段和有界分页。"""

    def setUp(self) -> None:
        """建立每个测试独立的审计替身。"""
        self.store = FakeAuditStore()

    def test_query_supports_legacy_incomplete_fallback_and_damaged_count(self) -> None:
        """确认 v1 缺少结束事件时显示 incomplete，损坏信息只保留计数。"""
        payload = web_audit_query(self.store, page=1, page_size=1)
        self.assertEqual(payload["pagination"], {
            "page": 1, "page_size": 1, "total": 2, "total_pages": 2,
            "has_next": True, "has_previous": False,
        })
        incomplete = web_audit_query(self.store, source="hook", page_size=10)["items"][0]
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertIsNone(incomplete["session_id"])
        self.assertIsNone(incomplete["session_label"])
        self.assertTrue(payload["summary"]["has_damaged"])
        self.assertEqual(payload["summary"]["damaged_count"], 1)
        self.assertNotIn("damaged", payload["summary"])
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("private", serialized)
        self.assertNotIn("raw prompt", serialized)
        self.assertNotIn("raw_result", serialized)
        self.assertNotIn("authorization", serialized)

    def test_query_orders_latest_activity_first(self) -> None:
        """分页前按最新活动倒序排列，第一页不会固定落在最旧历史记录。"""
        payload = web_audit_query(self.store, page=1, page_size=1)
        self.assertEqual(payload["items"][0]["invocation_id"], "invoke-finished")

    def test_query_accepts_multiple_statuses_before_paging(self) -> None:
        """多状态在共享核心一次筛选，分页总数不会受 Web 层分批合并上限影响。"""
        payload = web_audit_query(self.store, status=["succeeded", "incomplete"], page_size=10)
        self.assertEqual(payload["pagination"]["total"], 2)

    def test_common_filters_and_summary_reuse_same_safe_projection(self) -> None:
        """确认来源、状态、操作和会话筛选，以及 fallback 计数均保持安全。"""
        payload = web_audit_query(
            self.store, source="mcp", status="succeeded", operation="search_knowledge",
            session_label="Codex · 会话 001", page_size=10,
        )
        self.assertEqual(payload["pagination"]["total"], 1)
        item = payload["items"][0]
        self.assertTrue(item["fallback"])
        self.assertEqual(item["capture_level"], "diagnostic")
        self.assertEqual(web_audit_summary(self.store, source="mcp")["fallback_records"], 1)
        self.assertEqual(web_audit_summary(self.store, source="hook")["fallback_records"], 0)
        self.assertNotIn("result_summary", item)
        self.assertNotIn("action", item)
        self.assertNotIn("client", item)

    def test_detail_matches_event_or_invocation_and_never_returns_raw_fields(self) -> None:
        """确认详情支持两个安全标识，并拒绝未知标识。"""
        item = web_audit_detail(self.store, "invoke-finished")
        self.assertIsNotNone(item)
        # 合并调用以开始事件作为稳定主标识；结束事件 ID 仍可用于查找。
        self.assertEqual(item["event_id"], "start-v2")
        self.assertIsNotNone(web_audit_detail(self.store, "finish-v2"))
        self.assertIsNone(web_audit_detail(self.store, "not-found"))
        self.assertNotIn("result_summary", item)
        self.assertNotIn("action", item)

    def test_page_bounds_and_item_projection_are_strict(self) -> None:
        """确认超界分页被拒绝，投影忽略未来字段和内部路径。"""
        with self.assertRaises(ValueError):
            web_audit_query(self.store, page_size=101)
        with self.assertRaises(ValueError):
            web_audit_query(self.store, page=0)
        projected = web_audit_item({
            "event_id": "safe-id", "operation": "x", "status": "failed",
            "traceback": "secret traceback", "diagnostic": {"payload": "raw"},
            "path": r"C:\private\path", "result": "raw result",
        })
        self.assertEqual(projected["event_id"], "safe-id")
        self.assertNotIn("traceback", projected)
        self.assertNotIn("diagnostic", projected)
        self.assertNotIn("path", projected)
        self.assertNotIn("result", projected)

    def test_v4_fields_are_projected_and_invalid_values_are_dropped(self) -> None:
        """Web 只投影合法规则关联字段与摘要，正文、差异和路径即使混入旧记录也不泄漏。"""
        valid = web_audit_item({
            "schema_version": 4, "record_type": "invocation_finished", "event_id": "event-v4",
            "invocation_id": "invoke-v4", "status": "succeeded", "resource_type": "RULE",
            "resource_id": "USER", "change_id": "change-v4", "before_hash": "A" * 64,
            "after_hash": "b" * 64, "rollback_status": "RECOVERY_REQUIRED",
            "candidate": "rule body must not pass", "diff": "full diff must not pass",
            "path": r"C:\private\USER_RULES.md",
        })
        self.assertEqual(valid["schema_version"], 4)
        self.assertEqual(valid["resource_type"], "rule")
        self.assertEqual(valid["resource_id"], "user")
        self.assertEqual(valid["change_id"], "change-v4")
        self.assertEqual(valid["before_hash"], "a" * 64)
        self.assertEqual(valid["after_hash"], "b" * 64)
        self.assertEqual(valid["rollback_status"], "recovery_required")
        serialized = json.dumps(valid, ensure_ascii=False)
        self.assertNotIn("rule body", serialized)
        self.assertNotIn("full diff", serialized)
        self.assertNotIn("USER_RULES", serialized)

        invalid = web_audit_item({
            "schema_version": 4, "event_id": "event-invalid", "resource_type": "file",
            "resource_id": "secret", "change_id": r"C:\private\change", "before_hash": "x",
            "after_hash": "f" * 63, "rollback_status": "success",
        })
        for field in ("change_id", "resource_type", "resource_id", "before_hash", "after_hash", "rollback_status"):
            self.assertIsNone(invalid[field])

    def test_v4_change_filter_and_legacy_records(self) -> None:
        """变更筛选只命中 v4 规则调用，旧 v1/v2 记录仍可查询且字段为空。"""
        self.store.loaded["events"].extend([
            {
                "schema_version": 4, "record_type": "invocation_started", "event_id": "start-v4",
                "invocation_id": "invoke-v4", "timestamp": "2026-08-29T09:02:00+08:00",
                "source": "web", "agent": "codex", "operation": "rule.user.update", "status": "started",
                "change_id": "change-v4", "resource_type": "rule", "resource_id": "user",
                "before_hash": "a" * 64, "rollback_status": "not_started",
            },
            {
                "schema_version": 4, "record_type": "invocation_finished", "event_id": "finish-v4",
                "invocation_id": "invoke-v4", "timestamp": "2026-08-29T09:02:01+08:00",
                "source": "web", "agent": "codex", "operation": "rule.user.update", "status": "succeeded",
                "change_id": "change-v4", "resource_type": "rule", "resource_id": "user",
                "before_hash": "a" * 64, "after_hash": "b" * 64, "rollback_status": "not_applicable",
            },
        ])
        payload = web_audit_query(self.store, change_id="change-v4", resource_type="rule", resource_id="user", page_size=10)
        self.assertEqual(payload["pagination"]["total"], 1)
        self.assertEqual(payload["items"][0]["after_hash"], "b" * 64)
        self.assertEqual(web_audit_query(self.store, change_id="not-found", page_size=10)["pagination"]["total"], 0)
        legacy = web_audit_query(self.store, source="hook", page_size=10)["items"][0]
        for field in ("change_id", "resource_type", "resource_id", "before_hash", "after_hash", "rollback_status"):
            self.assertIsNone(legacy[field])

    def test_web_text_redacts_unix_roots_without_touching_logical_paths(self) -> None:
        """确认 Unix 绝对路径不残留根目录前缀，知识逻辑路径和 URL 保持可读。"""
        for root in ("opt", "srv", "root", "usr", "mnt", "foo"):
            value = _redact_web_text(f"before /{root}/private/file.txt after")
            self.assertNotIn(f"/{root}/", value)
            self.assertIn("[PATH]", value)
        self.assertEqual(_redact_web_text("content/projects/aikb-web/README.md"), "content/projects/aikb-web/README.md")
        self.assertEqual(_redact_web_text("https://example.com/a/b"), "https://example.com/a/b")

    def test_same_timestamp_uses_stable_identifiers(self) -> None:
        """确认同一时间的调用按 invocation_id 稳定排序，不依赖 JSONL 文件顺序。"""
        store = FakeAuditStore()
        store.loaded["events"] = [
            {"record_type": "invocation_started", "event_id": "event-z", "invocation_id": "invoke-z", "timestamp": "2026-08-29T09:00:00+08:00", "status": "started"},
            {"record_type": "invocation_started", "event_id": "event-a", "invocation_id": "invoke-a", "timestamp": "2026-08-29T09:00:00+08:00", "status": "started"},
        ]
        items = web_audit_query(store, page_size=10)["items"]
        self.assertEqual([item["invocation_id"] for item in items], ["invoke-a", "invoke-z"])


if __name__ == "__main__":
    unittest.main()
