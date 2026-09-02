"""阶段 3 波次 2 的任务编排边界。

本模块只负责动作准入后的排队、并发、取消、超时和安全审计关联。执行器是
注入式协议，默认实现不会启动任何外部进程；Windows 执行器须在后续波次中
单独实现并满足 Job Object、环境和路径边界，不能从本模块绕过准入控制。
"""

from __future__ import annotations

import hmac
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Mapping, Protocol

from aikb.audit import AUDIT_SCHEMA_VERSION

from .actions import ActionError, ActionRegistry, ConfirmationTokenService
from .tasks import TERMINAL_STATES, TaskError, TaskStore


class OrchestratorError(ValueError):
    """动作预览不匹配、任务状态不允许或编排参数不安全。"""


class ExecutorProtocol(Protocol):
    """未来受信任执行器的最小同步协议；不得接收任意命令行。"""

    def run(
        self,
        task: Mapping[str, Any],
        emit: Callable[[str | bytes], None],
        cancel_event: threading.Event,
    ) -> Mapping[str, Any] | str | None: ...


class UnavailableExecutor:
    """默认安全占位，不创建进程，只让任务以受控失败结束。"""

    def run(
        self,
        task: Mapping[str, Any],
        emit: Callable[[str | bytes], None],
        cancel_event: threading.Event,
    ) -> Mapping[str, Any]:
        """抛出固定内部错误，由编排器转换为安全 failed 结果。"""
        raise RuntimeError("executor unavailable")


