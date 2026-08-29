"""Windows 受控执行底座的静态命令、环境和 Job 调用顺序测试。"""

from __future__ import annotations

import os
import ctypes
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from aikb_web.platform.windows.commands import CommandError, build_action_commands, build_child_environment
from aikb_web.platform.windows.executor import (
    CREATE_EXTENDED_STARTUPINFO_PRESENT,
    CREATE_SUSPENDED,
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
    STARTF_USESTDHANDLES,
    _StartupInfoEx,
    _Kernel32Api,
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    ExecutionError,
    WindowsExecutor,
)


class _Settings:
    """提供命令构造器所需的服务端绝对目录。"""

    def __init__(self, root: Path) -> None:
        self.repo_root = root
        self.knowledge_root = root / "knowledge"


class _FakeApi:
    """最小 Win32 mock，记录 Job 关联顺序并模拟匿名管道。"""

    def __init__(self, *, assign: bool = True, resume_value: int = 1, wait_values: list[int] | None = None, output: bytes = b"") -> None:
        self.assign_result = assign
        self.resume_value = resume_value
        self.wait_values = list(wait_values or [WAIT_OBJECT_0])
        self.output = output
        self.calls: list[tuple[str, object]] = []
        self._pipe_index = 0

    def create_job(self) -> int:
        self.calls.append(("create_job", 10))
        return 10

    def configure_job(self, handle: int) -> bool:
        self.calls.append(("configure_job", handle))
        return True

    def create_pipe(self) -> tuple[int, int]:
        self._pipe_index += 1
        pair = (100 + self._pipe_index * 2, 101 + self._pipe_index * 2)
        self.calls.append(("create_pipe", pair))
        return pair

    def create_nul(self) -> int:
        self.calls.append(("create_nul", 150))
        return 150

    def set_inherit(self, handle: int, inherit: bool) -> bool:
        self.calls.append(("set_inherit", (handle, inherit)))
        return True

    def create_process(self, *args: object) -> tuple[int, int]:
        self.calls.append(("create_process", args))
        return (20, 21)

    def assign(self, job: int, process: int) -> bool:
        self.calls.append(("assign", (job, process)))
        return self.assign_result

    def terminate_process(self, process: int, code: int = 1) -> bool:
        self.calls.append(("terminate_process", (process, code)))
        return True

    def terminate_job(self, job: int, code: int = 1) -> bool:
        self.calls.append(("terminate_job", (job, code)))
        return True

    def resume(self, thread: int) -> int:
        self.calls.append(("resume", thread))
        return self.resume_value

    def wait(self, process: int, milliseconds: int) -> int:
        self.calls.append(("wait", (process, milliseconds)))
        return self.wait_values.pop(0) if self.wait_values else WAIT_OBJECT_0

    def exit_code(self, process: int) -> int:
        self.calls.append(("exit_code", process))
        return 0

    def read(self, handle: int, size: int = 4096) -> tuple[bool, bytes]:
        self.calls.append(("read", handle))
        output, self.output = self.output, b""
        return (bool(output), output)

    def close(self, handle: int | None) -> None:
        self.calls.append(("close", handle))


