"""Windows Job Object 受控进程执行器。

这里是 Web 任务服务使用的最低层边界：进程必须以挂起状态创建、先加入启用
``KILL_ON_JOB_CLOSE`` 的 Job Object 再恢复；任何 Job 失败都拒绝启动。模块可在
非 Windows 主机导入，但默认执行器只允许在 Windows 上实例化，单元测试通过
注入 API mock 验证调用顺序和句柄清理。
"""

from __future__ import annotations

import codecs
import ctypes
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .commands import CommandSpec, build_child_environment


class ExecutionError(RuntimeError):
    """Job、进程或输出管道初始化失败；错误文本不包含命令和物理路径。"""


@dataclass(frozen=True)
class ExecutionResult:
    """进程执行的有限安全结果，不公开 PID、句柄、命令或环境。"""

    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    output_invalid_utf8: bool = False

    def public_dict(self) -> dict[str, Any]:
        """转换为任务服务可保存的安全字段。"""
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "output_invalid_utf8": self.output_invalid_utf8,
        }


MAX_OUTPUT_BYTES = 2 * 1024 * 1024


def _safe_output(parts: list[str]) -> str:
    """合并并限制单流输出；非法 UTF-8 不向任务事实源写入 U+FFFD。"""
    text = "".join(parts).replace("\ufffd", "[INVALID_UTF8]")
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return text
    return encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")


JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
CREATE_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
RESUME_THREAD_FAILED = 0xFFFFFFFF
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64), ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64), ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64), ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", ctypes.c_uint32), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", ctypes.c_int)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32), ("lpReserved", ctypes.c_wchar_p), ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p), ("dwX", ctypes.c_uint32), ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32), ("dwYSize", ctypes.c_uint32), ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32), ("dwFillAttribute", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16), ("cbReserved2", ctypes.c_uint16), ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", ctypes.c_void_p), ("hStdOutput", ctypes.c_void_p), ("hStdError", ctypes.c_void_p),
    ]


class _StartupInfoEx(ctypes.Structure):
    """STARTUPINFOEX，借助属性列表限制可继承句柄集合。"""

    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32), ("dwThreadId", ctypes.c_uint32),
    ]


