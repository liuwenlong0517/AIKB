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
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


class TaskError(ValueError):
    """任务不存在、状态转换非法或持久化数据不符合安全边界。"""


@dataclass
class _TaskFacts:
    """同进程任务事实缓存；仅缓存安全投影和最近事件，事实仍由 JSONL 保存。"""

    snapshot: dict[str, Any]
    events_tail: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=512))
    last_event_id: int = 0
    file_offset: int = 0
    file_identity: tuple[int, int] = (0, 0)
    modified_ns: int = 0


@dataclass
class _ConditionEntry:
    """任务等待条件及活跃引用数；无 waiter 时可安全从注册表移除。"""

    condition: threading.Condition
    waiters: int = 0


@dataclass(frozen=True)
class TaskEventsResult:
    """事件增量读取结果；replay_reset 时客户端必须以 snapshot 重新建立投影。"""

    events: list[dict[str, Any]]
    latest_event_id: int
    snapshot: dict[str, Any]
    replay_reset: bool = False


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
# 单个输出最多 2 MiB；仅保留少量最近投影，避免历史任务全部常驻内存。
MAX_TASK_FACTS = 64
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
        self._facts_guard = threading.RLock()
        self._facts: OrderedDict[str, _TaskFacts] = OrderedDict()
        self._conditions: dict[str, _ConditionEntry] = {}
        # 列表页只读取有界字段的内存索引；完整事实回放仍留给详情、恢复与显式兼容入口。
        self._snapshot_index: dict[str, dict[str, Any]] = {}
        self._refresh_task_dirs()
        self._load_snapshot_index()
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

    @staticmethod
    def _list_projection(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """提取列表所需的小型安全字段，避免把输出、结果和参数常驻列表索引。"""

        allowed = (
            "task_id", "action_id", "risk_level", "status", "created_at", "updated_at",
            "timeout_seconds", "output_truncated", "last_event_id", "progress",
        )
        return {key: _safe_json(snapshot.get(key)) for key in allowed if key in snapshot}

    def _load_snapshot_index(self) -> None:
        """启动时读取一次派生快照；普通列表请求不再遍历任务目录或回放事实。"""

        loaded: dict[str, dict[str, Any]] = {}
        for task_id, task_dir in list(self._task_dirs.items()):
            try:
                snapshot = json.loads((task_dir / "snapshot.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(snapshot, dict) and snapshot.get("task_id") == task_id:
                loaded[task_id] = self._list_projection(snapshot)
        with self._facts_guard:
            self._snapshot_index = loaded

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

    def _acquire_condition(self, task_id: str) -> _ConditionEntry:
        """返回任务变更通知器；通知与任务锁绑定，避免错过追加事件。"""
        with self._facts_guard:
            entry = self._conditions.get(task_id)
            if entry is None:
                with self._locks_guard:
                    task_lock = self._locks.setdefault(task_id, threading.RLock())
                entry = _ConditionEntry(threading.Condition(task_lock))
                self._conditions[task_id] = entry
            entry.waiters += 1
            return entry

    def _cached_facts(self, task_id: str) -> _TaskFacts | None:
        """在独立缓存锁下取得并提升 LRU 项；调用方仍须持有任务锁。"""
        with self._facts_guard:
            facts = self._facts.get(task_id)
            if facts is not None:
                self._facts.move_to_end(task_id)
            return facts

    def _remember_facts(self, task_id: str, facts: _TaskFacts) -> None:
        """插入事实缓存并有界淘汰；字典操作不依赖其他任务锁。"""
        with self._facts_guard:
            self._facts[task_id] = facts
            self._facts.move_to_end(task_id)
            while len(self._facts) > MAX_TASK_FACTS:
                self._facts.popitem(last=False)

    @staticmethod
    def _snapshot_fields(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """复制可由事件重建的安全快照字段，排除仅用于内部校验的临时数据。"""
        return json.loads(_canonical(dict(snapshot)))

    def _read_events_file(self, task_id: str) -> tuple[list[dict[str, Any]], int, os.stat_result]:
        """首次读取并严格校验事实文件，同时返回可继续读取的字节游标。"""
        path = self._task_dir(task_id) / "events.jsonl"
        if not path.is_file():
            raise TaskError("任务事实源不可用")
        events: list[dict[str, Any]] = []
        previous_id = 0
        try:
            with path.open("rb") as handle:
                opened_stat = os.fstat(handle.fileno())
                while True:
                    raw_line = handle.readline()
                    if not raw_line:
                        break
                    if not raw_line.endswith(b"\n"):
                        raise TaskError("任务事实源不可用")
                    line = raw_line.decode("utf-8")
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    event_id = value.get("event_id") if isinstance(value, dict) else None
                    if not isinstance(value, dict) or not isinstance(event_id, int) or event_id != previous_id + 1:
                        raise TaskError("任务事实源不可用")
                    previous_id = event_id
                    events.append(value)
                offset = handle.tell()
            stat = path.stat()
            # 事实源在读取期间被替换或追加时，宁可下次重读，也不返回可能缺失的事件。
            opened_identity = (getattr(opened_stat, "st_dev", 0), getattr(opened_stat, "st_ino", 0))
            path_identity = (getattr(stat, "st_dev", 0), getattr(stat, "st_ino", 0))
            if stat.st_size != offset or stat.st_size != opened_stat.st_size or path_identity != opened_identity:
                raise TaskError("任务事实源发生并发变更")
        except TaskError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise TaskError("任务事实源不可用") from error
        if not events or events[0].get("type") != "snapshot" or not isinstance(events[0].get("snapshot"), dict):
            raise TaskError("任务事实源缺少 snapshot 基线")
        return events, offset, stat

    def _read_events(self, task_id: str) -> list[dict[str, Any]]:
        """兼容内部恢复代码的完整读取入口；公开读取统一使用 events_after。"""
        events, _, _ = self._read_events_file(task_id)
        return events

    def read_all_events(self, task_id: str) -> list[dict[str, Any]]:
        """显式读取完整事件历史；仅供低频兼容/验收调用，不得用于 SSE 热路径。"""
        with self._lock_for(task_id):
            return [dict(event) for event in self._read_events(task_id)]

    def _apply_event(self, state: dict[str, Any], event: Mapping[str, Any]) -> None:
        """把单个已校验事件折叠到安全投影，供全量和增量路径共享。"""
        event_type = event.get("type")
        if event_type == "status":
            state["status"] = _safe_text(event.get("status"), 40)
            if event.get("reason"):
                state["last_reason"] = _safe_text(event.get("reason"), 300)
        elif event_type == "output":
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

    def _new_facts(self, task_id: str) -> tuple[_TaskFacts, list[dict[str, Any]]]:
        """从事实源建立一次严格缓存；完整事件只由当前调用栈直接消费。"""
        events, offset, stat = self._read_events_file(task_id)
        rebuilt = self._replay_events(task_id, events)
        facts = _TaskFacts(
            snapshot=rebuilt,
            events_tail=deque(events[-512:], maxlen=512),
            last_event_id=events[-1]["event_id"],
            file_offset=offset,
            file_identity=(getattr(stat, "st_dev", 0), getattr(stat, "st_ino", 0)),
            modified_ns=getattr(stat, "st_mtime_ns", 0),
        )
        self._remember_facts(task_id, facts)
        return facts, events

    def _read_incremental(self, task_id: str, facts: _TaskFacts, stat: os.stat_result) -> list[dict[str, Any]]:
        """从已校验字节游标读取追加事件；任何断裂都 fail-closed。"""
        path = self._task_dir(task_id) / "events.jsonl"
        pending: list[dict[str, Any]] = []
        previous_id = facts.last_event_id
        try:
            with path.open("rb") as handle:
                handle.seek(facts.file_offset)
                while True:
                    raw_line = handle.readline()
                    if not raw_line:
                        break
                    if not raw_line.endswith(b"\n"):
                        raise TaskError("任务事实源不可用")
                    line = raw_line.decode("utf-8")
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    event_id = value.get("event_id") if isinstance(value, dict) else None
                    if not isinstance(value, dict) or not isinstance(event_id, int) or event_id != previous_id + 1:
                        raise TaskError("任务事实源不可用")
                    # 先校验整个追加批次，再改变缓存，避免半批次污染后续读取。
                    pending.append(value)
                    previous_id = event_id
                offset = handle.tell()
            current_stat = path.stat()
            current_identity = (getattr(current_stat, "st_dev", 0), getattr(current_stat, "st_ino", 0))
            if current_stat.st_size != offset or current_identity != facts.file_identity:
                raise TaskError("任务事实源发生并发变更")
        except TaskError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise TaskError("任务事实源不可用") from error
        probe = dict(facts.snapshot)
        for event in pending:
            self._apply_event(probe, event)
        for event in pending:
            facts.events_tail.append(event)
        facts.snapshot = probe
        facts.last_event_id = previous_id
        facts.file_offset = offset
        facts.modified_ns = getattr(current_stat, "st_mtime_ns", getattr(stat, "st_mtime_ns", 0))
        return pending

    def _ensure_facts(self, task_id: str) -> tuple[_TaskFacts, bool, list[dict[str, Any]] | None]:
        """返回缓存并检测文件代际/截断；reset 标记要求 SSE 发送 replay_reset。"""
        path = self._task_dir(task_id) / "events.jsonl"
        try:
            stat = path.stat()
        except OSError as error:
            raise TaskError("任务事实源不可用") from error
        facts = self._cached_facts(task_id)
        if facts is None:
            facts, initial = self._new_facts(task_id)
            return facts, False, initial
        identity = (getattr(stat, "st_dev", 0), getattr(stat, "st_ino", 0))
        generation_changed = identity != facts.file_identity or stat.st_size < facts.file_offset
        same_size_rewritten = stat.st_size == facts.file_offset and getattr(stat, "st_mtime_ns", 0) != facts.modified_ns
        if generation_changed or same_size_rewritten:
            facts, initial = self._new_facts(task_id)
            return facts, True, initial
        if stat.st_size > facts.file_offset:
            self._read_incremental(task_id, facts, stat)
        return facts, False, None

    def _replay_events(self, task_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        """严格按事件顺序重建当前安全投影，不信任旧 snapshot 的状态字段。"""
        state = self._snapshot_fields(events[0]["snapshot"])
        if state.get("task_id") != task_id:
            raise TaskError("任务事实源不可用")
        state["last_event_id"] = 1
        for event in events[1:]:
            self._apply_event(state, event)
        return state

    @staticmethod
    def _bounded_output(existing: str, segment: str) -> str:
        """按 UTF-8 字节而非 Python 字符数限制投影正文，避免中文导致预算膨胀。"""
        data = (existing + segment).encode("utf-8")
        if len(data) <= MAX_OUTPUT_TOTAL_BYTES:
            return existing + segment
        return data[-MAX_OUTPUT_TOTAL_BYTES:].decode("utf-8", errors="ignore")

    def _read_snapshot(self, task_id: str) -> dict[str, Any]:
        """返回同进程缓存投影；首次或事实游标异常时才严格回放 JSONL。"""
        with self._lock_for(task_id):
            facts, _, _ = self._ensure_facts(task_id)
            # snapshot 是派生物；仅校验/修复它，不以它替代事实源，也不触发 JSONL 重放。
            snapshot_path = self._task_dir(task_id) / "snapshot.json"
            try:
                persisted = json.loads(snapshot_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                persisted = None
            if persisted != facts.snapshot:
                self._write_snapshot(facts.snapshot)
            return self._snapshot_fields(facts.snapshot)

    def events_after(self, task_id: str, last_event_id: int = 0) -> TaskEventsResult:
        """读取游标后的事件；仅首次/游标落出缓存窗口时读取历史 JSONL。"""
        if not isinstance(last_event_id, int) or last_event_id < 0:
            raise TaskError("Last-Event-ID 无效")
        with self._lock_for(task_id):
            facts, reset, initial = self._ensure_facts(task_id)
            if reset:
                # 代际变化后旧游标不可解释，调用方必须以当前完整投影重置。
                return TaskEventsResult([], facts.last_event_id, self._snapshot_fields(facts.snapshot), True)
            if last_event_id > facts.last_event_id:
                return TaskEventsResult([], facts.last_event_id, self._snapshot_fields(facts.snapshot), True)
            if initial is not None:
                # 新建缓存也只保留 512 条尾部；大历史的旧游标必须以完整快照重置。
                earliest = initial[-512]["event_id"] if len(initial) > 512 else initial[0]["event_id"]
                if last_event_id < earliest - 1:
                    return TaskEventsResult(
                        [], facts.last_event_id, self._snapshot_fields(facts.snapshot), True,
                    )
                return TaskEventsResult(
                    [dict(event) for event in initial if event["event_id"] > last_event_id],
                    facts.last_event_id,
                    self._snapshot_fields(facts.snapshot),
                )
            earliest = facts.events_tail[0]["event_id"] if facts.events_tail else facts.last_event_id + 1
            if last_event_id < earliest - 1:
                # 已有缓存时不再次扫描历史；快照已经包含完整安全投影。
                return TaskEventsResult(
                    [],
                    facts.last_event_id,
                    self._snapshot_fields(facts.snapshot),
                    True,
                )
            return TaskEventsResult(
                [dict(event) for event in facts.events_tail if event["event_id"] > last_event_id],
                facts.last_event_id,
                self._snapshot_fields(facts.snapshot),
            )

    def wait_for_events(self, task_id: str, last_event_id: int, timeout: float = 15.0) -> bool:
        """等待事实追加或超时；通知由写入路径发出，不进行固定间隔忙轮询。"""
        entry = self._acquire_condition(task_id)
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            with entry.condition:
                while True:
                    facts, _, _ = self._ensure_facts(task_id)
                    if facts.last_event_id > last_event_id:
                        return True
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    entry.condition.wait(remaining)
        finally:
            with self._facts_guard:
                entry.waiters -= 1
                if entry.waiters <= 0 and self._conditions.get(task_id) is entry:
                    self._conditions.pop(task_id, None)

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
        facts = self._cached_facts(task_id)
        if facts is not None:
            # 写入已 fsync；先在内存中折叠，随后 _write_snapshot 会更新文件游标并广播。
            self._apply_event(facts.snapshot, event)
            facts.events_tail.append(event)
            facts.last_event_id = seq
            facts.file_offset = (task_dir / "events.jsonl").stat().st_size
        return event

    def _write_snapshot(self, snapshot: Mapping[str, Any], *, notify: bool = True) -> None:
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
            task_id = str(snapshot["task_id"])
            facts = self._cached_facts(str(snapshot["task_id"]))
            if facts is not None:
                facts.snapshot = self._snapshot_fields(snapshot)
                events_path = task_dir / "events.jsonl"
                stat = events_path.stat()
                facts.file_offset = stat.st_size
                facts.file_identity = (getattr(stat, "st_dev", 0), getattr(stat, "st_ino", 0))
                facts.modified_ns = getattr(stat, "st_mtime_ns", 0)
            # 条件等待者只在完整快照落盘后唤醒，避免观察到事件和投影的中间态。
            with self._facts_guard:
                self._snapshot_index[task_id] = self._list_projection(snapshot)
                entry = self._conditions.get(task_id)
                if notify and entry is not None:
                    entry.condition.notify_all()
                if notify and str(snapshot.get("status")) in TERMINAL_STATES and entry is not None and entry.waiters == 0:
                    self._conditions.pop(task_id, None)
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
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """创建 queued 任务并写入首个事实事件；不执行动作。

        ``task_id`` 仅供服务端规则协调器预生成并与事务 claim 绑定；公共请求
        不能控制此参数，非法或重复 ID 会在事实源写入前拒绝。
        """
        if task_id is None:
            task_id = uuid.uuid4().hex
        elif not isinstance(task_id, str) or TASK_ID_PATTERN.fullmatch(task_id) is None:
            raise TaskError("task_id 无效")
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
        """完整校验并列出任务；仅供启动恢复和内部低频兼容路径。"""
        self._refresh_task_dirs()
        values = []
        for task_id in list(self._task_dirs):
            try:
                values.append(self._read_snapshot(task_id))
            except (KeyError, TaskError):
                continue
        return sorted(values, key=lambda item: (str(item.get("updated_at") or ""), str(item.get("task_id") or "")), reverse=True)

    def list_tasks_page(self, *, page: int, page_size: int) -> tuple[list[dict[str, Any]], int]:
        """从内存小型索引返回一页；普通 API 请求不触发目录扫描或 JSONL 回放。"""

        if page < 1 or page_size < 1:
            raise TaskError("分页参数无效")
        with self._facts_guard:
            values = [dict(item) for item in self._snapshot_index.values()]
        values.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("task_id") or "")), reverse=True)
        start = (page - 1) * page_size
        return values[start:start + page_size], len(values)

    def forget_deleted_task(self, task_id: str) -> None:
        """在受控清理成功后移除终态任务缓存；不触碰任何磁盘内容。"""

        if not TASK_ID_PATTERN.fullmatch(task_id):
            return
        with self._facts_guard:
            self._snapshot_index.pop(task_id, None)
            self._facts.pop(task_id, None)
            entry = self._conditions.get(task_id)
            if entry is not None and entry.waiters == 0:
                self._conditions.pop(task_id, None)
        self._task_dirs.pop(task_id, None)

    def transition(self, task_id: str, status: str, *, reason: str | None = None, _notify: bool = True) -> dict[str, Any]:
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
            self._write_snapshot(snapshot, notify=_notify)
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
            # 成功/失败终态还要追加 result；两者一起通知 SSE，避免客户端收到终态后提前关闭而漏结果。
            snapshot = self.transition(task_id, status, _notify=False)
            safe_result = _safe_json(result)
            self._append_event(task_id, snapshot, "result", result=safe_result)
            snapshot["result"] = safe_result
            self._write_snapshot(snapshot)
            return snapshot
