"""阶段 3 波次 1 的本地任务事实源和安全状态机。

这里仅负责任务记录、状态转换、崩溃恢复和输出预算，绝不创建子进程、调用
Shell/Git、解释命令或接收 API 请求。未来执行器必须在更高层显式调用这些
原子能力，并自行满足 Windows Job Object 等阶段 3 后续前置条件。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


class TaskError(ValueError):
    """任务不存在、状态转换非法或持久化数据不符合安全边界。"""


TASK_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
TERMINAL_STATES = frozenset({"succeeded", "failed", "timed_out", "cancelled", "interrupted"})
TRANSITIONS = {
    "queued": frozenset({"running", "cancelled", "interrupted"}),
    "running": frozenset({"cancelling", "succeeded", "failed", "timed_out", "interrupted"}),
    "cancelling": frozenset({"cancelled", "interrupted"}),
}
MAX_OUTPUT_CHUNK_BYTES = 8 * 1024
MAX_OUTPUT_LINE_BYTES = 4 * 1024
# JSONL 事件还包含 event_id/type/timestamp 等元数据；为使物理行也不超过
# 4 KiB，正文块留出固定头部预算。正文仍远低于 8 KiB 的事件块上限。
MAX_OUTPUT_EVENT_TEXT_BYTES = MAX_OUTPUT_LINE_BYTES - 256
MAX_OUTPUT_TOTAL_BYTES = 2 * 1024 * 1024
SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|password|passwd|secret|private[_-]?key)\s*[:=]\s*([^\s,;]+)"
)
TOKEN_PATTERN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,})\b")
FILE_URI_PATTERN = re.compile(r"(?i)\bfile:/+(?:[a-z]:[\\/]|/)?[^\s<>\"']+")
PATH_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9_:/])/(?:"
    r"(?:users|home|private|var|tmp|workspace|mnt|opt|srv|root|usr|etc|bin|dev|proc|sys|run|lib|sbin|boot|media)(?:/[^\s<>\"']*)*"
    r"|(?:[^\s<>\"'/]+(?:/[^\s<>\"'/]+)*)+)"
    r"|(?<![a-z0-9_])(?:[a-z]:[\\/](?![\\/])[^\s<>\"']*|\\\\[^\s<>\"']+)"
)


def _safe_text(value: Any, limit: int = 4000) -> str:
    """脱敏控制字符、密钥和跨平台绝对路径，并限制公共文本长度。"""
    text = str(value if value is not None else "").replace("\x00", "")
    text = SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = TOKEN_PATTERN.sub("[REDACTED]", text)
    # ``file:///...`` 的首个斜杠前有冒号，不能依赖普通绝对路径正则命中。
    text = FILE_URI_PATTERN.sub("[LOCAL_PATH]", text)
    text = PATH_PATTERN.sub("[LOCAL_PATH]", text)
    text = "".join(
        char for char in text
        if char in "\n\r\t" or (ord(char) >= 32 and not 0xD800 <= ord(char) <= 0xDFFF)
    )
    return text[:limit]


def _canonical(value: Any) -> str:
    """生成 snapshot/event 使用的稳定 JSON。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_json(value: Any, depth: int = 0) -> Any:
    """递归构造无路径、有限深度的参数/结果摘要，防止未来动作扩展越界。"""
    if depth > 5:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:20]:
            safe_key = _safe_text(key, 120)
            normalized = re.sub(r"[^a-z0-9]+", "_", safe_key.lower()).strip("_")
            if any(part in normalized for part in ("token", "secret", "password", "passwd", "authorization", "cookie", "private_key", "api_key")):
                result[safe_key] = "[REDACTED]"
            elif normalized in {"command", "commands", "cmd", "argv", "environment", "env", "stdin", "stdout", "stderr", "payload", "raw", "traceback", "diagnostic"}:
                result[safe_key] = "[REDACTED]"
            elif normalized in {"path", "source_path", "file_path", "absolute_path"} or normalized.endswith(("_path", "_root", "_directory")):
                result[safe_key] = "[LOCAL_PATH]"
            else:
                result[safe_key] = _safe_json(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_json(item, depth + 1) for item in list(value)[:50]]
    return _safe_text(value, 4000)


