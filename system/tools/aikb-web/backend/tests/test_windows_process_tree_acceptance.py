"""后续波次 3 的真实 Windows 父子孙进程树终验入口。

默认跳过，避免普通后端测试启动外部进程。设置
``AIKB_RUN_WINDOWS_TREE_ACCEPTANCE=1`` 后运行本文件，才执行真实 Job Object
取消/超时/关闭测试。每代进程把 PID 写入独立临时标记，验收不依赖执行器结果
中的内部 PID 字段。
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from aikb_web.platform.windows.commands import CommandSpec
from aikb_web.platform.windows.executor import ExecutionResult, WindowsExecutor


WAIT_TIMEOUT = 0x00000102
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000


class _ProcessProbe:
    """用只读 OpenProcess/WaitForSingleObject 探测 PID 是否仍存活。"""

    def __init__(self) -> None:
        if os.name != "nt":
            raise unittest.SkipTest("仅 Windows 执行真实进程树验收")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        self._kernel32.OpenProcess.restype = ctypes.c_void_p
        self._kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_int

    def alive(self, pid: int) -> bool:
        """返回 PID 当前是否可查询且未退出，不终止或修改目标进程。"""
        handle = self._kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return self._kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            self._kernel32.CloseHandle(handle)


def _tree_script() -> str:
    """生成单文件树脚本；子进程通过绝对 Python 路径继续生成下一代。"""
    return """import os, pathlib, subprocess, sys, time
role, marker, child_marker, grandchild_marker, duration = sys.argv[1:]
pathlib.Path(marker).write_text(str(os.getpid()), encoding='ascii')
child = None
if role == 'parent':
    child = subprocess.Popen([sys.executable, __file__, 'child', child_marker, grandchild_marker, grandchild_marker, duration])
elif role == 'child':
    child = subprocess.Popen([sys.executable, __file__, 'grandchild', child_marker, grandchild_marker, grandchild_marker, duration])
if float(duration) < 0:
    while True:
        time.sleep(1)
else:
    time.sleep(float(duration))
if child is not None:
    child.wait()
