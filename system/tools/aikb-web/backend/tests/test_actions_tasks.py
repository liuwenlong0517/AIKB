"""阶段 3 波次 1 动作和任务核心的临时 workspace 回归测试。"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from aikb_web.core.actions import ActionError, ActionRegistry, ConfirmationTokenService
from aikb_web.core.tasks import MAX_OUTPUT_LINE_BYTES, MAX_OUTPUT_TOTAL_BYTES, MAX_TASK_FACTS, TaskError, TaskStore


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

    def test_incremental_events_cache_and_cursor_reset(self) -> None:
        """重复快照读取不重放 JSONL，新增事件按游标读取且越界返回 reset。"""
        task_id = self.task["task_id"]
        with patch.object(self.store, "_read_events_file", wraps=self.store._read_events_file) as read_file:
            self.assertEqual(self.store.get_task(task_id)["last_event_id"], 1)
            self.assertEqual(self.store.get_task(task_id)["last_event_id"], 1)
            self.store.transition(task_id, "running")
            batch = self.store.events_after(task_id, 1)
            self.assertEqual([event["event_id"] for event in batch.events], [2])
            self.assertEqual(read_file.call_count, 1)
            self.assertTrue(self.store.events_after(task_id, 2).events == [])
            reset = self.store.events_after(task_id, 99)
            self.assertTrue(reset.replay_reset)
            self.assertEqual(reset.latest_event_id, 2)

    def test_event_wait_is_notified_by_append_and_shared_by_subscribers(self) -> None:
        """等待者由写入通知唤醒，两个订阅者共享同一事实缓存。"""
        task_id = self.task["task_id"]
        self.store.get_task(task_id)
        result: list[bool] = []
        waiter = threading.Thread(target=lambda: result.append(self.store.wait_for_events(task_id, 1, 2.0)))
        waiter.start()
        time.sleep(0.03)
        self.store.transition(task_id, "running")
        waiter.join(1.0)
        self.assertEqual(result, [True])
        with patch.object(self.store, "_read_events_file", wraps=self.store._read_events_file) as read_file:
            self.assertEqual(self.store.events_after(task_id, 0).latest_event_id, 2)
            self.assertEqual(self.store.events_after(task_id, 2).events, [])
            self.assertEqual(read_file.call_count, 0)

    def test_multiple_waiters_share_condition_without_eviction(self) -> None:
        """多个并发订阅者共享条件且都能被一次追加唤醒，等待后注册表回收。"""
        task_id = self.task["task_id"]
        self.store.get_task(task_id)
        results: list[bool] = []
        ready = threading.Barrier(3)

        def wait_once() -> None:
            ready.wait()
            results.append(self.store.wait_for_events(task_id, 1, 2.0))

        threads = [threading.Thread(target=wait_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        ready.wait()
        time.sleep(0.03)
        self.store.transition(task_id, "running")
        for thread in threads:
            thread.join(1.0)
        self.assertEqual(sorted(results), [True, True])
        self.assertNotIn(task_id, self.store._conditions)

    def test_restart_with_damaged_fact_source_fails_closed(self) -> None:
        """新进程不能信任损坏的事实文件或旧 snapshot。"""
        task_id = self.task["task_id"]
        task_dir = next((self.root / "runtime" / "web" / "tasks").rglob("events.jsonl")).parent
        with (task_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"event_id":99,"type":"status"}\n')
        with self.assertRaises(TaskError):
            TaskStore(self.root, recover=False).get_task(task_id)

    def test_cached_long_history_resets_without_rescanning_and_keeps_projection(self) -> None:
        """get_task 先建立缓存后，落出尾窗的两个订阅者都只收 reset 快照。"""
        task_id = self.task["task_id"]
        self.store.transition(task_id, "running")
        self.store.append_output(task_id, "x\n" * 600)
        self.store.finish(task_id, status="succeeded", result={"summary": "final"})
        self.store.get_task(task_id)
        with patch.object(self.store, "_read_events_file", wraps=self.store._read_events_file) as read_file:
            first = self.store.events_after(task_id, 0)
            second = self.store.events_after(task_id, 0)
        self.assertTrue(first.replay_reset)
        self.assertTrue(second.replay_reset)
        self.assertEqual(read_file.call_count, 0)
        self.assertEqual(first.snapshot["status"], "succeeded")
        self.assertEqual(first.snapshot["result"]["summary"], "final")
        self.assertTrue(first.snapshot["output"])

    def test_new_store_large_history_cursor_zero_resets_with_complete_snapshot(self) -> None:
        """新进程首次遇到大历史时也不把完整事件列表交给 SSE，直接 reset 快照。"""
        task_id = self.task["task_id"]
        self.store.transition(task_id, "running")
        self.store.append_output(task_id, "y\n" * 600)
        self.store.finish(task_id, status="succeeded", result={"summary": "new-store"})
        restarted = TaskStore(self.root, recover=False)
        result = restarted.events_after(task_id, 0)
        self.assertTrue(result.replay_reset)
        self.assertEqual(result.snapshot["status"], "succeeded")
        self.assertEqual(result.snapshot["result"]["summary"], "new-store")
        self.assertTrue(result.snapshot["output"])
        # 低频兼容入口仍保留完整历史，不受 SSE 的 512 条尾部窗口影响。
        complete = restarted.read_all_events(task_id)
        self.assertGreater(len(complete), 512)
        self.assertEqual([event["event_id"] for event in complete], list(range(1, len(complete) + 1)))

    def test_fact_cache_is_bounded_and_evicted_tasks_rebuild_correctly(self) -> None:
        """历史任务投影采用有界 LRU，淘汰后仍从事实源严格重建。"""
        tasks = [self.task]
        for index in range(MAX_TASK_FACTS + 8):
            tasks.append(self.store.create_task(
                action_id="validate.structure", parameters={}, risk_level="read_only", effects=[], timeout_seconds=120,
                concurrency_group="structure_validation", preview_digest=f"{index:064x}",
            ))
        for task in tasks:
            self.store.get_task(task["task_id"])
        self.assertLessEqual(len(self.store._facts), MAX_TASK_FACTS)
        self.assertEqual(self.store.get_task(self.task["task_id"])["task_id"], self.task["task_id"])
        self.assertLessEqual(len(self.store._facts), MAX_TASK_FACTS)


if __name__ == "__main__":
    unittest.main()