class TaskOrchestrator:
    """以全局和动作并发组限额调度安全任务，并把事实写入 TaskStore。"""

    EXECUTOR_STOP_GRACE_SECONDS = 5.0

    def __init__(
        self,
        workspace_root: Any,
        *,
        registry: ActionRegistry | None = None,
        token_service: ConfirmationTokenService | None = None,
        task_store: TaskStore | None = None,
        executor: ExecutorProtocol | Callable[..., Any] | None = None,
        audit_sink: Callable[[Mapping[str, Any]], Any] | None = None,
        max_concurrency: int = 2,
    ) -> None:
        """绑定 workspace 和注入依赖；初始化会恢复遗留非终态任务。"""
        if max_concurrency < 1:
            raise OrchestratorError("并发限制无效")
        self.registry = registry or ActionRegistry()
        self.tokens = token_service or ConfirmationTokenService()
        self.executor = executor or UnavailableExecutor()
        self.audit_sink = audit_sink
        self.max_concurrency = max_concurrency
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._group_guard = threading.Lock()
        self._group_slots: dict[str, threading.BoundedSemaphore] = {}
        self._jobs_guard = threading.Lock()
        self._jobs: dict[str, tuple[Future[Any], threading.Event]] = {}
        self._shutdown_guard = threading.Lock()
        self._shutdown_started = False
        self._pool = ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="aikb-web-task")
        self.store = task_store or TaskStore(workspace_root, recover=False)
        self._recover_startup_tasks()

    def _recover_startup_tasks(self) -> None:
        """启动时将遗留非终态收敛为 interrupted，并补写原调用终态审计。"""
        for snapshot in list(self.store.list_tasks()):
            if snapshot.get("status") not in {"queued", "running", "cancelling"}:
                continue
            try:
                recovered = self.store.transition(str(snapshot["task_id"]), "interrupted", reason="service_restarted")
                self._audit(record_type="invocation_finished", task=recovered, status="interrupted")
            except (TaskError, KeyError):
                continue

    def _group_slot(self, group: str, limit: int) -> threading.BoundedSemaphore:
        """按静态动作规格缓存并发组信号量，不允许请求覆盖限制。"""
        with self._group_guard:
            return self._group_slots.setdefault(group, threading.BoundedSemaphore(max(1, limit)))

    def _audit(self, *, record_type: str, task: Mapping[str, Any], status: str, **fields: Any) -> None:
        """写入最小安全审计关联；审计故障不改变任务状态机。"""
        if not callable(self.audit_sink):
            return
        record = {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "record_type": record_type,
            "event_id": uuid.uuid4().hex,
            "invocation_id": str(task.get("invocation_id") or task.get("task_id")),
            "target_task_id": str(task.get("task_id")),
            "task_id": str(task.get("task_id")),
            "source": "web",
            "operation": "controlled_action",
            "action_id": str(task.get("action_id") or "") or None,
            # 审计 schema 的 action 是安全摘要对象，不把动作名伪装成原始命令文本。
            "action": {"action_id": str(task.get("action_id") or "")} if task.get("action_id") else None,
            "status": status,
            **fields,
        }
        try:
            self.audit_sink(record)
        except Exception:
            # 审计是旁路记录；不能把底层路径、异常或 traceback 传播到 API。
            return

    def submit(self, *, action_id: str, parameters: Mapping[str, Any], preview_digest: str, confirmation_token: str) -> dict[str, Any]:
        """在 shutdown 互斥区完成令牌消费和入队，避免留下无终态 queued 任务。"""
        with self._shutdown_guard:
            if self._shutdown_started:
                raise OrchestratorError("任务服务已关闭")
            return self._submit(action_id=action_id, parameters=parameters, preview_digest=preview_digest, confirmation_token=confirmation_token)

    def _submit(self, *, action_id: str, parameters: Mapping[str, Any], preview_digest: str, confirmation_token: str) -> dict[str, Any]:
        """校验预览绑定、消费单次令牌、创建 queued 任务并后台调度。"""
        try:
            preview = self.registry.preview(action_id, parameters)
        except ActionError as error:
            raise OrchestratorError("动作或参数无效") from error
        if not isinstance(preview_digest, str) or not hmac.compare_digest(preview["preview_digest"], preview_digest):
            raise OrchestratorError("预览已失效")
        try:
            self.tokens.consume(
                confirmation_token,
                action_id=action_id,
                parameters=preview["parameters"],
                risk_level=preview["risk_level"],
                preview_digest=preview_digest,
            )
        except ActionError as error:
            raise OrchestratorError("确认令牌无效或已消费") from error
        spec = self.registry.get(action_id)
        task = self.store.create_task(
            action_id=action_id,
            parameters=preview["parameters"],
            risk_level=preview["risk_level"],
            effects=preview["effects"],
            timeout_seconds=preview["timeout_seconds"],
            concurrency_group=preview["concurrency_group"],
            preview_digest=preview_digest,
            invocation_id=f"web-{uuid.uuid4().hex}",
        )
        self._audit(record_type="invocation_started", task=task, status="started")
        cancel_event = threading.Event()
        # 先让 worker 等待登记完成，避免极快 fake executor 在 _jobs 写入前结束并留下幽灵记录。
        registered = threading.Event()
        future = self._pool.submit(self._run, task["task_id"], spec, cancel_event, registered)
        with self._jobs_guard:
            self._jobs[task["task_id"]] = (future, cancel_event)
        registered.set()
        return self.store.get_task(task["task_id"])

    def _invoke(
        self, task: Mapping[str, Any], cancel_event: threading.Event,
    ) -> tuple[Any, BaseException | None, threading.Event]:
        """在独立线程执行注入器，以便调度线程能实施超时和取消信号。"""
        done = threading.Event()
        result: dict[str, Any] = {}

        def emit(value: str | bytes) -> None:
            """把执行器输出送入 TaskStore；终态竞态只安全忽略。"""
            try:
                self.store.append_output(str(task["task_id"]), value)
            except TaskError:
                return

        def call() -> None:
            try:
                runner = getattr(self.executor, "run", self.executor)
                result["value"] = runner(task, emit, cancel_event)
            except BaseException as error:  # 由编排层转为安全失败摘要
                result["error"] = error
            finally:
                done.set()

        thread = threading.Thread(target=call, name="aikb-web-executor", daemon=True)
        thread.start()
        timeout = max(1, min(int(task.get("timeout_seconds") or 1), 120))
        if not done.wait(timeout):
            cancel_event.set()
            return None, TimeoutError("task timeout"), done
        return result.get("value"), result.get("error"), done

    def _run(self, task_id: str, spec: Any, cancel_event: threading.Event, registered: threading.Event) -> None:
        """获取并发槽、执行动作、写入终态和结束审计；不暴露底层异常。"""
        registered.wait()
        group_slot = self._group_slot(spec.concurrency_group, spec.concurrency_limit)
        acquired_global = False
        acquired_group = False
        try:
            while not acquired_global:
                if self.store.get_task(task_id).get("status") != "queued":
                    return
                acquired_global = self._slots.acquire(timeout=0.1)
            while not acquired_group:
                if self.store.get_task(task_id).get("status") != "queued":
                    return
                acquired_group = group_slot.acquire(timeout=0.1)
            current = self.store.get_task(task_id)
            if current.get("status") != "queued":
                return
            current = self.store.transition(task_id, "running")
            value, error, execution_done = self._invoke(current, cancel_event)
            latest = self.store.get_task(task_id)
            # _invoke 在超时时也会置 cancel_event；先看任务状态区分用户取消和超时信号。
            if latest.get("status") == "cancelling":
                current = self.store.finish(task_id, status="cancelled", result={"outcome": "cancelled"})
            elif error is not None and isinstance(error, TimeoutError):
                current = self.store.finish(task_id, status="timed_out", result={"outcome": "timeout"})
            elif cancel_event.is_set():
                current = self.store.finish(task_id, status="cancelled", result={"outcome": "cancelled"})
            elif error is not None:
                current = self.store.finish(task_id, status="failed", result={"outcome": "executor_failed"})
            else:
                reported = value.get("status") if isinstance(value, Mapping) else None
                target = reported if reported in {"succeeded", "failed", "timed_out", "cancelled"} else "failed"
                if target == "cancelled" and latest.get("status") == "running":
                    self.store.transition(task_id, "cancelling", reason="executor_cancelled")
                current = self.store.finish(task_id, status=target, result=value if target == reported else {"outcome": "invalid_executor_result"})
            # 正式 Windows 执行器会在超时后终止 Job；这里再给有限收敛时间。
            # 注入式异常执行器不能永久占住线程池和任务槽。
            if isinstance(error, TimeoutError):
                execution_done.wait(self.EXECUTOR_STOP_GRACE_SECONDS)
            self._audit(record_type="invocation_finished", task=current, status=str(current.get("status")))
        except (TaskError, KeyError):
            return
        except Exception:
            # 未预期执行器/编排错误只收敛为安全失败；底层异常不进入任务或 API。
            try:
                latest = self.store.get_task(task_id)
                if latest.get("status") in {"running", "cancelling"}:
                    failed = self.store.finish(task_id, status="failed", result={"outcome": "orchestrator_failed"})
                    self._audit(record_type="invocation_finished", task=failed, status="failed")
            except Exception:
                return
        finally:
            if acquired_group:
                group_slot.release()
            if acquired_global:
                self._slots.release()
            with self._jobs_guard:
                self._jobs.pop(task_id, None)

    def cancel(self, task_id: str) -> dict[str, Any]:
        """发出取消请求并关联独立审计事件；queued 任务立即终止。"""
        before = self.store.get_task(task_id)
        if before.get("status") in TERMINAL_STATES:
            # 终态重复取消是幂等读取，不重复制造审计调用。
            return before
        task = self.store.cancel(task_id)
        with self._jobs_guard:
            job = self._jobs.get(task_id)
        if job:
            job[1].set()
        cancel_task = dict(task)
        cancel_task["invocation_id"] = f"web-cancel-{uuid.uuid4().hex}"
        self._audit(record_type="invocation_started", task=cancel_task, status="started", operation="task_cancel", target_task_id=task_id)
        self._audit(record_type="invocation_finished", task=cancel_task, status="cancelled", operation="task_cancel", target_task_id=task_id)
        if task.get("status") == "cancelled":
            # queued 任务不会进入 worker，因此由取消请求补齐原调用的 finished 事件。
            self._audit(record_type="invocation_finished", task=task, status="cancelled")
        return self.store.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        """读取任务安全投影。"""
        return self.store.get_task(task_id)

    def list_tasks(self) -> list[dict[str, Any]]:
        """读取任务安全投影列表。"""
        return self.store.list_tasks()

    def events(self, task_id: str) -> list[dict[str, Any]]:
        """兼容旧调用方读取全部事件；事实校验由 TaskStore 公开接口完成。"""
        return self.store.read_all_events(task_id)

    def events_after(self, task_id: str, last_event_id: int = 0):
        """读取任务游标后的增量事件，并保留 replay_reset 元数据。"""
        return self.store.events_after(task_id, last_event_id)

    def wait_for_events(self, task_id: str, last_event_id: int, timeout: float = 15.0) -> bool:
        """等待任务事实追加；供 SSE 在无事件期间阻塞而非忙轮询。"""
        return self.store.wait_for_events(task_id, last_event_id, timeout)

    def shutdown(self) -> None:
        """广播取消、关闭注入式执行器并等待调度线程，避免服务退出遗留任务。"""
        with self._shutdown_guard:
            if self._shutdown_started:
                return
            self._shutdown_started = True
        with self._jobs_guard:
            jobs = list(self._jobs.values())
        for _, cancel_event in jobs:
            cancel_event.set()
        close_executor = getattr(self.executor, "shutdown", None)
        if callable(close_executor):
            try:
                close_executor()
            except Exception:
                pass
        self._pool.shutdown(wait=True, cancel_futures=True)
