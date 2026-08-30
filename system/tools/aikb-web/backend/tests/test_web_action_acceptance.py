"""真实 Windows 三动作和 REST/SSE 验收；默认不触发，仅显式环境变量开启。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from aikb_web.core.orchestrator import TaskOrchestrator
from aikb_web.core.windows_actions import WindowsActionsExecutor
from aikb_web.main import create_app


@unittest.skipUnless(os.environ.get("AIKB_RUN_WEB_ACTION_ACCEPTANCE") == "1", "显式设置 AIKB_RUN_WEB_ACTION_ACCEPTANCE=1 才运行真实 Windows 动作")
class WebActionAcceptanceTests(unittest.TestCase):
    """只读验收真实控制仓/知识仓；不执行任何 Git 写操作。"""

    def setUp(self) -> None:
        from aikb.config import Settings

        loaded = Settings.load()
        self.temp = tempfile.TemporaryDirectory(prefix="aikb-web-acceptance-")
        workspace = Path(self.temp.name) / "workspace"
        workspace.mkdir()
        self.settings = SimpleNamespace(
            repo_root=loaded.repo_root,
            knowledge_root=loaded.knowledge_root,
            workspace_root=workspace,
        )
        self.before = {"control": self._git_state(loaded.repo_root), "knowledge": self._git_state(loaded.knowledge_root)}
        self.audit: list[dict[str, Any]] = []
        self.adapter = WindowsActionsExecutor(self.settings)
        self.adapter.validate(["validate.structure", "repository.status.control", "repository.status.knowledge"])
        self.orchestrator = TaskOrchestrator(self.settings, executor=self.adapter, audit_sink=self.audit.append)

    def tearDown(self) -> None:
        self.orchestrator.shutdown()
        self.temp.cleanup()

    @staticmethod
    def _git_state(root: Path) -> tuple[str, str, str]:
        """读取 branch/revision/status 三个只读字段，作为动作前后不变基线。"""
        values = []
        for args in (("branch", "--show-current"), ("rev-parse", "--short=12", "HEAD"), ("--no-optional-locks", "status", "--porcelain=v1", "--branch")):
            result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", check=True)
            values.append(result.stdout)
        return tuple(values)  # type: ignore[return-value]

    def _run_direct(self, action_id: str) -> dict[str, Any]:
        """通过预览令牌提交真实动作并等待终态。"""
        preview = self.orchestrator.registry.preview(action_id, {})
        token = self.orchestrator.tokens.issue(
            action_id=action_id, parameters={}, risk_level=preview["risk_level"], preview_digest=preview["preview_digest"],
        )
        task = self.orchestrator.submit(
            action_id=action_id, parameters={}, preview_digest=preview["preview_digest"], confirmation_token=token,
        )
        for _ in range(600):
            current = self.orchestrator.get_task(task["task_id"])
            if current["status"] in {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}:
                return current
            time.sleep(0.1)
        self.fail(f"真实动作未在期限内结束: {action_id}")

    def test_real_three_actions_are_read_only_and_audited(self) -> None:
        """三动作均成功，事件/审计完整且双仓状态前后完全一致。"""
        tasks = [self._run_direct(action_id) for action_id in (
            "validate.structure", "repository.status.control", "repository.status.knowledge",
        )]
        for task in tasks:
            with self.subTest(action_id=task["action_id"]):
                self.assertEqual(task["status"], "succeeded", {"result": task.get("result"), "output": task.get("output")})
        for task in tasks:
            events = self.orchestrator.events(task["task_id"])
            ids = [event["event_id"] for event in events]
            self.assertEqual(ids, list(range(1, len(ids) + 1)))
            serialized = json.dumps({"task": task, "events": events}, ensure_ascii=False)
            self.assertNotIn("\ufffd", serialized)
            self.assertNotIn("password=", serialized.lower())
            self.assertNotIn(str(self.settings.repo_root), serialized)
            self.assertNotIn(str(self.settings.knowledge_root), serialized)
            invocation = task["invocation_id"]
            records = [item for item in self.audit if item.get("invocation_id") == invocation]
            self.assertEqual({item["record_type"] for item in records}, {"invocation_started", "invocation_finished"})
            self.assertTrue(all(item.get("schema_version") == 4 for item in records))
            self.assertTrue(all(item.get("task_id") == task["task_id"] and item.get("action_id") == task["action_id"] for item in records))
        self.assertEqual(self.before, {"control": self._git_state(self.settings.repo_root), "knowledge": self._git_state(self.settings.knowledge_root)})

    def test_real_orchestrator_rest_preview_create_detail_sse_and_replay(self) -> None:
        """真实编排器经 TestClient 完成 preview→create→detail/SSE，令牌不可重放。"""
        app = create_app(SimpleNamespace(settings=self.settings, overview=lambda: {"index": {"available": True}}), orchestrator=self.orchestrator)
        app.state.platform_action_available = True
        client = TestClient(app)
        headers = {"Content-Type": "application/json", "X-AIKB-Request": "1", "Host": "localhost:80", "Origin": "http://localhost:80"}
        preview = client.post("/api/v1/actions/repository.status.control/preview", headers=headers, json={"parameters": {}}).json()["data"]
        body = {"action_id": "repository.status.control", "parameters": {}, "preview_digest": preview["preview"]["preview_digest"], "confirmation_token": preview["confirmation_token"]}
        response = client.post("/api/v1/tasks", headers=headers, json=body)
        self.assertEqual(response.status_code, 200)
        task_id = response.json()["data"]["task"]["task_id"]
        for _ in range(600):
            detail = client.get(f"/api/v1/tasks/{task_id}").json()["data"]["task"]
            if detail["status"] in {"succeeded", "failed", "timed_out", "cancelled"}:
                break
            time.sleep(0.1)
        self.assertEqual(detail["status"], "succeeded")
        self.assertEqual(client.post("/api/v1/tasks", headers=headers, json=body).status_code, 400)
        stream = client.get(f"/api/v1/tasks/{task_id}/events")
        self.assertEqual(stream.status_code, 200)
        self.assertIn("event: snapshot", stream.text)
        self.assertIn("event: result", stream.text)


if __name__ == "__main__":
    unittest.main()