class WindowsCommandTests(unittest.TestCase):
    """验证动作数组和子环境不接受客户端扩展。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="aikb win executor ")
        self.root = Path(self.temp.name)
        self.settings = _Settings(self.root)
        self.settings.knowledge_root.mkdir()
        (self.root / "system" / "tests").mkdir(parents=True)
        (self.root / "system" / "tests" / "validate-structure.ps1").write_text("# fixture", encoding="utf-8")
        (self.root / "trusted").mkdir()
        for name in ("pwsh", "git"):
            (self.root / "trusted" / f"{name}.exe").write_bytes(b"")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_three_actions_are_fixed_absolute_argument_arrays(self) -> None:
        with patch(
            "aikb_web.platform.windows.commands._registered_program",
            side_effect=lambda name: self.root / "trusted" / f"{name}.exe",
        ):
            validate = build_action_commands("validate.structure", self.settings)[0]
            control = build_action_commands("repository.status.control", self.settings)
            knowledge = build_action_commands("repository.status.knowledge", self.settings)
        self.assertTrue(validate.program.is_absolute())
        self.assertEqual(validate.argv[0:4], ("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"))
        self.assertIn("-PythonPath", validate.argv)
        self.assertEqual(validate.argv[validate.argv.index("-PythonPath") + 1], str(Path(sys.executable).resolve()))
        self.assertNotIn("Invoke-Expression", validate.arguments())
        self.assertEqual(len(control), 3)
        self.assertTrue(all(item.cwd == self.settings.knowledge_root.resolve() for item in knowledge))
        self.assertEqual(knowledge[1].argv[-3:], ("rev-parse", "--short=12", "HEAD"))
        with self.assertRaises(CommandError):
            build_action_commands("custom.command", self.settings)

    def test_child_environment_is_blank_allowlist(self) -> None:
        environment = build_child_environment(self.settings, {
            "SystemRoot": "C:\\Windows", "TEMP": "C:\\Temp", "TMP": "C:\\Temp",
            "SystemDrive": "C:", "ProgramData": "C:\\ProgramData", "ALLUSERSPROFILE": "C:\\ProgramData",
            "USERPROFILE": "C:\\Users\\tester", "SECRET_TOKEN": "do-not-leak", "HTTP_PROXY": "proxy",
        })
        self.assertEqual(environment["AIKB_HOME"], str(self.root.resolve()))
        self.assertEqual(environment["AIKB_KNOWLEDGE_HOME"], str(self.settings.knowledge_root.resolve()))
        self.assertNotIn("SECRET_TOKEN", environment)
        self.assertNotIn("HTTP_PROXY", environment)
        self.assertNotIn(str(self.root), environment["Path"])
        self.assertEqual(set(environment) - {"AIKB_HOME", "AIKB_KNOWLEDGE_HOME", "PYTHONIOENCODING", "PYTHONUTF8", "NO_COLOR", "SystemRoot", "SystemDrive", "ProgramData", "ALLUSERSPROFILE", "TEMP", "TMP", "USERPROFILE", "Path", "PATHEXT"}, set())

    def test_trusted_program_does_not_fall_back_to_path(self) -> None:
        """机器级安装记录缺失时拒绝动作，不执行仓库或 workspace 中的同名文件。"""
        with patch("aikb_web.platform.windows.commands._registered_program", return_value=None):
            with self.assertRaises(CommandError):
                build_action_commands("repository.status.control", self.settings)

    def test_structure_script_accepts_optional_server_python_path(self) -> None:
        """脚本保留无参 PATH 行为，同时支持受控执行器传入的绝对叶子文件。"""
        script = Path(__file__).resolve().parents[4] / "tests" / "validate-structure.ps1"
        text = script.read_text(encoding="utf-8")
        self.assertIn("[string]$PythonPath", text)
        self.assertIn("Resolve-Path -LiteralPath $PythonPath", text)
        self.assertIn("-PathType Leaf", text)
        self.assertIn("& $pythonSource -m aikb validate", text)


class WindowsExecutorTests(unittest.TestCase):
    """验证挂起创建、Job 关联、恢复和输出/取消边界。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="aikb win execution ")
        root = Path(self.temp.name)
        self.settings = _Settings(root)
        self.settings.knowledge_root.mkdir()
        self.command = build_action_commands_with_fixture(root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_job_must_be_assigned_before_resume_and_result_is_safe(self) -> None:
        api = _FakeApi(output=b"out")
        result = WindowsExecutor(api, environment_builder=lambda settings: {"NO_COLOR": "1"}).run(
            self.command, self.settings, timeout_seconds=2,
        )
        names = [name for name, _ in api.calls]
        self.assertLess(names.index("assign"), names.index("resume"))
        process_args = next(value for name, value in api.calls if name == "create_process")
        self.assertEqual(process_args[-1], (150, 103, 105))
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.stdout, "out")
        self.assertNotIn("20", str(result.public_dict()))
        self.assertNotIn("handle", str(result.public_dict()).lower())

    def test_assign_failure_rejects_and_never_resumes(self) -> None:
        api = _FakeApi(assign=False)
        with self.assertRaises(ExecutionError):
            WindowsExecutor(api, environment_builder=lambda settings: {}).run(self.command, self.settings, timeout_seconds=2)
        names = [name for name, _ in api.calls]
        self.assertIn("terminate_process", names)
        self.assertNotIn("resume", names)

    def test_resume_dword_failure_rejects_after_job_assignment(self) -> None:
        from aikb_web.platform.windows.executor import RESUME_THREAD_FAILED

        api = _FakeApi(resume_value=RESUME_THREAD_FAILED)
        with self.assertRaises(ExecutionError):
            WindowsExecutor(api, environment_builder=lambda settings: {}).run(self.command, self.settings, timeout_seconds=2)
        names = [name for name, _ in api.calls]
        self.assertLess(names.index("assign"), names.index("resume"))
        self.assertIn("terminate_job", names)

    def test_cancel_uses_job_termination_and_marks_safe_status(self) -> None:
        api = _FakeApi(wait_values=[WAIT_TIMEOUT, WAIT_OBJECT_0])
        cancel = threading.Event()
        cancel.set()
        result = WindowsExecutor(api, environment_builder=lambda settings: {}).run(
            self.command, self.settings, timeout_seconds=2, cancel_event=cancel,
        )
        self.assertEqual(result.status, "cancelled")
        self.assertTrue(any(name == "terminate_job" for name, _ in api.calls))

    def test_invalid_utf8_is_reported_without_replacement_character(self) -> None:
        api = _FakeApi(output=b"ok\xff")
        result = WindowsExecutor(api, environment_builder=lambda settings: {}).run(
            self.command, self.settings, timeout_seconds=2,
        )
        self.assertTrue(result.output_invalid_utf8)
        self.assertNotIn("\ufffd", result.stdout)
        self.assertIn("[INVALID_UTF8]", result.stdout)

    def test_nonzero_exit_code_is_failed(self) -> None:
        api = _FakeApi()
        api.exit_code = lambda process: 7
        result = WindowsExecutor(api, environment_builder=lambda settings: {}).run(
            self.command, self.settings, timeout_seconds=2,
        )
        self.assertEqual(result.status, "failed")

    def test_wait_failed_terminates_job_and_fails_safely(self) -> None:
        from aikb_web.platform.windows.executor import WAIT_FAILED

        api = _FakeApi(wait_values=[WAIT_FAILED])
        result = WindowsExecutor(api, environment_builder=lambda settings: {}).run(
            self.command, self.settings, timeout_seconds=2,
        )
        self.assertEqual(result.status, "failed")
        self.assertTrue(any(name == "terminate_job" for name, _ in api.calls))

    @unittest.skipUnless(os.name == "nt", "仅 Windows 检查真实 kernel32 原型")
    def test_kernel32_uses_pointer_sized_handles_and_extended_startup(self) -> None:
        api = _Kernel32Api()
        self.assertIs(api.dll.CreateJobObjectW.restype, ctypes.c_void_p)
        self.assertIs(api.dll.ResumeThread.restype, ctypes.c_uint32)
        self.assertIs(api.dll.CreateProcessW.restype, ctypes.c_int)
        self.assertEqual(CREATE_SUSPENDED & CREATE_EXTENDED_STARTUPINFO_PRESENT, 0)
        self.assertGreater(PROC_THREAD_ATTRIBUTE_HANDLE_LIST, 0)
        self.assertGreater(STARTF_USESTDHANDLES, 0)
        self.assertTrue(hasattr(_StartupInfoEx, "lpAttributeList"))


def build_action_commands_with_fixture(root: Path):
    """构造不依赖真实 PATH 的静态命令夹具。"""
    from unittest.mock import patch
    from aikb_web.platform.windows.commands import build_action_commands

    settings = _Settings(root)
    (root / "system" / "tests").mkdir(parents=True)
    (root / "system" / "tests" / "validate-structure.ps1").write_text("# fixture", encoding="utf-8")
    trusted = root / "trusted"
    trusted.mkdir()
    for name in ("pwsh", "git"):
        (trusted / f"{name}.exe").write_bytes(b"")
    with patch("aikb_web.platform.windows.commands._registered_program", side_effect=lambda name: trusted / f"{name}.exe"):
        return build_action_commands("repository.status.control", settings)[0]


if __name__ == "__main__":
    unittest.main()