class _Kernel32Api:
    """kernel32 的窄包装；不把原始 Win32 错误文本向上层暴露。"""

    def __init__(self) -> None:
        if os.name != "nt":
            raise ExecutionError("Windows 执行器仅支持 Windows")
        self.dll = ctypes.WinDLL("kernel32", use_last_error=True)
        self._declare_functions()

    def _declare_functions(self) -> None:
        """显式声明所有 Win32 原型，避免 64 位 HANDLE 被默认 c_int 截断。"""
        handle = ctypes.c_void_p
        bool_type = ctypes.c_int
        dword = ctypes.c_uint32
        self.dll.CreateJobObjectW.argtypes = [ctypes.POINTER(_SecurityAttributes), ctypes.c_wchar_p]
        self.dll.CreateJobObjectW.restype = handle
        self.dll.SetInformationJobObject.argtypes = [handle, dword, ctypes.c_void_p, dword]
        self.dll.SetInformationJobObject.restype = bool_type
        self.dll.CreatePipe.argtypes = [ctypes.POINTER(handle), ctypes.POINTER(handle), ctypes.POINTER(_SecurityAttributes), dword]
        self.dll.CreatePipe.restype = bool_type
        self.dll.SetHandleInformation.argtypes = [handle, dword, dword]
        self.dll.SetHandleInformation.restype = bool_type
        self.dll.CreateFileW.argtypes = [ctypes.c_wchar_p, dword, dword, ctypes.POINTER(_SecurityAttributes), dword, dword, handle]
        self.dll.CreateFileW.restype = handle
        self.dll.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, dword, dword, ctypes.POINTER(ctypes.c_size_t)]
        self.dll.InitializeProcThreadAttributeList.restype = bool_type
        self.dll.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, dword, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
        self.dll.UpdateProcThreadAttribute.restype = bool_type
        self.dll.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        self.dll.DeleteProcThreadAttributeList.restype = None
        self.dll.CreateProcessW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(_SecurityAttributes), ctypes.POINTER(_SecurityAttributes),
            bool_type, dword, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(_StartupInfoEx), ctypes.POINTER(_ProcessInformation),
        ]
        self.dll.CreateProcessW.restype = bool_type
        self.dll.AssignProcessToJobObject.argtypes = [handle, handle]
        self.dll.AssignProcessToJobObject.restype = bool_type
        self.dll.ResumeThread.argtypes = [handle]
        self.dll.ResumeThread.restype = dword
        self.dll.WaitForSingleObject.argtypes = [handle, dword]
        self.dll.WaitForSingleObject.restype = dword
        self.dll.GetExitCodeProcess.argtypes = [handle, ctypes.POINTER(dword)]
        self.dll.GetExitCodeProcess.restype = bool_type
        self.dll.ReadFile.argtypes = [handle, ctypes.c_void_p, dword, ctypes.POINTER(dword), ctypes.c_void_p]
        self.dll.ReadFile.restype = bool_type
        self.dll.TerminateJobObject.argtypes = [handle, dword]
        self.dll.TerminateJobObject.restype = bool_type
        self.dll.TerminateProcess.argtypes = [handle, dword]
        self.dll.TerminateProcess.restype = bool_type
        self.dll.CloseHandle.argtypes = [handle]
        self.dll.CloseHandle.restype = bool_type

    def create_job(self) -> int | None:
        return self.dll.CreateJobObjectW(None, None)

    def configure_job(self, handle: int) -> bool:
        info = _JobObjectExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        return bool(self.dll.SetInformationJobObject(handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)))

    def create_pipe(self) -> tuple[int, int] | None:
        attrs = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, 1)
        read_handle = ctypes.c_void_p()
        write_handle = ctypes.c_void_p()
        if not self.dll.CreatePipe(ctypes.byref(read_handle), ctypes.byref(write_handle), ctypes.byref(attrs), 0):
            return None
        return read_handle.value, write_handle.value

    def create_nul(self) -> int | None:
        """创建明确可继承的 NUL stdin，避免 STARTF_USESTDHANDLES 使用句柄 0。"""
        attrs = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, 1)
        generic_read = 0x80000000
        share_read_write = 0x00000003
        open_existing = 3
        normal = 0x00000080
        handle = self.dll.CreateFileW("NUL", generic_read, share_read_write, ctypes.byref(attrs), open_existing, normal, None)
        return None if handle in (None, 0, INVALID_HANDLE_VALUE) else handle

    def set_inherit(self, handle: int, inherit: bool) -> bool:
        return bool(self.dll.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT if inherit else 0))

    def create_process(
        self, application: str, command_line: str, cwd: str, env_block: str, stdin: int, stdout: int, stderr: int,
        inherited_handles: tuple[int, ...],
    ) -> tuple[int, int] | None:
        startup = _StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = stdin, stdout, stderr
        size = ctypes.c_size_t(0)
        # 首次调用只查询属性列表大小，失败是 Win32 约定而非启动失败。
        self.dll.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if not size.value:
            return None
        attribute_buffer = ctypes.create_string_buffer(size.value)
        attributes = ctypes.cast(attribute_buffer, ctypes.c_void_p)
        if not self.dll.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)):
            return None
        handle_array = (ctypes.c_void_p * len(inherited_handles))(*inherited_handles)
        try:
            if not self.dll.UpdateProcThreadAttribute(
                attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, ctypes.cast(handle_array, ctypes.c_void_p),
                ctypes.sizeof(handle_array), None, None,
            ):
                return None
            startup.lpAttributeList = attributes
            env_buffer = ctypes.create_unicode_buffer(env_block)
            mutable_command = ctypes.create_unicode_buffer(command_line)
            info = _ProcessInformation()
            flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW | CREATE_EXTENDED_STARTUPINFO_PRESENT
            ok = self.dll.CreateProcessW(
                application, mutable_command, None, None, True, flags, ctypes.cast(env_buffer, ctypes.c_void_p), cwd,
                ctypes.byref(startup), ctypes.byref(info),
            )
            return (info.hProcess, info.hThread) if ok else None
        finally:
            self.dll.DeleteProcThreadAttributeList(attributes)

    def assign(self, job: int, process: int) -> bool:
        return bool(self.dll.AssignProcessToJobObject(job, process))

    def resume(self, thread: int) -> int:
        # ResumeThread 返回 DWORD，0xFFFFFFFF 才表示失败，不能比较有符号 -1。
        return int(self.dll.ResumeThread(thread))

    def wait(self, process: int, milliseconds: int) -> int:
        return int(self.dll.WaitForSingleObject(process, milliseconds))

    def exit_code(self, process: int) -> int | None:
        value = ctypes.c_uint32()
        if not self.dll.GetExitCodeProcess(process, ctypes.byref(value)):
            return None
        return int(value.value)

    def read(self, handle: int, size: int = 4096) -> tuple[bool, bytes]:
        buffer = ctypes.create_string_buffer(size)
        count = ctypes.c_uint32()
        ok = bool(self.dll.ReadFile(handle, buffer, size, ctypes.byref(count), None))
        return ok, buffer.raw[: count.value]

    def terminate_job(self, job: int, code: int = 1) -> bool:
        return bool(self.dll.TerminateJobObject(job, code))

    def terminate_process(self, process: int, code: int = 1) -> bool:
        """仅清理尚未成功加入 Job 的挂起进程，不作为运行期树终止降级。"""
        return bool(self.dll.TerminateProcess(process, code))

    def close(self, handle: int | None) -> None:
        if handle not in (None, 0, INVALID_HANDLE_VALUE):
            self.dll.CloseHandle(handle)


