"""阶段 2 后端路由契约测试；不触及真实 Markdown、JSONL 或 SQLite。"""

from __future__ import annotations

import json
import unittest
from typing import Any

from fastapi.testclient import TestClient

from aikb_web.main import create_app


class FakeGateway:
    """只实现网关公共方法，验证 HTTP 层不会自行读取事实源。"""

    settings = None

    def __init__(self) -> None:
        """允许测试注入共享核心返回的索引状态，覆盖详情降级传播。"""
        self.detail_index: dict[str, Any] | None = None

    def overview(self) -> dict[str, Any]:
        return {"document_count": 0, "index": {"available": True}}

    def list_documents(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def list_tags(self) -> list[dict[str, Any]]:
        return []

    def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return {"query": query, "results": []}

    def read(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        return {"id": identifier, "status": "verified", "content": "ok"}

    def web_active_work_states(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "count": 1,
            "items": [{"work_id": "task-1", "status": "active", "project_id": "safe-project"}],
            "pagination": {"page": kwargs["page"], "page_size": kwargs["page_size"], "total": 1, "has_next": False},
            "index": {"status": "ready"},
        }

    def web_work_state(self, work_id: str) -> dict[str, Any]:
        data: dict[str, Any] = {"item": {"work_id": work_id, "sections": {"goal": "safe"}}}
        if self.detail_index is not None:
            data["index"] = self.detail_index
        return data

    def web_checkpoints(self, work_id: str, **kwargs: Any) -> dict[str, Any]:
        return {"work_id": work_id, "items": [], "pagination": {"total": 0}}

    def web_checkpoint(self, work_id: str, checkpoint_id: str) -> dict[str, Any]:
        return {"work_id": work_id, "item": {"checkpoint_id": checkpoint_id, "sections": {"goal": "safe"}}}

    def web_repository_summary(self) -> dict[str, Any]:
        return {"status": "ready", "repositories": [
            {"role": "control", "available": True, "branch": "main", "revision": "abc123", "dirty": True},
            {"role": "knowledge", "available": True, "branch": "main", "revision": "def456", "dirty": False},
        ]}

    def web_audit_summary(self, **kwargs: Any) -> dict[str, Any]:
        return {"count": 0, "statuses": {}, "damaged_count": 0}

    def web_audit_query(self, **kwargs: Any) -> dict[str, Any]:
        return {"items": [{
            "invocation_id": "invoke-1", "event_id": "event-1", "status": "succeeded",
            "action": "must-not-pass", "traceback": "must-not-pass",
        }], "summary": {"count": 1, "damaged_count": 0}, "pagination": {"total": 1}}

    def web_audit_detail(self, identifier: str) -> dict[str, Any] | None:
        if identifier != "invoke-1":
            return None
        return {"invocation_id": identifier, "status": "succeeded", "diagnostic": "must-not-pass"}


class RuntimeAuditApiTests(unittest.TestCase):
    """验证新路由的状态码、参数边界、错误包络和安全投影。"""

    def setUp(self) -> None:
        self.client = TestClient(create_app(FakeGateway()))

    def test_working_state_empty_shape_and_details(self) -> None:
        response = self.client.get("/api/v1/runtime/working-states")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["items"][0]["work_id"], "task-1")
        self.assertEqual(self.client.get("/api/v1/runtime/working-states/task-1").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/runtime/working-states/task-1/checkpoints").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/runtime/working-states/task-1/checkpoints/checkpoint-1").status_code, 200)

    def test_audit_filters_paging_and_safe_projection(self) -> None:
        response = self.client.get("/api/v1/audit/events", params={"source": "mcp", "page_size": 1})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("must-not-pass", serialized)
        self.assertEqual(self.client.get("/api/v1/audit/summary").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/audit/events/invoke-1").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/audit/events/not-found").status_code, 404)

    def test_invalid_ids_and_parameter_errors_are_structured(self) -> None:
        for path in (
            "/api/v1/runtime/working-states/C:%5Cprivate",
            "/api/v1/runtime/working-states/task-1/checkpoints/invalid..id",
        ):
            response = self.client.get(path)
            self.assertIn(response.status_code, (400, 404))
            self.assertEqual(response.json()["error"]["code"], "not_found" if response.status_code == 404 else "invalid_request")
            self.assertNotIn("private", response.text)
        response = self.client.get("/api/v1/runtime/working-states", params={"page_size": 51})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    def test_non_get_is_read_only_and_method_error_is_safe(self) -> None:
        response = self.client.post("/api/v1/runtime/working-states")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["error"]["code"], "method_not_allowed")
        self.assertNotIn("traceback", response.text.lower())

    def test_system_info_exposes_dirty_without_physical_path(self) -> None:
        response = self.client.get("/api/v1/system/info")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["repositories"]["control"]["dirty"])
        self.assertNotIn("E:\\", response.text)

    def test_detail_propagates_shared_index_degraded_state(self) -> None:
        """确认单任务详情不吞掉共享核心的 rebuilt/unavailable 状态。"""
        gateway = FakeGateway()
        gateway.detail_index = {"status": "rebuilt", "rebuilt": True}
        response = TestClient(create_app(gateway)).get("/api/v1/runtime/working-states/task-1")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["meta"]["degraded"])
        self.assertIn("index_rebuilt", response.json()["meta"]["warnings"])

        gateway.detail_index = {"status": "unavailable"}
        response = TestClient(create_app(gateway)).get("/api/v1/runtime/working-states/task-1")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "service_unavailable")


if __name__ == "__main__":
    unittest.main()
