"""阶段 4A 规则应用任务/审计关联的安全验收。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from fastapi.testclient import TestClient

from aikb_web.core.rule_task import RuleChangeTaskCoordinator, RuleTaskRejected
from aikb_web.core.rule_preview import RulePreviewService
from aikb_web.core.rule_transaction import RuleChangeStore, RuleTransactionExecutor
from aikb_web.core.tasks import TaskStore
from aikb_web.main import create_app


class _Gateway:
    """提供最小网关和可故障注入的内存审计端。"""

    settings = None

    def __init__(self) -> None:
        """初始化仅内存状态，不触碰 AIKB 正式工作区。"""
        self.records: list[dict[str, Any]] = []
        self.fail_after: int | None = None
        self._lock = threading.Lock()

    def web_audit_write(self, record: Mapping[str, Any]) -> None:
        """记录安全审计投影，按计数模拟开始/终态故障。"""
        with self._lock:
            if self.fail_after is not None and len(self.records) >= self.fail_after:
                raise OSError(r"C:\private\audit failure")
            self.records.append(dict(record))


class _Transaction:
    """只含安全摘要的可查询事务替身。"""

    def __init__(self, change_id: str) -> None:
        """创建固定规则摘要。"""
        self.change_id = change_id
        self.status = "prepared"
        self.values = {
            "change_id": change_id, "rule_id": "user", "action_id": "rule.user.update",
            "risk_level": "source_write", "before_hash": "1" * 64, "after_hash": "2" * 64,
            "diff_hash": "3" * 64, "preview_digest": "4" * 64, "repository_revision": "a" * 40,
            "rollback_status": "not_started",
        }

    def public_dict(self) -> dict[str, Any]:
        """返回不含正文、diff、路径或令牌的事务投影。"""
        return {**self.values, "status": self.status}


class _Store:
    """为协调器提供 change 查询的内存事务存储替身。"""

    def __init__(self, transaction: _Transaction) -> None:
        """绑定单个服务端生成变更。"""
        self.transaction = transaction

    def load(self, change_id: str) -> _Transaction:
        """按逻辑 ID 查询事务，不拼接路径。"""
        if change_id != self.transaction.change_id:
            raise KeyError(change_id)
        return self.transaction


class _Executor:
    """可观测事务执行器，令牌仅用于内存校验和 worker 调用。"""

    def __init__(self, change_id: str = "change-" + "a" * 32) -> None:
        """准备固定摘要、令牌和可注入终态。"""
        self.change_id = change_id
        self.token = "token-secret"
        self.transaction = _Transaction(change_id)
        self._store = _Store(self.transaction)
        self.apply_calls = 0
        self.result_status = "succeeded"
        self.recovery_marked = False
        self.claimed_task_id: str | None = None
        self._lock = threading.Lock()

    def prepare(self, change_id: str, confirmation_token: str) -> Mapping[str, Any]:
        """执行非消费确认并返回安全摘要。"""
        if change_id != self.change_id or confirmation_token != self.token or self.transaction.status != "prepared":
            raise ValueError("invalid confirmation")
        return self.transaction.values

    def apply(self, change_id: str, confirmation_token: str, preview_digest: str) -> Mapping[str, Any]:
        """模拟底层原子事务，并返回真实终态。"""
        with self._lock:
            if confirmation_token != self.token or preview_digest != "4" * 64:
                raise ValueError("invalid confirmation")
            self.apply_calls += 1
            self.transaction.status = self.result_status
            return {"status": self.result_status, "change_id": change_id, "path": r"C:\private\USER_RULES.md"}

    def claim(self, change_id: str, task_id: str) -> bool:
        """模拟事务锁内 claim，支持跨协调器重复提交验收。"""
        with self._lock:
            if self.claimed_task_id is not None and self.claimed_task_id != task_id:
                return False
            self.claimed_task_id = task_id
            return True

    def release_claim(self, change_id: str, task_id: str) -> None:
        """释放尚未消费令牌的事务认领。"""
        with self._lock:
            if self.claimed_task_id == task_id and self.transaction.status == "prepared":
                self.claimed_task_id = None

    def mark_audit_failure(self, change_id: str) -> None:
        """把终态审计故障标为需要恢复，供协调器 fail-closed 调用。"""
        self.recovery_marked = True
        self.transaction.status = "recovery_required"

    def recover(self) -> list[dict[str, Any]]:
        """提供启动恢复钩子。"""
        return []


class RuleApplySecurityTests(unittest.TestCase):
    """覆盖准入、任务安全投影、终态审计和变更状态查询。"""

    def setUp(self) -> None:
        """为每项测试创建隔离任务事实源和协调器。"""
        self.temp = tempfile.TemporaryDirectory(prefix="aikb-rule-task-")
        self.gateway = _Gateway()
        self.executor = _Executor()
        self.coordinator = RuleChangeTaskCoordinator(
            self.executor, workspace_root=Path(self.temp.name),
            task_store=TaskStore(Path(self.temp.name), recover=False), audit_sink=self.gateway.web_audit_write,
        )
        self.app = create_app(gateway=self.gateway, rule_task_coordinator=self.coordinator)
        self.client = TestClient(self.app)
        self.headers = {
            "Content-Type": "application/json", "X-AIKB-Request": "1",
            "Host": "localhost:80", "Origin": "http://localhost:80",
        }
        self.body = {"change_id": self.executor.change_id, "confirmation_token": self.executor.token}

    def tearDown(self) -> None:
        """关闭客户端、worker 和临时任务事实源。"""
        self.client.close()
        self.coordinator.shutdown()
        self.temp.cleanup()

    def _post(self, body: Mapping[str, Any] | None = None, *, rule_id: str = "user", headers: Mapping[str, str] | None = None):
        """提交最小同源 apply 请求。"""
        return self.client.post(
            f"/api/v1/rules/{rule_id}/apply", headers=dict(headers or self.headers), json=dict(body or self.body),
        )

    def _wait_task(self, task_id: str) -> dict[str, Any]:
        """等待后台任务终态，避免以 queued 瞬时状态伪报完成。"""
        for _ in range(100):
            task = self.coordinator.store.get_task(task_id)
            if task.get("status") in {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}:
                return task
            time.sleep(0.01)
        self.fail("规则任务未进入终态")

    def test_apply_is_queued_then_terminal_and_safe_projection(self) -> None:
        """apply 只返回 queued 任务，worker 完成后才写 succeeded 审计。"""
        response = self._post()
        self.assertEqual(response.status_code, 200, response.text)
        task_id = response.json()["data"]["task"]["task_id"]
        task = self._wait_task(task_id)
        self.assertEqual(task["status"], "succeeded")
        self.assertEqual(task["parameters"], {"change_id": self.executor.change_id})
        self.assertNotIn("token-secret", json.dumps(task, ensure_ascii=False))
        self.assertEqual([item["status"] for item in self.gateway.records], ["started", "succeeded"])
        self.assertEqual(self.executor.apply_calls, 1)
        for record in self.gateway.records:
            self.assertEqual(record["schema_version"], 4)
            self.assertNotIn("confirmation_token", record)
            self.assertNotIn("path", record)
        status = self.client.get(f"/api/v1/rules/changes/{self.executor.change_id}")
        self.assertEqual(status.status_code, 200, status.text)
        projection = status.json()["data"]
        self.assertEqual(projection["task"]["task_id"], task_id)
        self.assertEqual(projection["change"]["change_id"], self.executor.change_id)
        self.assertNotIn("path", json.dumps(projection, ensure_ascii=False).lower())

    def test_wrong_token_unknown_readonly_and_request_protection_fail_closed(self) -> None:
        """错误令牌、未知/只读规则和跨源请求均不得开始审计或创建任务。"""
        self.assertEqual(self._post({**self.body, "confirmation_token": "wrong"}).status_code, 409)
        self.assertEqual(self._post(rule_id="agent").status_code, 403)
        self.assertEqual(self._post(rule_id="unknown").status_code, 403)
        self.assertEqual(self._post(headers={"Content-Type": "application/json"}).status_code, 400)
        self.assertEqual(self._post(headers={**self.headers, "Origin": "http://evil.example"}).status_code, 400)
        self.assertEqual(self.gateway.records, [])
        self.assertEqual(self.executor.apply_calls, 0)

    def test_concurrent_same_change_has_one_task_and_one_token_consumer(self) -> None:
        """并发重复提交同一 change 只能创建一个任务并调用一次 executor。"""
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self._post(), (1, 2)))
        self.assertEqual(sum(response.status_code == 200 for response in responses), 1)
        accepted = next(response for response in responses if response.status_code == 200)
        self._wait_task(accepted.json()["data"]["task"]["task_id"])
        self.assertEqual(self.executor.apply_calls, 1)
        self.assertEqual(len(self.coordinator.store.list_tasks()), 1)

    def test_two_coordinators_claim_one_change_without_duplicate_task(self) -> None:
        """两个协调器共享事务事实源时 claim 只允许一个预生成 task_id。"""
        other = RuleChangeTaskCoordinator(
            self.executor, workspace_root=Path(self.temp.name), task_store=self.coordinator.store,
            audit_sink=self.gateway.web_audit_write,
        )
        try:
            accepted = self.coordinator.apply(change_id=self.executor.change_id, confirmation_token=self.executor.token)
            with self.assertRaises(RuleTaskRejected):
                other.apply(change_id=self.executor.change_id, confirmation_token=self.executor.token)
            self._wait_task(accepted["task"]["task_id"])
            self.assertEqual(self.executor.apply_calls, 1)
            self.assertEqual(len(self.coordinator.store.list_tasks()), 1)
        finally:
            other.shutdown()

    def test_extra_fields_and_sensitive_error_material_are_rejected(self) -> None:
        """正文、diff、路径等客户端扩展不能进入任务、审计或错误响应。"""
        response = self._post({**self.body, "candidate_content": "candidate secret", "path": r"C:\private"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("candidate secret", response.text)
        self.assertNotIn("C:\\private", response.text)
        self.assertEqual(self.gateway.records, [])

    def test_terminal_audit_failure_marks_recovery_and_blocks_followup(self) -> None:
        """终态审计失败将事务显式标为 recovery_required，并阻止后续写入。"""
        self.gateway.fail_after = 1
        response = self._post()
        self.assertEqual(response.status_code, 200, response.text)
        task = self._wait_task(response.json()["data"]["task"]["task_id"])
        self.assertEqual(task["status"], "failed")
        self.assertTrue(self.executor.recovery_marked)
        self.assertEqual(self.executor.transaction.status, "recovery_required")
        self.assertEqual(self._post().status_code, 503)
        status = self.client.get("/api/v1/system/info").json()["data"]["rule_writes"]
        self.assertTrue(status["recovery_required"])

    def test_real_temporary_git_preview_apply_and_three_way_consistency(self) -> None:
        """临时 Git 仓端到端验证 preview/apply、文件摘要、事务和审计三方一致。"""
        source_root = Path(os.environ.get("AIKB_HOME", Path(__file__).resolve().parents[5])).resolve()
        with tempfile.TemporaryDirectory(prefix="aikb-rule-e2e-") as root_name:
            root = Path(root_name) / "repo"
            workspace = Path(root_name) / "workspace"
            root.mkdir()
            workspace.mkdir()
            for relative in (
                "ENTRY_RULES.md", "INDEX.md", "system/rules/USER_RULES.md",
                "system/rules/AI_RULES.md", "system/rules/CONTRIBUTING.md",
            ):
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_root / relative, destination)

            def git(*args: str) -> str:
                """运行临时仓固定 Git 命令；仓库外路径不参与命令参数。"""
                result = subprocess.run(
                    ["git", *args], cwd=root, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            git("init", "--quiet")
            git("config", "user.email", "aikb-test@example.invalid")
            git("config", "user.name", "AIKB rule task test")
            git("add", ".")
            git("commit", "--quiet", "-m", "controlled rules")
            before_status = git("status", "--porcelain=v1")
            service = RulePreviewService(SimpleNamespace(repo_root=root, workspace_root=workspace))
            detail = service.get_rule("user")
            candidate = str(detail["content"]) + "\n# phase4-e2e\n"
            preview = service.preview("user", base_content_hash=str(detail["content_hash"]), candidate_content=candidate)
            self.assertEqual(git("status", "--porcelain=v1"), before_status)
            records: list[dict[str, Any]] = []
            executor = RuleTransactionExecutor(service)
            coordinator = RuleChangeTaskCoordinator(
                executor, task_store=TaskStore(workspace, recover=False), audit_sink=records.append,
            )
            try:
                submitted = coordinator.apply(
                    change_id=str(preview["change_id"]), confirmation_token=str(preview["confirmation_token"]),
                )
                task = self._wait_external_task(coordinator, str(submitted["task"]["task_id"]))
                self.assertEqual(task["status"], "succeeded")
                change = coordinator.get_change(str(preview["change_id"]))
                self.assertEqual(change["change"]["status"], "succeeded")
                self.assertEqual(change["task"]["task_id"], task["task_id"])
                self.assertIn("phase4-e2e", (root / "system/rules/USER_RULES.md").read_text(encoding="utf-8"))
                self.assertEqual([item["status"] for item in records], ["started", "succeeded"])
                self.assertEqual(task["parameters"], {"change_id": preview["change_id"]})
                transaction_dir = RuleChangeStore(workspace)._directory(str(preview["change_id"]))
                payload = json.loads((transaction_dir / "transaction.json").read_text(encoding="utf-8"))
                self.assertNotIn("candidate_content", payload)
                self.assertFalse((transaction_dir / "candidate.md").exists())
                self.assertFalse((transaction_dir / "backup.md").exists())
                self.assertIn("USER_RULES.md", git("status", "--porcelain=v1"))
            finally:
                coordinator.shutdown()

    @staticmethod
    def _wait_external_task(coordinator: RuleChangeTaskCoordinator, task_id: str) -> dict[str, Any]:
        """等待真实原子事务任务完成。"""
        for _ in range(150):
            task = coordinator.store.get_task(task_id)
            if task.get("status") in {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}:
                return task
            time.sleep(0.02)
        raise AssertionError("真实规则任务未进入终态")


if __name__ == "__main__":
    unittest.main()