class TaskStore:
    """管理 workspace/runtime/web/tasks 下的追加事实源和可重建快照。"""

    def __init__(self, workspace_root: Path | str | Any, *, clock: Any | None = None, recover: bool = True):
        """绑定临时或正式 workspace；不启动执行器，初始化只扫描本地快照。"""
        actual_root = getattr(workspace_root, "workspace_root", workspace_root)
        self.workspace_root = Path(actual_root).resolve()
        self.tasks_root = self.workspace_root / "runtime" / "web" / "tasks"
        self.tasks_root.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._task_dirs: dict[str, Path] = {}
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}
        self._refresh_task_dirs()
        if recover:
            self.recover_interrupted()

    def _refresh_task_dirs(self) -> None:
        """从固定年月布局发现合法任务目录，不接受客户端指定物理路径。"""
        # events.jsonl 才是事实源，因此即使 snapshot 丢失也必须发现任务目录。
        for task_path in self.tasks_root.glob("*/*/*"):
            task_id = task_path.name
            if TASK_ID_PATTERN.fullmatch(task_id) and (
                (task_path / "events.jsonl").is_file() or (task_path / "snapshot.json").is_file()
            ):
                self._task_dirs[task_id] = task_path

    def _task_dir(self, task_id: str) -> Path:
        """按服务生成的 ID 定位任务目录；非法 ID 不参与路径拼接。"""
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise TaskError("task_id 无效")
        path = self._task_dirs.get(task_id)
        if path is None:
            self._refresh_task_dirs()
            path = self._task_dirs.get(task_id)
        if path is None:
            raise KeyError("任务不存在")
        return path

    def _lock_for(self, task_id: str) -> threading.RLock:
        """返回任务级可重入锁，协调同进程内 event+snapshot 的原子顺序。"""
        with self._locks_guard:
            return self._locks.setdefault(task_id, threading.RLock())

    @staticmethod
    def _snapshot_fields(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """复制可由事件重建的安全快照字段，排除仅用于内部校验的临时数据。"""
        return json.loads(_canonical(dict(snapshot)))

    def _read_events(self, task_id: str) -> list[dict[str, Any]]:
        """读取并校验 JSONL 严格递增事件；事实损坏不会静默伪造状态。"""
        path = self._task_dir(task_id) / "events.jsonl"
        if not path.is_file():
            raise TaskError("任务事实源不可用")
        events: list[dict[str, Any]] = []
        previous_id = 0
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                event_id = value.get("event_id") if isinstance(value, dict) else None
                if not isinstance(value, dict) or not isinstance(event_id, int) or event_id != previous_id + 1:
                    raise TaskError("任务事实源不可用")
                previous_id = event_id
                events.append(value)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise TaskError("任务事实源不可用") from error
        if not events or events[0].get("type") != "snapshot" or not isinstance(events[0].get("snapshot"), dict):
            raise TaskError("任务事实源缺少 snapshot 基线")
        return events

    def _replay_events(self, task_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        """严格按事件顺序重建当前安全投影，不信任旧 snapshot 的状态字段。"""
        state = self._snapshot_fields(events[0]["snapshot"])
        if state.get("task_id") != task_id:
            raise TaskError("任务事实源不可用")
        state["last_event_id"] = 1
        for event in events[1:]:
            event_type = event.get("type")
            if event_type == "status":
                state["status"] = _safe_text(event.get("status"), 40)
                if event.get("reason"):
                    state["last_reason"] = _safe_text(event.get("reason"), 300)
            elif event_type == "output":
                # 事件正文的真实上限由 JSONL 物理行检查保证；回放不能使用更小的
                # 固定字符上限，否则重启后会悄悄丢掉合法的 4 KiB 行尾内容。
                segment = _safe_text(event.get("text"), MAX_OUTPUT_CHUNK_BYTES)
                state["output"] = self._bounded_output(str(state.get("output") or ""), segment)
                state["output_bytes"] = min(MAX_OUTPUT_TOTAL_BYTES, int(state.get("output_bytes") or 0) + len(segment.encode("utf-8")))
            elif event_type == "output_truncated":
                state["output_truncated"] = True
            elif event_type == "result":
                state["result"] = _safe_json(event.get("result"))
            else:
                raise TaskError("任务事实源包含未知事件")
            state["updated_at"] = event.get("timestamp")
            state["last_event_id"] = event["event_id"]
        return state

    @staticmethod
    def _bounded_output(existing: str, segment: str) -> str:
        """按 UTF-8 字节而非 Python 字符数限制投影正文，避免中文导致预算膨胀。"""
        data = (existing + segment).encode("utf-8")
        if len(data) <= MAX_OUTPUT_TOTAL_BYTES:
            return existing + segment
        return data[-MAX_OUTPUT_TOTAL_BYTES:].decode("utf-8", errors="ignore")

    def _read_snapshot(self, task_id: str) -> dict[str, Any]:
        """以 events 回放为权威读取安全 snapshot，并修复缺失、损坏或落后的投影。"""
        # 读也与写共用 RLock，避免观察到“事件已追加、snapshot 尚未替换”的中间态。
        with self._lock_for(task_id):
            events = self._read_events(task_id)
            rebuilt = self._replay_events(task_id, events)
            path = self._task_dir(task_id) / "snapshot.json"
            existing: dict[str, Any] | None = None
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict) and value.get("task_id") == task_id:
                    existing = value
            except (OSError, UnicodeError, json.JSONDecodeError):
                existing = None
            if existing != rebuilt:
                self._write_snapshot(rebuilt)
            return rebuilt

    def _append_event(self, task_id: str, state: dict[str, Any], event_type: str, **fields: Any) -> dict[str, Any]:
        """在调用方持有任务锁时追加严格递增事实事件。"""
        task_dir = self._task_dir(task_id)
        seq = int(state.get("last_event_id") or 0) + 1
        event = {"event_id": seq, "type": event_type, "timestamp": self._clock().isoformat(timespec="milliseconds"), **fields}
        line = _canonical(event) + "\n"
        if event_type == "output":
            # 物理 JSONL 行限制必须在序列化后检查，引用、反斜杠和换行都会膨胀。
            raw_text = str(fields.get("text") or "")
            if len(raw_text.encode("utf-8")) > MAX_OUTPUT_CHUNK_BYTES or len(line.encode("utf-8")) > MAX_OUTPUT_LINE_BYTES:
                raise TaskError("任务输出事件超过行预算")
        try:
            with (task_dir / "events.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, UnicodeError) as error:
            raise TaskError("任务事件写入失败") from error
        state["last_event_id"] = seq
        state["updated_at"] = event["timestamp"]
        return event

    def _write_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        """原子替换当前投影，避免浏览器读取半写 JSON。"""
        task_dir = self._task_dir(str(snapshot["task_id"]))
        content = _canonical(dict(snapshot)) + "\n"
        temp_name: str | None = None
        try:
            handle, temp_name = tempfile.mkstemp(prefix="snapshot-", suffix=".tmp", dir=task_dir)
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_name, task_dir / "snapshot.json")
            temp_name = None
        except (OSError, UnicodeError) as error:
            raise TaskError("任务快照写入失败") from error
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

    def create_task(
        self, *, action_id: str, parameters: Mapping[str, Any], risk_level: str, effects: list[str] | tuple[str, ...],
        timeout_seconds: int, concurrency_group: str, preview_digest: str, invocation_id: str | None = None,
    ) -> dict[str, Any]:
        """创建 queued 任务并写入首个事实事件；不执行动作。"""
        task_id = uuid.uuid4().hex
        now = self._clock()
        directory = self.tasks_root / f"{now:%Y}" / f"{now:%m}" / task_id
        directory.mkdir(parents=True, exist_ok=False)
        self._task_dirs[task_id] = directory
        snapshot = {
            "task_id": task_id, "action_id": _safe_text(action_id, 120), "parameters": _safe_json(dict(parameters)),
            "risk_level": _safe_text(risk_level, 40), "effects": [_safe_text(item, 200) for item in effects[:20]],
            "timeout_seconds": max(1, min(int(timeout_seconds), 120)), "concurrency_group": _safe_text(concurrency_group, 120),
            "preview_digest": _safe_text(preview_digest, 128), "invocation_id": _safe_text(invocation_id, 160) if invocation_id else None,
            "status": "queued", "created_at": now.isoformat(timespec="milliseconds"), "updated_at": now.isoformat(timespec="milliseconds"),
            "progress": None, "output": "", "output_bytes": 0, "output_truncated": False, "result": None, "last_event_id": 0,
        }
        # 先落首个事实事件，再生成派生 snapshot；写入失败时不会留下貌似可用的空投影。
        with self._lock_for(task_id):
            self._append_event(task_id, snapshot, "snapshot", snapshot=self._snapshot_fields(snapshot))
            # 首事件携带完整安全基线；此后 snapshot 即使丢失也能从 events 重建。
            self._write_snapshot(snapshot)
        return dict(snapshot)

    def get_task(self, task_id: str) -> dict[str, Any]:
        """读取不含物理路径、PID、命令和原始异常的任务投影。"""
        return self._read_snapshot(task_id)

    def list_tasks(self) -> list[dict[str, Any]]:
        """按更新时间倒序列出本地任务安全快照。"""
        self._refresh_task_dirs()
        values = []
        for task_id in list(self._task_dirs):
            try:
                values.append(self._read_snapshot(task_id))
            except (KeyError, TaskError):
                continue
        return sorted(values, key=lambda item: (str(item.get("updated_at") or ""), str(item.get("task_id") or "")), reverse=True)

    def transition(self, task_id: str, status: str, *, reason: str | None = None) -> dict[str, Any]:
        """执行严格状态转换；终态不可逆，非法边界不会写入事实源。"""
        with self._lock_for(task_id):
            snapshot = self._read_snapshot(task_id)
            current = str(snapshot.get("status") or "")
            if current in TERMINAL_STATES:
                if current == status:
                    return snapshot
                raise TaskError("终态任务不可再次转换")
            if status not in TRANSITIONS.get(current, frozenset()):
                raise TaskError("任务状态转换无效")
            self._append_event(task_id, snapshot, "status", status=status, reason=_safe_text(reason, 300) if reason else None)
            snapshot["status"] = status
            self._write_snapshot(snapshot)
            return snapshot

    def cancel(self, task_id: str) -> dict[str, Any]:
        """取消任务；queued 直接终止，running 先进入 cancelling，终态幂等。"""
        with self._lock_for(task_id):
            snapshot = self._read_snapshot(task_id)
            status = snapshot.get("status")
            if status in TERMINAL_STATES:
                return snapshot
            if status == "queued":
                return self.transition(task_id, "cancelled", reason="user_cancelled")
            if status == "running":
                return self.transition(task_id, "cancelling", reason="user_cancelled")
            return snapshot

    def recover_interrupted(self) -> list[str]:
        """把服务重启时所有非终态历史任务收敛为 interrupted，不猜测旧进程。"""
        recovered: list[str] = []
        for snapshot in list(self.list_tasks()):
            task_id = str(snapshot.get("task_id") or "")
            if snapshot.get("status") in {"queued", "running", "cancelling"}:
                self.transition(task_id, "interrupted", reason="service_restarted")
                recovered.append(task_id)
        return recovered

    def append_output(self, task_id: str, output: str | bytes) -> list[dict[str, Any]]:
        """按 UTF-8 字节预算追加脱敏输出，并在超限时只写一次截断事件。"""
        with self._lock_for(task_id):
            snapshot = self._read_snapshot(task_id)
            if snapshot.get("status") in TERMINAL_STATES:
                raise TaskError("终态任务不可追加输出")
            if isinstance(output, bytes):
                text = output.decode("utf-8", errors="replace").replace("\ufffd", "[INVALID_UTF8]")
            else:
                text = str(output)
            text = _safe_text(text, MAX_OUTPUT_TOTAL_BYTES)
            events: list[dict[str, Any]] = []
            used = int(snapshot.get("output_bytes") or 0)
            budget_exceeded = False
            # keepends 保留可读换行；按字符累积 UTF-8 字节，避免切断中文字符。
            lines = text.splitlines(keepends=True) or ([text] if text else [])
            for line in lines:
                segment_chars: list[str] = []
                segment_bytes = 0
                escaped_segment_bytes = 0
                for character in line:
                    character_bytes = len(character.encode("utf-8"))
                    # json.dumps 的字符串片段长度等于 _canonical(event) 中的实际正文长度，
                    # 因而引号、反斜杠、换行等转义膨胀会真正计入 4 KiB 物理行预算。
                    escaped_bytes = len(json.dumps(character, ensure_ascii=False)[1:-1].encode("utf-8"))
                    base_event = {
                        "event_id": int(snapshot.get("last_event_id") or 0) + 1,
                        "type": "output",
                        "timestamp": self._clock().isoformat(timespec="milliseconds"),
                        "text": "",
                    }
                    base_line_bytes = len((_canonical(base_event) + "\n").encode("utf-8"))
                    if segment_chars and (
                        segment_bytes + character_bytes > MAX_OUTPUT_CHUNK_BYTES
                        or base_line_bytes + escaped_segment_bytes + escaped_bytes > MAX_OUTPUT_LINE_BYTES
                    ):
                        segment = "".join(segment_chars)
                        segment_size = len(segment.encode("utf-8"))
                        if used + segment_size > MAX_OUTPUT_TOTAL_BYTES:
                            budget_exceeded = True
                            break
                        event = self._append_event(task_id, snapshot, "output", text=segment)
                        events.append(event)
                        used += segment_size
                        snapshot["output"] = self._bounded_output(str(snapshot.get("output") or ""), segment)
                        snapshot["output_bytes"] = used
                        segment_chars, segment_bytes, escaped_segment_bytes = [], 0, 0
                    segment_chars.append(character)
                    segment_bytes += character_bytes
                    escaped_segment_bytes += escaped_bytes
                if segment_chars and not snapshot.get("output_truncated") and not budget_exceeded:
                    segment = "".join(segment_chars)
                    segment_size = len(segment.encode("utf-8"))
                    if used + segment_size <= MAX_OUTPUT_TOTAL_BYTES:
                        event = self._append_event(task_id, snapshot, "output", text=segment)
                        events.append(event)
                        used += segment_size
                        snapshot["output"] = self._bounded_output(str(snapshot.get("output") or ""), segment)
                        snapshot["output_bytes"] = used
                    else:
                        budget_exceeded = True
                if budget_exceeded or used >= MAX_OUTPUT_TOTAL_BYTES:
                    if not snapshot.get("output_truncated"):
                        event = self._append_event(task_id, snapshot, "output_truncated", reason="output_budget_exceeded")
                        events.append(event)
                        snapshot["output_truncated"] = True
                    break
            if events:
                self._write_snapshot(snapshot)
            return events

    def finish(self, task_id: str, *, status: str, result: Mapping[str, Any] | str | None = None) -> dict[str, Any]:
        """写入终态安全结果；结果不会保存完整异常、命令或诊断正文。"""
        if status not in TERMINAL_STATES - {"interrupted"}:
            raise TaskError("终态结果无效")
        with self._lock_for(task_id):
            snapshot = self._read_snapshot(task_id)
            current = snapshot.get("status")
            if current not in {"running", "cancelling"}:
                raise TaskError("只有运行中任务可以完成")
            if current == "cancelling" and status != "cancelled":
                raise TaskError("取消中的任务只能进入 cancelled")
            if current == "running" and status == "cancelled":
                raise TaskError("运行中任务必须先进入 cancelling")
            snapshot = self.transition(task_id, status)
            safe_result = _safe_json(result)
            self._append_event(task_id, snapshot, "result", result=safe_result)
            snapshot["result"] = safe_result
            self._write_snapshot(snapshot)
            return snapshot