def _environment_block(environment: dict[str, str]) -> str:
    """按 Windows 约定生成排序的 UTF-16 环境块。"""
    return "\0".join(f"{key}={value}" for key, value in sorted(environment.items(), key=lambda item: item[0].upper())) + "\0\0"


class WindowsExecutor:
    """以 Job Object 管理单个外部动作及其完整子进程树。"""

    def __init__(self, api: Any | None = None, *, environment_builder: Callable[[object], dict[str, str]] = build_child_environment):
        """绑定 Win32 API；生产默认使用 kernel32，测试可注入等价 mock。"""
        self._api = api or _Kernel32Api()
        self._environment_builder = environment_builder
        self._active: set[tuple[int, int]] = set()
        self._active_lock = threading.Lock()

    def _read_pipe(self, handle: int, sink: list[str], invalid: list[bool], stop: threading.Event) -> None:
        """增量读取 UTF-8 管道，读端错误或非法字节只记录安全标志。"""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        try:
            while not stop.is_set():
                ok, data = self._api.read(handle)
                if data:
                    try:
                        sink.append(decoder.decode(data, final=False))
                    except UnicodeDecodeError:
                        invalid.append(True)
                        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                        sink.append(decoder.decode(data, final=False).replace("\ufffd", "[INVALID_UTF8]"))
                if not ok or not data:
                    break
            try:
                tail = decoder.decode(b"", final=True)
                if tail:
                    sink.append(tail)
            except UnicodeDecodeError:
                invalid.append(True)
        finally:
            # 读端由 run() 在所有读取线程结束后统一关闭，避免重复 CloseHandle。
            pass

    def run(
        self, command: CommandSpec, settings: object, *, timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> ExecutionResult:
        """启动固定命令并等待完成、取消或超时；Job 失败时绝不父进程 kill 降级。"""
        if timeout_seconds <= 0:
            raise ExecutionError("执行超时预算无效")
        environment = self._environment_builder(settings)
        job = self._api.create_job()
        if not job or not self._api.configure_job(job):
            self._api.close(job)
            raise ExecutionError("无法建立 Windows Job Object")
        pipes: list[tuple[int, int]] = []
        stdin_handle: int | None = None
        process: int | None = None
        thread: int | None = None
        assigned = False
        readers: list[threading.Thread] = []
        stop_reading = threading.Event()
        stdout, stderr = [], []
        invalid: list[bool] = []
        started = time.monotonic()
        try:
            for _ in range(2):
                pipe = self._api.create_pipe()
                if not pipe:
                    raise ExecutionError("无法建立输出管道")
                read_handle, write_handle = pipe
                if not self._api.set_inherit(read_handle, False):
                    raise ExecutionError("无法保护输出管道")
                pipes.append(pipe)
            stdin_handle = self._api.create_nul()
            if not stdin_handle:
                raise ExecutionError("无法建立 NUL 标准输入")
            process_info = self._api.create_process(
                str(command.program), subprocess.list2cmdline(list(command.arguments())), str(command.cwd),
                _environment_block(environment), stdin_handle, pipes[0][1], pipes[1][1],
                (stdin_handle, pipes[0][1], pipes[1][1]),
            )
            if not process_info:
                raise ExecutionError("无法创建受控进程")
            process, thread = process_info
            if not self._api.assign(job, process):
                # Assign 失败时进程尚未受 Job 管理，只能清理这个仍挂起的句柄；
                # 运行期取消仍严格走 TerminateJobObject，绝不退化为父进程 kill。
                self._api.terminate_process(process, 1)
                raise ExecutionError("无法将进程加入 Windows Job Object")
            assigned = True
            if self._api.resume(thread) == RESUME_THREAD_FAILED:
                raise ExecutionError("无法恢复受控进程")
            self._api.close(thread)
            thread = None
            self._api.close(stdin_handle)
            stdin_handle = None
            for read_handle, _ in pipes:
                sink = stdout if len(readers) == 0 else stderr
                reader = threading.Thread(target=self._read_pipe, args=(read_handle, sink, invalid, stop_reading), daemon=True)
                readers.append(reader)
                reader.start()
            # 父进程只保留读端；子进程启动后立即关闭本地写端。
            for _, write_handle in pipes:
                self._api.close(write_handle)
            pipes = [(read_handle, 0) for read_handle, _ in pipes]
            with self._active_lock:
                self._active.add((job, process))
            status = "succeeded"
            while True:
                waited = self._api.wait(process, 50)
                if waited == WAIT_OBJECT_0:
                    break
                if waited == WAIT_FAILED:
                    status = "failed"
                    self._api.terminate_job(job, 5)
                    self._api.wait(process, 2000)
                    break
                if waited != WAIT_TIMEOUT:
                    # 未知等待码不应被当成超时继续轮询，避免失控进程长期存活。
                    status = "failed"
                    self._api.terminate_job(job, 6)
                    self._api.wait(process, 2000)
                    break
                if cancel_event is not None and cancel_event.is_set():
                    status = "cancelled"
                    self._api.terminate_job(job, 2)
                    self._api.wait(process, 2000)
                    break
                if time.monotonic() - started >= timeout_seconds:
                    status = "timed_out"
                    self._api.terminate_job(job, 3)
                    self._api.wait(process, 2000)
                    break
            # 进程终止后先排空两个管道，再组装返回值，避免 return 表达式早于读取线程。
            for reader in readers:
                reader.join(timeout=2)
            stop_reading.set()
            exit_code = self._api.exit_code(process)
            if status == "succeeded" and exit_code != 0:
                status = "failed"
            return ExecutionResult(status, exit_code, _safe_output(stdout), _safe_output(stderr), round((time.monotonic() - started) * 1000), bool(invalid))
        except ExecutionError:
            # 进程仍处于挂起态或尚未受控时只关闭资源；成功 Assign 后由 Job 收敛。
            if process and thread is not None and assigned:
                self._api.terminate_job(job, 1)
            raise
        finally:
            stop_reading.set()
            for reader in readers:
                reader.join(timeout=2)
            for read_handle, write_handle in pipes:
                self._api.close(read_handle)
                self._api.close(write_handle)
            self._api.close(stdin_handle)
            self._api.close(thread)
            self._api.close(process)
            with self._active_lock:
                self._active.discard((job, process))
            # 关闭 Job 是最终树收敛边界；KILL_ON_JOB_CLOSE 已在创建时强制启用。
            self._api.close(job)

    def shutdown(self) -> None:
        """服务关闭时终止仍在管理的 Job，随后关闭句柄，不暴露内部标识。"""
        with self._active_lock:
            active = list(self._active)
        for job, _ in active:
            self._api.terminate_job(job, 4)


__all__ = ["ExecutionError", "ExecutionResult", "MAX_OUTPUT_BYTES", "WindowsExecutor"]