"""


@unittest.skipUnless(
    os.name == "nt" and os.environ.get("AIKB_RUN_WINDOWS_TREE_ACCEPTANCE") == "1",
    "后续波次 3 显式 Windows 进程树验收",
)
class WindowsProcessTreeAcceptance(unittest.TestCase):
    """真实验证 Job Object 能收敛父、子、孙三代进程。"""

    def _scenario(self, root: Path, duration: float) -> tuple[CommandSpec, dict[str, Path], object]:
        """准备树脚本、三代 PID 标记和只含必要目录字段的服务设置。"""
        script = root / "tree.py"
        script.write_text(_tree_script(), encoding="utf-8")
        markers = {role: root / f"{role}.pid" for role in ("parent", "child", "grandchild")}
        command = CommandSpec(
            "tree.acceptance", Path(sys.executable).resolve(),
            (str(script), "parent", str(markers["parent"]), str(markers["child"]), str(markers["grandchild"]), str(duration)),
            root,
        )
        settings = type("Settings", (), {"repo_root": root, "knowledge_root": root})()
        return command, markers, settings

    def _wait_for_markers(self, markers: dict[str, Path], timeout: float = 8.0) -> list[int]:
        """等待三代完成 PID 标记，超时即失败。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(path.is_file() for path in markers.values()):
                try:
                    return [int(markers[role].read_text(encoding="ascii")) for role in ("parent", "child", "grandchild")]
                except (OSError, ValueError):
                    pass
            time.sleep(0.05)
        self.fail("三代进程未在限定时间写入 PID 标记")

    def _assert_dead(self, probe: _ProcessProbe, pids: list[int], timeout: float = 8.0) -> None:
        """等待并确认每个标记 PID 均不可存活，避免只断言执行器状态。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(probe.alive(pid) for pid in pids):
                return
            time.sleep(0.05)
        self.fail(f"进程树仍有残留：{pids}")

    def test_normal_completion_leaves_no_processes(self) -> None:
        """三代短生命周期均正常退出，且每个 PID 最终不可存活。"""
        with tempfile.TemporaryDirectory(prefix="aikb-tree-normal-") as directory:
            root = Path(directory)
            command, markers, settings = self._scenario(root, 0.2)
            executor = WindowsExecutor()
            result_holder: list[ExecutionResult] = []
            thread = threading.Thread(target=lambda: result_holder.append(executor.run(command, settings, timeout_seconds=8)))
            thread.start()
            pids = self._wait_for_markers(markers)
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result_holder[0].status, "succeeded")
            self._assert_dead(_ProcessProbe(), pids)

    def test_user_cancel_leaves_no_processes(self) -> None:
        """用户取消通过 TerminateJobObject 收敛三代进程。"""
        with tempfile.TemporaryDirectory(prefix="aikb-tree-cancel-") as directory:
            root = Path(directory)
            command, markers, settings = self._scenario(root, -1)
            executor = WindowsExecutor()
            event = threading.Event()
            result_holder: list[ExecutionResult] = []
            thread = threading.Thread(target=lambda: result_holder.append(executor.run(command, settings, timeout_seconds=8, cancel_event=event)))
            thread.start()
            pids = self._wait_for_markers(markers)
            event.set()
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result_holder[0].status, "cancelled")
            self._assert_dead(_ProcessProbe(), pids)

    def test_timeout_leaves_no_processes(self) -> None:
        """超时路径独立验证 Job 终止和三代进程清理。"""
        with tempfile.TemporaryDirectory(prefix="aikb-tree-timeout-") as directory:
            root = Path(directory)
            command, markers, settings = self._scenario(root, -1)
            executor = WindowsExecutor()
            result_holder: list[ExecutionResult] = []
            thread = threading.Thread(target=lambda: result_holder.append(executor.run(command, settings, timeout_seconds=0.6)))
            thread.start()
            pids = self._wait_for_markers(markers)
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result_holder[0].status, "timed_out")
            self._assert_dead(_ProcessProbe(), pids)

    def test_concurrent_two_task_cancel_leaves_no_processes(self) -> None:
        """两个并发任务同时取消，分别确认两棵三代树均无残留。"""
        with tempfile.TemporaryDirectory(prefix="aikb-tree-concurrent-") as directory:
            roots = [Path(directory) / "one", Path(directory) / "two"]
            for root in roots:
                root.mkdir()
            scenarios = [self._scenario(root, -1) for root in roots]
            executor = WindowsExecutor()
            events = [threading.Event(), threading.Event()]
            holders: list[list[ExecutionResult]] = [[], []]
            threads = [threading.Thread(target=lambda i=i: holders[i].append(executor.run(scenarios[i][0], scenarios[i][2], timeout_seconds=8, cancel_event=events[i]))) for i in range(2)]
            for thread in threads:
                thread.start()
            pids = [self._wait_for_markers(scenario[1]) for scenario in scenarios]
            for event in events:
                event.set()
            for thread in threads:
                thread.join(timeout=15)
                self.assertFalse(thread.is_alive())
            self.assertEqual([holder[0].status for holder in holders], ["cancelled", "cancelled"])
            probe = _ProcessProbe()
            for tree_pids in pids:
                self._assert_dead(probe, tree_pids)

    def test_shutdown_leaves_no_processes(self) -> None:
        """服务关闭调用 Job 终止后，执行器返回且三代进程全部退出。"""
        with tempfile.TemporaryDirectory(prefix="aikb-tree-shutdown-") as directory:
            root = Path(directory)
            command, markers, settings = self._scenario(root, -1)
            executor = WindowsExecutor()
            result_holder: list[ExecutionResult] = []
            thread = threading.Thread(target=lambda: result_holder.append(executor.run(command, settings, timeout_seconds=8)))
            thread.start()
            pids = self._wait_for_markers(markers)
            executor.shutdown()
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result_holder[0].status, "failed")
            self._assert_dead(_ProcessProbe(), pids)


if __name__ == "__main__":
    unittest.main()
