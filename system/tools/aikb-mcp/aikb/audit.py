"""AIKB 本机文本审计日志、查询聚合与 Markdown 报告。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import Settings


AUDIT_SCHEMA_VERSION = 1
AUDIT_FIELDS = (
    "schema_version", "record_type", "event_id", "invocation_id", "timestamp", "source", "agent",
    "client", "connection_id", "session_id", "project_id", "operation", "action", "status",
    "outcome_code", "result_summary", "duration_ms", "error_type",
)
FINISHED_STATUS = {"succeeded", "failed", "noop", "blocked"}
SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|password|passwd|secret|private[_-]?key)\b\s*[:=]\s*(?:bearer\s+)?(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^,;\r\n]*)"
)
TOKEN_VALUE_PATTERN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,})\b")


def _now() -> datetime:
    return datetime.now().astimezone()


def _redact_text(value: str, limit: int = 500) -> str:
    text = value.replace("\x00", "")
    text = SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = TOKEN_VALUE_PATTERN.sub("[REDACTED]", text)
    return text[:limit]


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:30]:
            safe_key = _redact_text(str(key), 80)
            if re.search(r"(?i)(authorization|cookie|password|secret|token|private[_-]?key)", safe_key):
                result[safe_key] = "[REDACTED]"
            else:
                result[safe_key] = _sanitize(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item, depth=depth + 1) for item in list(value)[:30]]
    return _redact_text(str(value))


def audit_project_id(project_path: str | None) -> str | None:
    """将本机项目路径转换为不暴露绝对路径的稳定标识。"""
    if not project_path or not str(project_path).strip():
        return None
    try:
        path = Path(str(project_path)).expanduser().resolve()
    except (OSError, RuntimeError):
        path = Path(str(project_path))
    normalized = str(path).replace("\\", "/").lower()
    name = re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-") or "project"
    return f"{name[:48]}-{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:10]}"


def summarize_tool_action(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """只从白名单字段构造 MCP 动作摘要，避免落盘正文或任意参数。"""
    if name == "search_knowledge":
        # 查询文本和标签均可能来自用户输入；审计只需要证明发生了检索及其过滤形态，不能保存原文。
        return {
            "query_chars": len(str(arguments.get("query") or "")),
            "has_type_filter": bool(arguments.get("type")),
            "has_status_filter": bool(arguments.get("status")),
            "tag_count": len(arguments.get("tags") or []),
            "limit": _sanitize(arguments.get("limit", 5)),
        }
    if name == "read_knowledge":
        identifier = str(arguments.get("id_or_path") or "")
        if identifier and not identifier.startswith("aikb:") and Path(identifier).is_absolute():
            identifier = Path(identifier).name
        return _sanitize({
            "id_or_path": identifier, "section": arguments.get("section"),
            "max_chars": arguments.get("max_chars", 4000),
        })
    if name == "get_work_state":
        return _sanitize({"project_id": audit_project_id(arguments.get("project_path")), "work_id": arguments.get("work_id")})
    if name == "checkpoint_work_state":
        return _sanitize({
            "project_id": audit_project_id(arguments.get("project_path")), "work_id": arguments.get("work_id"),
            "status": arguments.get("status", "active"), "changed_files_count": len(arguments.get("changed_files") or []),
        })
    if name == "close_work_state":
        return _sanitize({"work_id": arguments.get("work_id"), "status": arguments.get("status")})
    return {"tool": _redact_text(name, 120)}


def summarize_tool_result(name: str, value: Any) -> tuple[str, dict[str, Any]]:
    """从已知结果结构提取最小统计信息，不保存 MCP 完整返回值。"""
    if not isinstance(value, dict):
        return "completed", {}
    if name == "search_knowledge":
        return "results_returned", {"count": int(value.get("count") or 0)}
    if name == "read_knowledge":
        return "knowledge_read", {"found": not bool(value.get("error")), "truncated": bool(value.get("truncated"))}
    if name == "get_work_state":
        return "work_state_returned", {"count": len(value.get("items") or []), "unique": bool(value.get("unique"))}
    if name == "checkpoint_work_state":
        return "checkpoint_created", {"work_id": _redact_text(str(value.get("work_id") or ""), 120)}
    if name == "close_work_state":
        return "work_state_closed", {"work_id": _redact_text(str(value.get("work_id") or ""), 120)}
    return "completed", {}


class AuditStore:
    """以按日 JSONL 保存审计事件；任何写入故障都降级而不影响调用方。"""

    def __init__(self, settings: Settings, clock: Callable[[], datetime] = _now):
        self.settings = settings
        self.clock = clock
        self.root = settings.workspace_root / "audit"
        self.events_root = self.root / "events"
        self.fallback_root = self.root / "fallback"
        self.reports_root = self.root / "reports"
        self.lock_path = settings.workspace_root / "runtime" / "audit.lock"

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    def _timestamp(self) -> tuple[datetime, str]:
        current = self.clock().astimezone()
        return current, current.isoformat(timespec="milliseconds")

    def _event_path(self, current: datetime) -> Path:
        return self.events_root / f"{current:%Y}" / f"{current:%m}" / f"{current:%Y-%m-%d}.jsonl"

    @contextmanager
    def _lock(self, timeout: float = 1.0) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        locked = False
        try:
            while not locked:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except (OSError, BlockingIOError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("audit lock timeout")
                    time.sleep(0.02)
            yield
        finally:
            if locked:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()

    def _normalize_record(self, record: dict[str, Any], current: datetime, timestamp: str) -> dict[str, Any]:
        normalized = {field: None for field in AUDIT_FIELDS}
        normalized.update(record)
        normalized["schema_version"] = AUDIT_SCHEMA_VERSION
        normalized["event_id"] = str(normalized.get("event_id") or self.new_id())
        normalized["timestamp"] = str(normalized.get("timestamp") or timestamp)
        normalized["agent"] = _redact_text(str(normalized.get("agent") or "unknown"), 120)
        for field, limit in (
            ("invocation_id", 120), ("connection_id", 120), ("session_id", 160), ("project_id", 120),
            ("operation", 120), ("status", 40), ("outcome_code", 120), ("error_type", 120),
        ):
            if normalized.get(field) is not None:
                normalized[field] = _redact_text(str(normalized[field]), limit)
        normalized["action"] = _sanitize(normalized.get("action"))
        normalized["result_summary"] = _sanitize(normalized.get("result_summary"))
        normalized["client"] = _sanitize(normalized.get("client"))
        return {field: normalized.get(field) for field in AUDIT_FIELDS}

    def _write_fallback(self, record: dict[str, Any], current: datetime) -> dict[str, Any]:
        try:
            directory = self.fallback_root / f"{current:%Y}" / f"{current:%m}" / f"{current:%Y-%m-%d}"
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{current:%H%M%S}-{record['event_id']}.json"
            fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(record, stream, ensure_ascii=False, separators=(",", ":"))
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, target)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            return {"written": True, "fallback": True, "path": str(target)}
        except Exception as exc:
            return {"written": False, "fallback": True, "error": type(exc).__name__}

    def write(self, record: dict[str, Any]) -> dict[str, Any]:
        current, timestamp = self._timestamp()
        normalized = self._normalize_record(record, current, timestamp)
        try:
            target = self._event_path(current)
            target.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")) + "\n"
            with self._lock():
                with target.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(line)
                    stream.flush()
            return {"written": True, "fallback": False, "path": str(target), "event_id": normalized["event_id"]}
        except Exception:
            return self._write_fallback(normalized, current)

    def start(
        self, *, source: str, agent: str, operation: str, action: dict[str, Any] | None = None,
        client: dict[str, Any] | None = None, connection_id: str | None = None,
        session_id: str | None = None, project_id: str | None = None,
    ) -> dict[str, Any]:
        invocation_id = self.new_id()
        started = time.perf_counter()
        result = self.write({
            "record_type": "invocation_started", "invocation_id": invocation_id, "source": source,
            "agent": agent or "unknown", "client": client, "connection_id": connection_id,
            "session_id": session_id, "project_id": project_id, "operation": operation, "action": action,
            "status": "started",
        })
        return {"invocation_id": invocation_id, "started": started, "write": result}

    def finish(
        self, invocation: dict[str, Any], *, source: str, agent: str, operation: str, status: str,
        outcome_code: str, result_summary: dict[str, Any] | None = None, error_type: str | None = None,
        client: dict[str, Any] | None = None, connection_id: str | None = None,
        session_id: str | None = None, project_id: str | None = None,
    ) -> dict[str, Any]:
        safe_status = status if status in FINISHED_STATUS else "failed"
        duration = max(0, round((time.perf_counter() - float(invocation["started"])) * 1000))
        return self.write({
            "record_type": "invocation_finished", "invocation_id": invocation["invocation_id"],
            "source": source, "agent": agent or "unknown", "client": client, "connection_id": connection_id,
            "session_id": session_id, "project_id": project_id, "operation": operation,
            "status": safe_status, "outcome_code": outcome_code, "result_summary": result_summary,
            "duration_ms": duration, "error_type": error_type,
        })

    def connection_initialized(self, *, agent: str, client: dict[str, Any], connection_id: str) -> dict[str, Any]:
        return self.write({
            "record_type": "connection_initialized", "source": "mcp", "agent": agent or "unknown",
            "client": client, "connection_id": connection_id, "operation": "initialize", "status": "succeeded",
            "outcome_code": "connection_initialized",
        })

    def _iter_source_files(self) -> Iterator[tuple[Path, bool]]:
        if self.events_root.exists():
            for path in sorted(self.events_root.rglob("*.jsonl")):
                yield path, False
        if self.fallback_root.exists():
            for path in sorted(self.fallback_root.rglob("*.json")):
                yield path, True

    def read_events(self) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        damaged: list[str] = []
        fallback_count = 0
        for path, fallback in self._iter_source_files():
            try:
                lines = path.read_text(encoding="utf-8").splitlines() if not fallback else [path.read_text(encoding="utf-8")]
                for number, line in enumerate(lines, 1):
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            raise ValueError("audit event is not an object")
                        value["_fallback"] = fallback
                        events.append(value)
                        fallback_count += int(fallback)
                    except (json.JSONDecodeError, ValueError, TypeError):
                        damaged.append(f"{path}:{number}")
            except (OSError, UnicodeError):
                damaged.append(str(path))
        events.sort(key=lambda item: str(item.get("timestamp") or ""))
        return {"events": events, "damaged": damaged, "fallback_count": fallback_count}


def parse_since(value: str | None, now: datetime | None = None) -> datetime | None:
    if not value:
        return None
    match = re.fullmatch(r"(\d+)([hd])", value.strip().lower())
    if not match:
        raise ValueError("--since 仅支持 <整数>h 或 <整数>d，例如 24h、7d")
    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError("--since 必须大于 0")
    delta = timedelta(hours=amount) if match.group(2) == "h" else timedelta(days=amount)
    return (now or _now()) - delta


def _event_datetime(event: dict[str, Any]) -> datetime | None:
    try:
        value = event.get("started_at") or event.get("timestamp") or event.get("finished_at") or ""
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.astimezone()
    except ValueError:
        return None


def filter_events(
    events: list[dict[str, Any]], *, since: str | None = None, on_date: str | None = None,
    agent: str | None = None, source: str | None = None, status: str | None = None,
) -> list[dict[str, Any]]:
    threshold = parse_since(since)
    selected_date = date.fromisoformat(on_date) if on_date else None
    result: list[dict[str, Any]] = []
    for event in events:
        timestamp = _event_datetime(event)
        if threshold and (timestamp is None or timestamp < threshold):
            continue
        if selected_date and (timestamp is None or timestamp.astimezone().date() != selected_date):
            continue
        if agent and event.get("agent") != agent:
            continue
        if source and event.get("source") != source:
            continue
        if status and event.get("status") != status:
            continue
        result.append(event)
    return result


def combine_invocations(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    invocations: dict[str, dict[str, Any]] = {}
    standalone: list[dict[str, Any]] = []
    for event in events:
        invocation_id = event.get("invocation_id")
        record_type = event.get("record_type")
        if not invocation_id or record_type not in {"invocation_started", "invocation_finished"}:
            standalone.append(dict(event))
            continue
        item = invocations.setdefault(str(invocation_id), {"invocation_id": invocation_id})
        if record_type == "invocation_started":
            item.update({key: value for key, value in event.items() if not str(key).startswith("_")})
            item["started_at"] = event.get("timestamp")
        else:
            for key in (
                "schema_version", "source", "agent", "client", "connection_id", "session_id", "project_id", "operation",
            ):
                if key not in item or item.get(key) is None:
                    item[key] = event.get(key)
            for key in ("status", "outcome_code", "result_summary", "duration_ms", "error_type"):
                item[key] = event.get(key)
            item["finished_at"] = event.get("timestamp")
            item["finish_event_id"] = event.get("event_id")
            item["_fallback"] = bool(item.get("_fallback")) or bool(event.get("_fallback"))
    for item in invocations.values():
        if "finished_at" not in item:
            item["status"] = "incomplete"
            item["outcome_code"] = "missing_finish_event"
    combined = list(invocations.values()) + standalone
    combined.sort(key=lambda item: str(item.get("started_at") or item.get("timestamp") or ""))
    return combined


def audit_summary(items: list[dict[str, Any]], *, damaged: list[str], fallback_count: int) -> dict[str, Any]:
    statuses = Counter(str(item.get("status") or "unknown") for item in items)
    agents = Counter(str(item.get("agent") or "unknown") for item in items)
    sources = Counter(str(item.get("source") or "unknown") for item in items)
    operations = Counter(str(item.get("operation") or "unknown") for item in items)
    durations = [int(item["duration_ms"]) for item in items if isinstance(item.get("duration_ms"), (int, float))]
    return {
        "count": len(items), "statuses": dict(statuses), "agents": dict(agents), "sources": dict(sources),
        "operations": dict(operations), "average_duration_ms": round(sum(durations) / len(durations), 1) if durations else None,
        "fallback_records": fallback_count, "damaged_count": len(damaged), "damaged": damaged,
        "last_activity": max((str(item.get("finished_at") or item.get("timestamp") or item.get("started_at") or "") for item in items), default=None),
    }


def render_markdown(items: list[dict[str, Any]], summary: dict[str, Any], title_date: str) -> str:
    def esc(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    lines = [
        f"# AIKB 审计报告：{title_date}", "", f"生成时间：{_now().isoformat(timespec='seconds')}", "", "## 总览", "",
        "| 指标 | 数量 |", "|---|---:|", f"| 逻辑事件 | {summary['count']} |",
    ]
    for status in ("succeeded", "noop", "blocked", "failed", "incomplete"):
        lines.append(f"| {status} | {summary['statuses'].get(status, 0)} |")
    lines.extend([
        f"| fallback 记录 | {summary['fallback_records']} |", f"| 损坏记录 | {summary['damaged_count']} |", "",
        "## Agent 活动", "", "| Agent | 次数 |", "|---|---:|",
    ])
    for agent, count in sorted(summary["agents"].items()):
        lines.append(f"| {esc(agent)} | {count} |")
    lines.extend(["", "## 调用明细", "", "| 时间 | Agent | 来源 | 操作 | 动作摘要 | 结果 | 耗时 |", "|---|---|---|---|---|---|---:|"])
    for item in items:
        timestamp = item.get("started_at") or item.get("timestamp") or ""
        action = json.dumps(item.get("action"), ensure_ascii=False, separators=(",", ":")) if item.get("action") else ""
        duration = "" if item.get("duration_ms") is None else f"{item['duration_ms']} ms"
        lines.append(
            f"| {esc(timestamp)} | {esc(item.get('agent'))} | {esc(item.get('source'))} | {esc(item.get('operation'))} | "
            f"{esc(action)} | {esc(item.get('status'))}/{esc(item.get('outcome_code'))} | {esc(duration)} |"
        )
    if summary["damaged"]:
        lines.extend(["", "## 无法解析的记录", ""] + [f"- `{path}`" for path in summary["damaged"]])
    return "\n".join(lines) + "\n"


def write_report(path: Path, content: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
