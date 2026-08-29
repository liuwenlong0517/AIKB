"""阶段 3 波次 2 编排、REST 和 SSE 的隔离回归测试。"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from aikb_web.core.actions import ActionRegistry, ActionSpec
from aikb_web.core.orchestrator import OrchestratorError, TaskOrchestrator
from aikb_web.core.tasks import TaskStore
from aikb_web.platform.base import PlatformState
from aikb_web.main import create_app


class _Gateway:
    """提供既有知识路由所需的最小替身，不触碰真实核心数据。"""

    settings = None

    def overview(self) -> dict[str, Any]:
        return {"index": {"available": True}}


class _Executor:
    """可控 fake executor：只发安全输出，不启动脚本或外部进程。"""

    def run(self, task: dict[str, Any], emit: Any, cancel_event: Any) -> dict[str, str]:
        emit("executor output\n")
        return {
            "status": "succeeded", "outcome": "fake_success", "summary": "safe summary",
            "path": "C:\\private\\result.txt", "nested": {"command": "whoami", "token": "secret-token", "secret": "hidden"},
        }


class _TimeoutExecutor:
    """不响应取消的 fake，用于验证编排器仍会安全收敛超时状态。"""

    def run(self, task: dict[str, Any], emit: Any, cancel_event: Any) -> dict[str, str]:
        time.sleep(1.4)
        return {"outcome": "late"}


class Phase3ApiTests(unittest.TestCase):
    """覆盖 mutation 安全边界、令牌单次消费、后台任务和 SSE 断点。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="aikb-phase3-api-")
        self.audit: list[dict[str, Any]] = []
        self.orchestrator = TaskOrchestrator(Path(self.temp.name), executor=_Executor(), audit_sink=self.audit.append)
        app = create_app(_Gateway(), orchestrator=self.orchestrator)
        # 测试注入 fake executor 代表已通过平台适配检查，避免依赖宿主机 PATH。
        app.state.platform_action_available = True
        self.client = TestClient(app)
        self.platform_patcher = patch(
            "aikb_web.api.v1.actions.platform_state",
            return_value=PlatformState("windows", "test", True),
        )
        self.platform_patcher.start()
        self.headers = {
            "Content-Type": "application/json",
            "X-AIKB-Request": "1",
            "Host": "localhost:80",
            "Origin": "http://localhost:80",
        }

    def tearDown(self) -> None:
        self.orchestrator.shutdown()
        self.platform_patcher.stop()
        self.temp.cleanup()

    def _preview(self) -> dict[str, Any]:
        response = self.client.post(
            "/api/v1/actions/validate.structure/preview",
            headers=self.headers,
            json={"parameters": {}},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]

    def test_actions_preview_and_mutation_security(self) -> None:
        self.assertEqual(self.client.get("/api/v1/actions").json()["data"]["items"][0]["action_id"], "repository.status.control")
        preview = self._preview()
        self.assertFalse(preview["preview"]["confirmation_required"])
        self.assertEqual(len(preview["confirmation_token"].split(".", 1)), 2)
        missing = self.client.post("/api/v1/actions/validate.structure/preview", json={"parameters": {}})
        self.assertEqual(missing.status_code, 400)
        wrong_origin = self.client.post(
            "/api/v1/actions/validate.structure/preview",
            headers={**self.headers, "Origin": "https://evil.example"},
            json={"parameters": {}},
        )
        self.assertEqual(wrong_origin.status_code, 400)
        dns_rebinding = self.client.post(
            "/api/v1/actions/validate.structure/preview",
            headers={**self.headers, "Host": "evil.example", "Origin": "http://evil.example"},
            json={"parameters": {}},
        )
        self.assertEqual(dns_rebinding.status_code, 400)
        unknown = self.client.post(
            "/api/v1/actions/validate.structure/preview",
            headers=self.headers,
            json={"parameters": {}, "unexpected": True},
        )
        self.assertEqual(unknown.status_code, 422)

    def test_task_token_is_single_use_and_sse_replays_to_terminal(self) -> None:
        preview = self._preview()
        body = {
            "action_id": preview["preview"]["action_id"],
            "parameters": preview["preview"]["parameters"],
            "preview_digest": preview["preview"]["preview_digest"],
            "confirmation_token": preview["confirmation_token"],
        }
        created = self.client.post("/api/v1/tasks", headers=self.headers, json=body)
        self.assertEqual(created.status_code, 200)
        task = created.json()["data"]["task"]
        task_id = task["task_id"]
        for _ in range(50):
            detail = self.client.get(f"/api/v1/tasks/{task_id}").json()["data"]["task"]
            if detail["status"] in {"succeeded", "failed", "timed_out", "cancelled"}:
                break
            time.sleep(0.01)
        self.assertEqual(detail["status"], "succeeded")
        self.assertEqual(detail["result"]["summary"], "safe summary")
        self.assertNotIn("path", detail["result"])
        self.assertNotIn("command", json.dumps(detail["result"]))
        self.assertNotIn("token", json.dumps(detail["result"]))
        replay = self.client.post("/api/v1/tasks", headers=self.headers, json=body)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay.json()["error"]["code"], "invalid_request")

        response = self.client.get(f"/api/v1/tasks/{task_id}/events")
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: snapshot", response.text)
        self.assertIn("event: output", response.text)
        self.assertIn("event: result", response.text)
        self.assertIn("safe summary", response.text)
        self.assertNotIn("whoami", response.text)
        ids = [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")]
        self.assertEqual(ids, sorted(set(ids)))
        resumed = self.client.get(f"/api/v1/tasks/{task_id}/events", headers={"Last-Event-ID": "1"})
        self.assertEqual(resumed.status_code, 200)
        self.assertNotIn('"event_id":1', resumed.text)
        self.assertTrue(any(item["record_type"] == "invocation_started" for item in self.audit))
        started = next(item for item in self.audit if item["record_type"] == "invocation_started")
        self.assertEqual(started["task_id"], task_id)
        self.assertEqual(started["action_id"], "validate.structure")
        self.assertTrue(any(
            item["record_type"] == "invocation_finished"
            and item["target_task_id"] == task_id
            and item["task_id"] == task_id
            and item["action_id"] == "validate.structure"
            for item in self.audit
        ))

    def test_list_and_missing_task_errors_are_safe(self) -> None:
        response = self.client.get("/api/v1/tasks")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["total"], 0)
        response = self.client.get("/api/v1/tasks/C:%5Cprivate")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("private", response.text)

    def test_non_windows_platform_cannot_preview_windows_action(self) -> None:
        with patch(
            "aikb_web.api.v1.actions.platform_state",
            return_value=PlatformState("linux", "test", False, "reserved"),
        ):
            response = self.client.post(
                "/api/v1/actions/validate.structure/preview",
                headers=self.headers,
                json={"parameters": {}},
            )
        self.assertEqual(response.status_code, 400)

    def test_cancel_and_timeout_have_terminal_safe_states(self) -> None:
        class BlockingExecutor:
            def run(self, task: dict[str, Any], emit: Any, cancel_event: Any) -> dict[str, str]:
                while not cancel_event.is_set():
                    time.sleep(0.01)
                return {"outcome": "cancelled"}

        cancel_orchestrator = TaskOrchestrator(Path(self.temp.name) / "cancel", executor=BlockingExecutor(), audit_sink=self.audit.append)
        preview = cancel_orchestrator.registry.preview("validate.structure", {})
        token = cancel_orchestrator.tokens.issue(
            action_id="validate.structure", parameters={}, risk_level="read_only", preview_digest=preview["preview_digest"],
        )
        task = cancel_orchestrator.submit(
            action_id="validate.structure", parameters={}, preview_digest=preview["preview_digest"], confirmation_token=token,
        )
        time.sleep(0.03)
        self.assertIn(cancel_orchestrator.cancel(task["task_id"])["status"], {"cancelling", "cancelled"})
        for _ in range(50):
            current = cancel_orchestrator.get_task(task["task_id"])
            if current["status"] == "cancelled":
                break
            time.sleep(0.01)
        self.assertEqual(current["status"], "cancelled")
        self.assertTrue(any(item.get("target_task_id") == task["task_id"] and item["operation"] == "task_cancel" for item in self.audit))
        cancel_started = next(item for item in self.audit if item["operation"] == "task_cancel" and item["record_type"] == "invocation_started")
        self.assertNotEqual(cancel_started["invocation_id"], task["invocation_id"])
        self.assertEqual(cancel_started["task_id"], task["task_id"])
        self.assertEqual(cancel_started["action_id"], "validate.structure")
        self.assertIsInstance(cancel_started["action"], dict)
        cancel_orchestrator.shutdown()

        spec = ActionSpec(
            "test.timeout", "测试超时", "测试", ("windows",), "read_only", (), "trusted_executor", "test",
            "test", 1, "test_timeout", 1, {"type": "object", "properties": {}, "additionalProperties": False}, False,
        )
        timeout_orchestrator = TaskOrchestrator(
            Path(self.temp.name) / "timeout", registry=ActionRegistry({"test.timeout": spec}), executor=_TimeoutExecutor(),
        )
        preview = timeout_orchestrator.registry.preview("test.timeout", {})
        token = timeout_orchestrator.tokens.issue(
            action_id="test.timeout", parameters={}, risk_level="read_only", preview_digest=preview["preview_digest"],
        )
        task = timeout_orchestrator.submit(
            action_id="test.timeout", parameters={}, preview_digest=preview["preview_digest"], confirmation_token=token,
        )
        for _ in range(150):
            current = timeout_orchestrator.get_task(task["task_id"])
            if current["status"] == "timed_out":
                break
            time.sleep(0.01)
        self.assertEqual(current["status"], "timed_out")
        timeout_orchestrator.shutdown()

    def test_startup_recovery_audits_interrupted_and_shutdown_rejects_submit(self) -> None:
        root = Path(self.temp.name) / "recovery"
        store = TaskStore(root, recover=False)
        old = store.create_task(
            action_id="validate.structure", parameters={}, risk_level="read_only", effects=[], timeout_seconds=120,
            concurrency_group="structure_validation", preview_digest="d" * 64, invocation_id="old-invocation",
        )
        audit: list[dict[str, Any]] = []
        recovered = TaskOrchestrator(root, task_store=store, executor=_Executor(), audit_sink=audit.append)
        self.assertEqual(recovered.get_task(old["task_id"])["status"], "interrupted")
        self.assertTrue(any(item.get("invocation_id") == "old-invocation" and item["status"] == "interrupted" for item in audit))
        preview = recovered.registry.preview("validate.structure", {})
        token = recovered.tokens.issue(
            action_id="validate.structure", parameters={}, risk_level="read_only", preview_digest=preview["preview_digest"],
        )
        recovered.shutdown()
        with self.assertRaises(OrchestratorError):
            recovered.submit(
                action_id="validate.structure", parameters={}, preview_digest=preview["preview_digest"], confirmation_token=token,
            )
        self.assertEqual(len(recovered.list_tasks()), 1)


if __name__ == "__main__":
    unittest.main()
