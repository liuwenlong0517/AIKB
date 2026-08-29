"""阶段 3 波次 1 动作和任务核心的临时 workspace 回归测试。"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from aikb_web.core.actions import ActionError, ActionRegistry, ConfirmationTokenService
from aikb_web.core.tasks import MAX_OUTPUT_LINE_BYTES, MAX_OUTPUT_TOTAL_BYTES, TaskError, TaskStore


class ActionCoreTests(unittest.TestCase):
    """验证静态注册表、严格参数和确认令牌边界。"""

    def test_static_registry_and_preview_are_strict(self) -> None:
        registry = ActionRegistry()
        self.assertEqual([item["action_id"] for item in registry.list()], [
            "repository.status.control", "repository.status.knowledge", "validate.structure",
        ])
        preview = registry.preview("validate.structure", {})
        self.assertEqual(preview["risk_level"], "read_only")
        self.assertFalse(preview["confirmation_required"])
        self.assertEqual(len(preview["preview_digest"]), 64)
        with self.assertRaises(ActionError):
            registry.preview("unknown.action", {})
        with self.assertRaises(ActionError):
            registry.preview("validate.structure", {"command": "whoami"})

    def test_confirmation_is_bound_to_preview_and_single_use(self) -> None:
        service = ConfirmationTokenService(clock=lambda: 1000.0)
        token = service.issue(action_id="validate.structure", parameters={}, risk_level="read_only", preview_digest="digest")
        service.consume(token, action_id="validate.structure", parameters={}, risk_level="read_only", preview_digest="digest")
        with self.assertRaises(ActionError):
            service.consume(token, action_id="validate.structure", parameters={}, risk_level="read_only", preview_digest="digest")
        expired = ConfirmationTokenService(clock=lambda: 1000.0)
        token = expired.issue(action_id="validate.structure", parameters={}, risk_level="read_only", preview_digest="digest")
        expired._clock = lambda: 1300.0
        with self.assertRaises(ActionError):
            expired.consume(token, action_id="validate.structure", parameters={}, risk_level="read_only", preview_digest="digest")

    def test_confirmation_concurrent_consume_is_single_success(self) -> None:
        service = ConfirmationTokenService(clock=lambda: 1000.0)
        token = service.issue(action_id="validate.structure", parameters={}, risk_level="read_only", preview_digest="digest")

        def consume_once() -> str:
            try:
                service.consume(token, action_id="validate.structure", parameters={}, risk_level="read_only", preview_digest="digest")
                return "ok"
            except ActionError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = [future.result() for future in (pool.submit(consume_once) for _ in range(32))]
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("rejected"), 31)

    def test_confirmation_tokens_purge_expired_and_are_bounded(self) -> None:
        """未消费预览不能无限增长；过期项在签发新令牌时主动回收。"""
        now = [1000.0]
        service = ConfirmationTokenService(clock=lambda: now[0])
        service.MAX_ACTIVE_TOKENS = 2
        for digest in ("one", "two"):
            service.issue(action_id="validate.structure", parameters={}, risk_level="read_only", preview_digest=digest)
        with self.assertRaises(ActionError):
            service.issue(action_id="validate.structure", parameters={}, risk_level="read_only", preview_digest="three")
        now[0] += service.TTL_SECONDS + 1
        service.issue(action_id="validate.structure", parameters={}, risk_level="read_only", preview_digest="after-expiry")
        self.assertEqual(len(service._records), 1)


class TaskCoreTests(unittest.TestCase):
    """验证 JSONL/snapshot、状态机、恢复和输出预算。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="aikb-web-task-")
        self.root = Path(self.temp.name)
        self.store = TaskStore(self.root)
        self.task = self.store.create_task(
            action_id="repository.status.control", parameters={}, risk_level="read_only",
            effects=["read:control_repository"], timeout_seconds=15, concurrency_group="repository_status",
            preview_digest="a" * 64, invocation_id="invoke-1",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_fact_source_and_atomic_snapshot_have_no_paths(self) -> None:
        task_id = self.task["task_id"]
        directory = next((self.root / "runtime" / "web" / "tasks").rglob("snapshot.json")).parent
        self.assertRegex(directory.parent.parent.name, r"^20\d{2}$")
        self.assertRegex(directory.parent.name, r"^(0[1-9]|1[0-2])$")
        self.assertEqual(self.store.get_task(task_id)["status"], "queued")
        self.assertNotIn(str(self.root), json.dumps(self.store.get_task(task_id), ensure_ascii=False))
        events = (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
        first = json.loads(events[0])
        self.assertEqual(first["type"], "snapshot")
        self.assertEqual(first["snapshot"]["task_id"], task_id)
        self.assertEqual(first["snapshot"]["status"], "queued")
        self.assertTrue((directory / "snapshot.json").is_file())

    def test_create_failure_does_not_leave_derived_snapshot(self) -> None:
        before = set((self.root / "runtime" / "web" / "tasks").rglob("snapshot.json"))
        with patch.object(self.store, "_append_event", side_effect=TaskError("simulated fact write failure")):
            with self.assertRaises(TaskError):
                self.store.create_task(
                    action_id="validate.structure", parameters={}, risk_level="read_only", effects=[],
                    timeout_seconds=120, concurrency_group="structure_validation", preview_digest="c" * 64,
                )
        after = set((self.root / "runtime" / "web" / "tasks").rglob("snapshot.json"))
        self.assertEqual(after, before)

    def test_snapshot_is_rebuilt_from_events_when_missing_damaged_or_stale(self) -> None:
        task_id = self.task["task_id"]
        self.store.transition(task_id, "running")
        task_dir = next((self.root / "runtime" / "web" / "tasks").rglob("events.jsonl")).parent
        snapshot_path = task_dir / "snapshot.json"

        snapshot_path.unlink()
        restored = TaskStore(self.root, recover=False).get_task(task_id)
        self.assertEqual(restored["status"], "running")
        self.assertEqual(restored["last_event_id"], 2)
        self.assertTrue(snapshot_path.is_file())

        snapshot_path.write_text("{damaged", encoding="utf-8")
        restored = TaskStore(self.root, recover=False).get_task(task_id)
        self.assertEqual(restored["status"], "running")

        snapshot_path.write_text(json.dumps({"task_id": task_id, "status": "queued", "last_event_id": 1}), encoding="utf-8")
        restored = TaskStore(self.root, recover=False).get_task(task_id)
        self.assertEqual(restored["status"], "running")
        self.assertEqual(restored["last_event_id"], 2)

    def test_transitions_cancel_and_crash_recovery(self) -> None:
        task_id = self.task["task_id"]
        self.store.transition(task_id, "running")
        self.store.cancel(task_id)
        self.assertEqual(self.store.get_task(task_id)["status"], "cancelling")
        self.store.transition(task_id, "cancelled")
        self.assertEqual(self.store.cancel(task_id)["status"], "cancelled")
        other = self.store.create_task(
            action_id="validate.structure", parameters={}, risk_level="read_only", effects=[], timeout_seconds=120,
            concurrency_group="structure_validation", preview_digest="b" * 64,
        )
        recovered = TaskStore(self.root)
        self.assertEqual(recovered.get_task(other["task_id"])["status"], "interrupted")

    def test_output_is_redacted_bounded_and_final_result_safe(self) -> None:
        task_id = self.task["task_id"]
        self.store.transition(task_id, "running")
        events = self.store.append_output(task_id, "password=secret /opt/private/file\n" + "中" * 5000)
        output_events = [event for event in events if event["type"] == "output"]
        self.assertTrue(output_events)
        self.assertTrue(all(len(event["text"].encode("utf-8")) <= MAX_OUTPUT_LINE_BYTES for event in output_events))
        task_dir = next((self.root / "runtime" / "web" / "tasks").rglob("events.jsonl"))
        self.assertTrue(all(len(line.encode("utf-8")) <= MAX_OUTPUT_LINE_BYTES for line in task_dir.read_text(encoding="utf-8").splitlines()))
        self.assertNotIn("\ufffd", task_dir.read_text(encoding="utf-8"))
        self.assertNotIn("/opt/", json.dumps(self.store.get_task(task_id), ensure_ascii=False))
        self.store.finish(task_id, status="succeeded", result={"summary": "ok", "path": "/root/private"})
        result = self.store.get_task(task_id)
        self.assertEqual(result["status"], "succeeded")
        self.assertNotIn("/root/", json.dumps(result, ensure_ascii=False))
        with self.assertRaises(TaskError):
            self.store.append_output(task_id, "later")

    def test_file_uri_and_sensitive_result_keys_are_redacted_in_fact_source(self) -> None:
        """file URI、令牌、命令和嵌套秘密不得进入 JSONL 或 snapshot。"""
        task_id = self.task["task_id"]
        self.store.transition(task_id, "running")
        self.store.append_output(task_id, "file:///home/alice/private.txt file:///C:/Users/alice/private.txt")
        self.store.finish(task_id, status="succeeded", result={
            "summary": "ok",
            "token": "plain-secret",
            "nested": {"authorization": "Bearer private", "command": "whoami", "path": "/root/private"},
        })
        task_dir = next((self.root / "runtime" / "web" / "tasks").rglob("events.jsonl")).parent
        persisted = (task_dir / "events.jsonl").read_text(encoding="utf-8") + (task_dir / "snapshot.json").read_text(encoding="utf-8")
        for forbidden in ("/home/alice", "C:/Users/alice", "plain-secret", "Bearer private", "whoami", "/root/private"):
            self.assertNotIn(forbidden, persisted)
        self.assertIn("[REDACTED]", persisted)
        self.assertIn("[LOCAL_PATH]", persisted)

    def test_output_jsonl_budget_counts_escaping_and_preserves_newlines(self) -> None:
        task_id = self.task["task_id"]
        self.store.transition(task_id, "running")
        events = self.store.append_output(task_id, ('"\\' * 5000) + "\nreadable\n")
        output_events = [event for event in events if event["type"] == "output"]
        self.assertGreater(len(output_events), 1)
        task_dir = next((self.root / "runtime" / "web" / "tasks").rglob("events.jsonl"))
        physical_lines = task_dir.read_text(encoding="utf-8").splitlines()
        self.assertTrue(all(len(line.encode("utf-8")) <= MAX_OUTPUT_LINE_BYTES for line in physical_lines))
        self.assertIn("\n", "".join(event["text"] for event in output_events))
        # 重新实例化后仍应按事实源保留转义字符和换行，不因回放限长而丢尾部。
        self.assertEqual(TaskStore(self.root, recover=False).get_task(task_id)["output"], self.store.get_task(task_id)["output"])

    def test_concurrent_output_events_have_unique_monotonic_ids(self) -> None:
        task_id = self.task["task_id"]
        self.store.transition(task_id, "running")
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.store.append_output, task_id, f"line-{index}\n") for index in range(40)]
            for future in futures:
                future.result()
        task_dir = next((self.root / "runtime" / "web" / "tasks").rglob("events.jsonl"))
        events = [json.loads(line) for line in task_dir.read_text(encoding="utf-8").splitlines()]
        ids = [event["event_id"] for event in events]
        self.assertEqual(ids, list(range(1, len(ids) + 1)))
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
