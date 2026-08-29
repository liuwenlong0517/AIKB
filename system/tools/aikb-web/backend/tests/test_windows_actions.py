"""Windows 动作适配器的静态命令顺序和终态映射测试。"""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aikb_web.core.windows_actions import WindowsActionsExecutor


class _FakeWindowsExecutor:
    """模拟底层 Job 执行器，确保测试不创建真实进程。"""

    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.calls: list[object] = []
        self.closed = False

    def run(self, command: object, settings: object, *, timeout_seconds: float, cancel_event: threading.Event) -> object:
        self.calls.append((command, timeout_seconds))
        return self.results.pop(0)

    def shutdown(self) -> None:
        self.closed = True


class WindowsActionsAdapterTests(unittest.TestCase):
    """验证多命令失败即停、安全语义投影和取消/关闭边界。"""

    def test_second_command_failure_stops_plan_and_returns_safe_status(self) -> None:
        commands = ["branch", "revision", "status"]
        executor = _FakeWindowsExecutor([
            SimpleNamespace(status="succeeded", stdout="one\n", stderr="", public_dict=lambda: {"status": "succeeded", "stdout": "one\n", "stderr": ""}),
            SimpleNamespace(status="failed", stdout="", stderr="bad\n", public_dict=lambda: {"status": "failed", "stdout": "", "stderr": "bad\n"}),
        ])
        with patch("aikb_web.core.windows_actions.os.name", "nt"):
            adapter = WindowsActionsExecutor(
                SimpleNamespace(), executor=executor,
                command_builder=lambda action_id, settings: tuple(commands),
            )
            output: list[str] = []
            result = adapter.run({"action_id": "repository.status.control", "timeout_seconds": 10}, output.append, threading.Event())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(output, [])

    def test_repository_status_returns_semantics_without_file_names(self) -> None:
        executor = _FakeWindowsExecutor([
            SimpleNamespace(public_dict=lambda: {"status": "succeeded", "stdout": "main\n"}),
            SimpleNamespace(public_dict=lambda: {"status": "succeeded", "stdout": "abcdef123456\n"}),
            SimpleNamespace(public_dict=lambda: {"status": "succeeded", "stdout": "## main...origin/main\n M private-name.txt\n?? secret-dir/\n"}),
        ])
        with patch("aikb_web.core.windows_actions.os.name", "nt"):
            adapter = WindowsActionsExecutor(SimpleNamespace(), executor=executor, command_builder=lambda *_: ("branch", "revision", "status"))
            output: list[str] = []
            result = adapter.run({"action_id": "repository.status.control", "timeout_seconds": 10}, output.append, threading.Event())
        self.assertEqual(result, {
            "status": "succeeded", "duration_ms": result["duration_ms"], "output_invalid_utf8": False,
            "branch": "main", "revision": "abcdef123456", "dirty": True, "changed_count": 2,
        })
        self.assertEqual(output, ["仓库状态读取完成。\n"])
        self.assertNotIn("private-name", str(result) + "".join(output))

    def test_cancel_before_next_command_and_shutdown_are_forwarded(self) -> None:
        executor = _FakeWindowsExecutor([])
        with patch("aikb_web.core.windows_actions.os.name", "nt"):
            adapter = WindowsActionsExecutor(SimpleNamespace(), executor=executor, command_builder=lambda *_: ("one",))
            cancelled = threading.Event()
            cancelled.set()
            result = adapter.run({"action_id": "validate.structure", "timeout_seconds": 10}, lambda _: None, cancelled)
            adapter.shutdown()
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(executor.calls)
        self.assertTrue(executor.closed)

    def test_shared_deadline_stops_before_late_command(self) -> None:
        executor = _FakeWindowsExecutor([
            SimpleNamespace(status="succeeded", stdout="", stderr="", public_dict=lambda: {"status": "succeeded"}),
        ])
        with patch("aikb_web.core.windows_actions.os.name", "nt"), patch(
            "aikb_web.core.windows_actions.time.monotonic", side_effect=[0.0, 0.0, 2.0, 2.0],
        ):
            adapter = WindowsActionsExecutor(SimpleNamespace(), executor=executor, command_builder=lambda *_: ("one", "two"))
            result = adapter.run({"action_id": "validate.structure", "timeout_seconds": 1}, lambda _: None, threading.Event())
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(len(executor.calls), 1)


if __name__ == "__main__":
    unittest.main()
