"""受控 Windows 动作适配器。

适配器只消费静态 ``build_action_commands`` 计划，并把每个固定命令交给已有
Job Object 执行器；不接受客户端命令、路径或环境。它在非 Windows 平台不被
实例化，便于 API 明确表达能力不可用而不是伪造成功。
"""

from __future__ import annotations

import os
import re
import time
import threading
from typing import Any, Callable, Mapping

from aikb_web.platform.windows.commands import CommandError, build_action_commands, build_child_environment
from aikb_web.platform.windows.executor import ExecutionError, WindowsExecutor


class WindowsActionsUnavailable(RuntimeError):
    """受信任程序、固定脚本或 Windows Job 执行器不可用。"""


class WindowsActionsExecutor:
    """将一个动作的静态命令序列映射为 ExecutorProtocol。"""

    def __init__(self, settings: Any, *, executor: WindowsExecutor | None = None, command_builder: Callable[..., Any] = build_action_commands):
        """绑定服务端设置和可注入底层执行器；非 Windows 主机拒绝初始化。"""
        if os.name != "nt":
            raise WindowsActionsUnavailable("Windows executor unavailable")
        self.settings = settings
        self._executor = executor or WindowsExecutor(environment_builder=self._environment)
        self._command_builder = command_builder

    @staticmethod
    def _environment(settings: Any) -> dict[str, str]:
        """构造底层最小环境；系统盘和 ProgramData 也由统一白名单负责。"""
        return build_child_environment(settings)

    def validate(self, action_ids: list[str] | tuple[str, ...]) -> None:
        """启动时解析所有静态动作，确保程序/脚本缺失不会产生假能力。"""
        try:
            for action_id in action_ids:
                self._command_builder(action_id, self.settings)
        except (CommandError, OSError, ValueError) as error:
            raise WindowsActionsUnavailable("Windows action prerequisites unavailable") from error

    def run(
        self,
        task: Mapping[str, Any],
        emit: Callable[[str | bytes], None],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        """顺序执行固定命令，共享动作 deadline，并在失败/取消/超时后立即停止。"""
        started = time.monotonic()
        try:
            commands = self._command_builder(str(task.get("action_id") or ""), self.settings)
        except (CommandError, OSError, ValueError) as error:
            raise WindowsActionsUnavailable("Windows action plan unavailable") from error
        if not commands:
            return {"status": "failed", "duration_ms": 0}
        deadline = started + max(1, min(int(task.get("timeout_seconds") or 1), 120))
        last_result: Any = None
        safe_outputs: list[str] = []
        for command in commands:
            if cancel_event.is_set():
                return {"status": "cancelled", "duration_ms": round((time.monotonic() - started) * 1000)}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"status": "timed_out", "duration_ms": round((time.monotonic() - started) * 1000)}
            try:
                last_result = self._executor.run(
                    command,
                    self.settings,
                    timeout_seconds=remaining,
                    cancel_event=cancel_event,
                )
            except ExecutionError as error:
                # 底层已将句柄/Job 收敛；公共任务只记录固定失败状态。
                return {"status": "failed", "duration_ms": round((time.monotonic() - started) * 1000)}
            public = last_result.public_dict() if callable(getattr(last_result, "public_dict", None)) else dict(last_result or {})
            # 原始 stdout/stderr 只在适配器内用于语义解析。尤其 Git porcelain
            # 含全部变更文件名，不能直接进入任务事实源或浏览器。
            safe_outputs.append(str(public.get("stdout") or ""))
            status = str(public.get("status") or "failed")
            if status != "succeeded":
                return {
                    "status": status if status in {"failed", "timed_out", "cancelled"} else "failed",
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "output_invalid_utf8": bool(public.get("output_invalid_utf8")),
                }
        duration_ms = round((time.monotonic() - started) * 1000)
        invalid_utf8 = bool((last_result.public_dict() if callable(getattr(last_result, "public_dict", None)) else {}).get("output_invalid_utf8")) if last_result else False
        action_id = str(task.get("action_id") or "")
        if action_id.startswith("repository.status.") and len(safe_outputs) == 3:
            branch_raw = safe_outputs[0].strip()
            revision_raw = safe_outputs[1].strip().lower()
            status_lines = [line for line in safe_outputs[2].splitlines() if line and not line.startswith("##")]
            branch = branch_raw if re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", branch_raw) else None
            revision = revision_raw if re.fullmatch(r"[0-9a-f]{7,40}", revision_raw) else None
            emit("仓库状态读取完成。\n")
            return {
                "status": "succeeded", "duration_ms": duration_ms, "output_invalid_utf8": invalid_utf8,
                "branch": branch, "revision": revision, "dirty": bool(status_lines), "changed_count": len(status_lines),
            }
        emit("结构校验通过。\n")
        return {
            "status": "succeeded",
            "duration_ms": duration_ms,
            "output_invalid_utf8": invalid_utf8,
        }

    def shutdown(self) -> None:
        """委托底层 Job 执行器终止仍受控的进程树。"""
        close = getattr(self._executor, "shutdown", None)
        if callable(close):
            close()


__all__ = ["WindowsActionsExecutor", "WindowsActionsUnavailable"]
