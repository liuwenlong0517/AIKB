"""AIKB 本机文本审计日志、查询聚合与 Markdown 报告。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
import zipfile
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator
from xml.sax.saxutils import escape as xml_escape

from .config import Settings


AUDIT_SCHEMA_VERSION = 2
AUDIT_FIELDS = (
    "schema_version", "record_type", "event_id", "invocation_id", "timestamp", "source", "agent",
    "client", "connection_id", "session_id", "session_label", "project_id", "operation", "action", "action_text",
    "status", "outcome_code", "result_summary", "result_text", "capture_level", "duration_ms", "error_type",
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


def _sanitize_diagnostic(value: Any, *, limit: int, collection_limit: int, depth: int = 0) -> Any:
    """保存诊断附件前递归脱敏并限制总体体积，full-local 也不绕过密钥保护。"""
    if depth > 10:
        return "[TRUNCATED: depth]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value, min(limit, 16_000))
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:collection_limit]:
            safe_key = _redact_text(str(key), 120)
            result[safe_key] = "[REDACTED]" if re.search(r"(?i)(authorization|cookie|password|secret|token|private[_-]?key)", safe_key) else _sanitize_diagnostic(item, limit=limit, collection_limit=collection_limit, depth=depth + 1)
        if len(value) > collection_limit:
            result["_truncated_fields"] = len(value) - collection_limit
        return _truncate_diagnostic(result, limit)
    if isinstance(value, (list, tuple, set)):
        result = [_sanitize_diagnostic(item, limit=limit, collection_limit=collection_limit, depth=depth + 1) for item in list(value)[:collection_limit]]
        if len(value) > collection_limit:
            result.append(f"[TRUNCATED: {len(value) - collection_limit} items]")
        return _truncate_diagnostic(result, limit)
    return _redact_text(str(value), min(limit, 16_000))


def _truncate_diagnostic(value: Any, limit: int) -> Any:
    """以 UTF-8 字节预算约束诊断记录，保证恶意或异常返回不会无限膨胀。"""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= limit:
        return value
    preview = encoded[:limit].decode("utf-8", errors="ignore")
    return {"_truncated": True, "_original_bytes": len(encoded), "preview": preview}


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
    """只从白名单字段构造 MCP 动作摘要，保留足以人工审查的脱敏关键词预览。"""
    if name == "search_knowledge":
        # 检索词对审计有解释价值，因此保存限长且脱敏的预览，仍不保存完整 prompt。
        return {
            "query_preview": _redact_text(str(arguments.get("query") or ""), 160),
            "type": _sanitize(arguments.get("type")), "status": _sanitize(arguments.get("status")),
            "tags": _sanitize(arguments.get("tags") or []),
            "limit": _sanitize(arguments.get("limit", 5)),
        }
    if name == "review_knowledge":
        return {}
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


def describe_action(operation: str, action: dict[str, Any] | None) -> str:
    """把白名单动作摘要翻译为稳定中文说明，不依赖模型生成或原始 prompt。"""
    action = action or {}
    if operation == "search_knowledge":
        filters = []
        if action.get("type"):
            filters.append(f"类型 {action['type']}")
        if action.get("status"):
            filters.append(f"状态 {action['status']}")
        if action.get("tags"):
            filters.append("标签 " + "、".join(str(tag) for tag in action["tags"]))
        suffix = f"；过滤：{'，'.join(filters)}" if filters else ""
        return f"检索知识：关键词“{action.get('query_preview') or '（空）'}”；最多返回 {action.get('limit', 5)} 条{suffix}"
    if operation == "review_knowledge":
        return "读取知识审查队列"
    if operation == "read_knowledge":
        section = action.get("section") or "全文"
        return f"读取知识“{action.get('id_or_path') or '未知对象'}”的“{section}”；字符预算 {action.get('max_chars', 4000)}"
    if operation == "get_work_state":
        return f"查询项目 {action.get('project_id') or '未知项目'} 的活动任务"
    if operation == "checkpoint_work_state":
        return f"为任务 {action.get('work_id') or '新任务'} 写入 {action.get('status') or 'active'} 检查点；变更文件 {action.get('changed_files_count', 0)} 个"
    if operation == "close_work_state":
        return f"关闭任务 {action.get('work_id') or '未知任务'}；状态 {action.get('status') or '未知'}"
    if operation == "initialize":
        return "初始化 MCP 连接"
    event = action.get("event") or operation
    return f"处理生命周期事件：{event}"


def describe_result(operation: str, status: str, outcome_code: str | None, result: dict[str, Any] | None) -> str:
    """将结果代码和最小统计信息转换为可读结论，失败不写入完整异常文本。"""
    result = result or {}
    if status == "failed":
        return f"执行失败：{outcome_code or '未知错误'}"
    messages = {
        "results_returned": f"检索完成，返回 {result.get('count', 0)} 条结果",
        "knowledge_reviewed": f"知识审查队列：{result.get('candidate_count', 0)} 个候选，{result.get('review_count', 0)} 个复核条件",
        "knowledge_read": "已读取知识" + ("（内容已截断）" if result.get("truncated") else ""),
        "work_state_returned": f"找到 {result.get('count', 0)} 个活动任务" + ("（唯一候选）" if result.get("unique") else ""),
        "checkpoint_created": "检查点已创建",
        "work_state_closed": "任务已关闭",
        "connection_initialized": "MCP 连接已初始化",
        "resume_context_injected": "已注入唯一活动任务的恢复信息",
        "no_active_work": "没有活动任务，无需处理",
        "multiple_active_work": f"发现 {result.get('candidate_count', 0)} 个候选，未注入恢复信息",
        "checkpoint_required": "检测到检查点后的 Git 变化，已阻止结束",
        "git_unchanged": "Git 状态未变化，无需阻止结束",
        "recursion_skipped": "检测到递归 Stop hook，已跳过",
        "pre_compact_observed": "已记录上下文压缩前事件",
        "session_end_observed": "已记录会话结束事件",
        "invalid_project": "未提供有效项目路径",
    }
    return messages.get(outcome_code or "", f"处理完成：{outcome_code or status}")


def summarize_tool_result(name: str, value: Any) -> tuple[str, dict[str, Any]]:
    """从已知结果结构提取最小统计信息，不保存 MCP 完整返回值。"""
    if not isinstance(value, dict):
        return "completed", {}
    if name == "search_knowledge":
        return "results_returned", {"count": int(value.get("count") or 0)}
    if name == "review_knowledge":
        return "knowledge_reviewed", {
            "candidate_count": len(value.get("candidates") or []),
            "review_count": len(value.get("review_items") or []),
        }
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
        self.diagnostic_root = self.root / "diagnostic"
        self.fallback_root = self.root / "fallback"
        self.reports_root = self.root / "reports"
        self.session_registry_path = self.root / "sessions.json"
        self.lock_path = settings.workspace_root / "runtime" / "audit.lock"

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    def _timestamp(self) -> tuple[datetime, str]:
        current = self.clock().astimezone()
        return current, current.isoformat(timespec="milliseconds")

    def _event_path(self, current: datetime) -> Path:
        return self.events_root / f"{current:%Y}" / f"{current:%m}" / f"{current:%Y-%m-%d}.jsonl"

    def _diagnostic_path(self, current: datetime) -> Path:
        return self.diagnostic_root / f"{current:%Y}" / f"{current:%m}" / f"{current:%Y-%m-%d}.jsonl"

    def resolve_session_label(
        self, *, agent: str, source: str, session_id: str | None, connection_id: str | None,
        project_id: str | None, supplied_label: str | None = None,
    ) -> str:
        """分配可读且跨进程稳定的本机会话标签，不把连接 ID 伪装成真实会话 ID。"""
        provided = _redact_text(str(supplied_label or "").strip(), 240)
        identity = str(session_id or connection_id or "").strip()
        if not identity:
            return provided or f"{agent or 'unknown'} · 未提供会话 ID"
        key = hashlib.sha256(f"{agent}|{source}|{identity}".encode("utf-8")).hexdigest()
        try:
            with self._lock():
                registry: dict[str, str] = {}
                if self.session_registry_path.is_file():
                    registry = json.loads(self.session_registry_path.read_text(encoding="utf-8"))
                if key in registry:
                    return registry[key]
                project_name = (project_id or "项目").rsplit("-", 1)[0] or "项目"
                ordinal = 1 + sum(value.startswith(f"{agent} ·") for value in registry.values())
                kind = "会话" if session_id else "MCP 连接"
                label = provided or f"{agent or 'unknown'} · {project_name} · {kind} {ordinal:03d} · {self.clock().astimezone():%Y-%m-%d %H:%M}"
                registry[key] = label
                self.session_registry_path.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix="sessions.", suffix=".tmp", dir=self.session_registry_path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                        json.dump(registry, stream, ensure_ascii=False, separators=(",", ":"))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.session_registry_path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                return label
        except Exception:
            return provided or f"{agent or 'unknown'} · {'会话' if session_id else 'MCP 连接'}"

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
            ("invocation_id", 120), ("connection_id", 120), ("session_id", 160), ("session_label", 240), ("project_id", 120),
            ("operation", 120), ("status", 40), ("outcome_code", 120), ("error_type", 120),
            ("action_text", 1200), ("result_text", 1200),
        ):
            if normalized.get(field) is not None:
                normalized[field] = _redact_text(str(normalized[field]), limit)
        normalized["action"] = _sanitize(normalized.get("action"))
        normalized["result_summary"] = _sanitize(normalized.get("result_summary"))
        normalized["client"] = _sanitize(normalized.get("client"))
        normalized["capture_level"] = normalized.get("capture_level") if normalized.get("capture_level") in {"safe", "diagnostic", "full-local"} else "safe"
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

    def write_diagnostic(
        self, *, invocation_id: str, source: str, agent: str, operation: str, phase: str,
        session_id: str | None, session_label: str | None, payload: Any,
    ) -> None:
        """按显式分级保存本机诊断附件；任何故障均被吞没，不能影响业务调用。"""
        level = self.settings.audit_capture_level
        if level == "safe":
            return
        current, timestamp = self._timestamp()
        limit = 32_000 if level == "diagnostic" else 256_000
        value = _sanitize_diagnostic(payload, limit=limit, collection_limit=50 if level == "diagnostic" else 500)
        record = {
            "schema_version": 1, "record_type": "diagnostic", "event_id": self.new_id(), "invocation_id": invocation_id,
            "timestamp": timestamp, "source": source, "agent": _redact_text(agent, 120), "operation": operation,
            "phase": phase, "session_id": _redact_text(session_id, 160) if session_id else None,
            "session_label": _redact_text(session_label, 240) if session_label else None,
            "capture_level": level, "payload": value,
        }
        try:
            target = self._diagnostic_path(current)
            target.parent.mkdir(parents=True, exist_ok=True)
            with self._lock():
                with target.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                    stream.flush()
        except Exception:
            pass

    def read_diagnostics(self, invocation_id: str) -> dict[str, Any]:
        """读取某次调用的独立诊断附件；损坏附件不影响主审计查询。"""
        items: list[dict[str, Any]] = []
        damaged: list[str] = []
        if self.diagnostic_root.exists():
            for path in sorted(self.diagnostic_root.rglob("*.jsonl")):
                try:
                    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                        if line.strip():
                            value = json.loads(line)
                            if value.get("invocation_id") == invocation_id:
                                items.append(value)
                except (OSError, UnicodeError, json.JSONDecodeError):
                    damaged.append(f"{path}:{number if 'number' in locals() else 1}")
        return {"count": len(items), "items": items, "damaged": damaged}

    def start(
        self, *, source: str, agent: str, operation: str, action: dict[str, Any] | None = None,
        client: dict[str, Any] | None = None, connection_id: str | None = None,
        session_id: str | None = None, project_id: str | None = None, session_label: str | None = None,
    ) -> dict[str, Any]:
        session_label = session_label or self.resolve_session_label(
            agent=agent, source=source, session_id=session_id, connection_id=connection_id, project_id=project_id,
        )
        invocation_id = self.new_id()
        started = time.perf_counter()
        result = self.write({
            "record_type": "invocation_started", "invocation_id": invocation_id, "source": source,
            "agent": agent or "unknown", "client": client, "connection_id": connection_id,
            "session_id": session_id, "session_label": session_label, "project_id": project_id, "operation": operation, "action": action,
            "action_text": describe_action(operation, action), "capture_level": self.settings.audit_capture_level,
            "status": "started",
        })
        return {"invocation_id": invocation_id, "started": started, "write": result}

    def finish(
        self, invocation: dict[str, Any], *, source: str, agent: str, operation: str, status: str,
        outcome_code: str, result_summary: dict[str, Any] | None = None, error_type: str | None = None,
        client: dict[str, Any] | None = None, connection_id: str | None = None,
        session_id: str | None = None, project_id: str | None = None, session_label: str | None = None,
    ) -> dict[str, Any]:
        safe_status = status if status in FINISHED_STATUS else "failed"
        duration = max(0, round((time.perf_counter() - float(invocation["started"])) * 1000))
        return self.write({
            "record_type": "invocation_finished", "invocation_id": invocation["invocation_id"],
            "source": source, "agent": agent or "unknown", "client": client, "connection_id": connection_id,
            "session_id": session_id, "session_label": session_label, "project_id": project_id, "operation": operation,
            "status": safe_status, "outcome_code": outcome_code, "result_summary": result_summary,
            "result_text": describe_result(operation, safe_status, outcome_code, result_summary),
            "capture_level": self.settings.audit_capture_level, "duration_ms": duration, "error_type": error_type,
        })

    def connection_initialized(self, *, agent: str, client: dict[str, Any], connection_id: str) -> dict[str, Any]:
        session_label = self.resolve_session_label(
            agent=agent, source="mcp", session_id=None, connection_id=connection_id, project_id=None,
        )
        return self.write({
            "record_type": "connection_initialized", "source": "mcp", "agent": agent or "unknown",
            "client": client, "connection_id": connection_id, "session_label": session_label, "operation": "initialize", "status": "succeeded",
            "outcome_code": "connection_initialized", "action_text": describe_action("initialize", None),
            "result_text": describe_result("initialize", "succeeded", "connection_initialized", None),
            "capture_level": self.settings.audit_capture_level,
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
    agent: str | None = None, source: str | None = None, status: str | list[str] | tuple[str, ...] | None = None,
    operation: str | None = None,
) -> list[dict[str, Any]]:
    threshold = parse_since(since)
    selected_date = date.fromisoformat(on_date) if on_date else None
    statuses = {str(item) for item in status} if isinstance(status, (list, tuple)) else ({status} if status else set())
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
        if statuses and event.get("status") not in statuses:
            continue
        if operation and event.get("operation") != operation:
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
            item["_fallback"] = bool(item.get("_fallback")) or bool(event.get("_fallback"))
        else:
            for key in (
                "schema_version", "source", "agent", "client", "connection_id", "session_id", "session_label", "project_id", "operation", "capture_level",
            ):
                if key not in item or item.get(key) is None:
                    item[key] = event.get(key)
            for key in ("status", "outcome_code", "result_summary", "result_text", "duration_ms", "error_type"):
                item[key] = event.get(key)
            item["finished_at"] = event.get("timestamp")
            item["finish_event_id"] = event.get("event_id")
            item["_fallback"] = bool(item.get("_fallback")) or bool(event.get("_fallback"))
    for item in invocations.values():
        if "finished_at" not in item:
            item["status"] = "incomplete"
            item["outcome_code"] = "missing_finish_event"
    combined = list(invocations.values()) + standalone
    # v1 历史日志没有可读字段；报告阶段即时派生，避免迁移或改写审计事实源。
    for item in combined:
        item["action_text"] = item.get("action_text") or describe_action(str(item.get("operation") or ""), item.get("action"))
        item["result_text"] = item.get("result_text") or describe_result(
            str(item.get("operation") or ""), str(item.get("status") or ""), item.get("outcome_code"), item.get("result_summary"),
        )
        if not item.get("session_label"):
            item["session_label"] = f"{item.get('agent') or 'unknown'} · 历史记录（未提供会话标签）"
            # CLI 报告继续使用兼容标签；Web 安全模型据此恢复为 null，避免把派生标签
            # 当成事实源提供的真实会话信息。
            item["_session_label_synthesized"] = True
        item["capture_level"] = item.get("capture_level") or "safe"
    # 先按标识升序，再按时间升序，保证同刻历史记录不依赖文件遍历顺序。
    combined.sort(key=lambda item: _audit_sort_key(item)[1:])
    combined.sort(key=lambda item: _audit_sort_key(item)[0])
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


# Web 审计查询的输出预算是协议边界，不等同于本机报告或诊断命令的完整能力。
# 特别是 ``action``/``result_summary`` 可能包含调用参数或返回值，即使写入时已经
# 脱敏，也不应因为 Web 查询而重新扩大暴露面。
WEB_AUDIT_MAX_PAGE_SIZE = 100
WEB_AUDIT_MAX_PAGE = 10_000
WEB_AUDIT_SAFE_FIELDS = (
    "schema_version", "record_type", "event_id", "invocation_id", "timestamp", "started_at", "finished_at",
    "source", "agent", "session_id", "session_label", "project_id", "operation", "status", "outcome_code",
    "action_text", "result_text", "capture_level", "duration_ms", "error_type", "fallback",
)
WEB_AUDIT_WINDOWS_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/](?![\\/])|\\\\)[^\s\"'<>|]+")
# 覆盖常见根目录与未知的两级以上 Unix 绝对路径；冒号边界排除
# ``https://`` 等 URL，裸的 ``content/...`` 逻辑路径没有开头斜杠。
WEB_AUDIT_UNIX_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_:/])/(?:"
    r"(?:Users|home|private|tmp|var|etc|opt|srv|root|usr|mnt|workspace|bin|dev|proc|sys|run|lib|sbin|boot|media)"
    r"(?:/[^\s\"'<>|]*)*"
    r"|(?:[^\s\"'<>|/]+(?:/[^\s\"'<>|/]+)*)+)"
)


def _redact_web_text(value: str) -> str:
    """移除旧日志字段中可能残留的 Windows/Unix 绝对路径，再交给通用脱敏器。"""
    value = WEB_AUDIT_WINDOWS_PATH_PATTERN.sub("[PATH]", value)
    return WEB_AUDIT_UNIX_PATH_PATTERN.sub("[PATH]", value)


def _audit_sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
    """生成稳定审计排序键：活动时间优先，标识用于同刻记录的确定性打散。"""
    activity = str(item.get("finished_at") or item.get("timestamp") or item.get("started_at") or "")
    invocation = str(item.get("invocation_id") or "")
    event = str(item.get("event_id") or item.get("finish_event_id") or "")
    return activity, invocation, event


def _web_identifier(value: Any, limit: int = 160) -> str | None:
    """把审计标识压缩为有限字符串；空值保持 ``None``，不替缺失会话造值。"""
    if value is None:
        return None
    text = _redact_web_text(_redact_text(str(value), limit)).strip()
    return text or None


def _web_error_type(value: Any) -> str | None:
    """只保留异常类型名，不把旧日志里的异常消息、路径或 traceback 投影给 Web。"""
    text = _web_identifier(value, 120)
    if not text:
        return None
    # 新旧日志都可能把 ``Type: message`` 写进 error_type；类型名足够用于筛选和审计。
    candidate = text.split(":", 1)[0].strip().split("\n", 1)[0].strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", candidate):
        return candidate.rsplit(".", 1)[-1]
    return "error"


def web_audit_item(item: dict[str, Any]) -> dict[str, Any]:
    """将已合并审计项转换成 Web 安全读模型。

    该投影只复制稳定的时间、状态和中文说明字段，不复制原始 ``action``、
    ``result_summary``、``client`` 或内部 ``_fallback`` 等字段。调用方可以看到
    ``fallback`` 和 ``capture_level`` 这类安全状态，但永远不能由此取得 diagnostic
    附件；未知字段也会被忽略，以免 v1/v2 或未来 schema 扩展意外越过边界。
    """
    if not isinstance(item, dict):
        return {}
    result: dict[str, Any] = {
        "schema_version": item.get("schema_version") if item.get("schema_version") in {1, 2} else None,
        "record_type": _web_identifier(item.get("record_type"), 80),
        "event_id": _web_identifier(item.get("event_id"), 120),
        "invocation_id": _web_identifier(item.get("invocation_id"), 120),
        "timestamp": _web_identifier(item.get("timestamp"), 80),
        "started_at": _web_identifier(item.get("started_at"), 80),
        "finished_at": _web_identifier(item.get("finished_at"), 80),
        "source": _web_identifier(item.get("source"), 40),
        "agent": _web_identifier(item.get("agent"), 120),
        # session_id 若事实源提供则原样关联；缺失保持 null，session_label 也只使用已有值。
        "session_id": _web_identifier(item.get("session_id"), 160),
        "session_label": None if item.get("_session_label_synthesized") else _web_identifier(item.get("session_label"), 240),
        "project_id": _web_identifier(item.get("project_id"), 120),
        "operation": _web_identifier(item.get("operation"), 120),
        "status": _web_identifier(item.get("status"), 40),
        "outcome_code": _web_identifier(item.get("outcome_code"), 120),
        "action_text": _web_identifier(item.get("action_text"), 1200),
        "result_text": _web_identifier(item.get("result_text"), 1200),
        "capture_level": item.get("capture_level") if item.get("capture_level") in {"safe", "diagnostic", "full-local"} else "safe",
        "duration_ms": max(0, int(item["duration_ms"])) if isinstance(item.get("duration_ms"), (int, float)) else None,
        "error_type": _web_error_type(item.get("error_type")),
        "fallback": bool(item.get("_fallback") or item.get("fallback")),
    }
    # 返回固定字段集合，避免把安全投影当成通用字典而意外追加原始内容。
    return {key: result[key] for key in WEB_AUDIT_SAFE_FIELDS if key in result}


def _web_filters(
    items: list[dict[str, Any]], *, session_label: str | None = None, project_id: str | None = None,
) -> list[dict[str, Any]]:
    """应用不属于核心 ``filter_events`` 的两个展示筛选，不读取或解析诊断附件。"""
    if session_label:
        items = [item for item in items if item.get("session_label") == session_label]
    if project_id:
        items = [item for item in items if item.get("project_id") == project_id]
    return items


def _validate_web_paging(page: int, page_size: int) -> tuple[int, int]:
    """校验分页上下界，防止 API 通过超大 offset 或响应页突破本地预算。"""
    if isinstance(page, bool) or isinstance(page_size, bool):
        raise ValueError("分页参数必须是整数")
    if not isinstance(page, int) or not isinstance(page_size, int):
        raise ValueError("分页参数必须是整数")
    if page < 1 or page > WEB_AUDIT_MAX_PAGE:
        raise ValueError(f"page 必须在 1 至 {WEB_AUDIT_MAX_PAGE} 之间")
    if page_size < 1 or page_size > WEB_AUDIT_MAX_PAGE_SIZE:
        raise ValueError(f"page_size 必须在 1 至 {WEB_AUDIT_MAX_PAGE_SIZE} 之间")
    return page, page_size


def _web_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """删除 ``audit_summary`` 中的损坏文件路径，仅保留计数、状态和时间摘要。"""
    allowed = (
        "count", "statuses", "agents", "sources", "operations", "average_duration_ms",
        "fallback_records", "damaged_count", "last_activity",
    )
    result = {key: summary.get(key) for key in allowed}
    # 计数键来自本地日志，统一转成字符串和非负整数，避免异常对象被 JSON 序列化。
    for key in ("statuses", "agents", "sources", "operations"):
        values = result.get(key)
        if isinstance(values, dict):
            result[key] = {
                _web_identifier(name, 120) or "unknown": max(0, int(count))
                for name, count in values.items()
                if isinstance(count, (int, float))
            }
        else:
            result[key] = {}
    for key in ("count", "fallback_records", "damaged_count"):
        result[key] = max(0, int(result[key] or 0))
    result["has_damaged"] = result["damaged_count"] > 0
    return result


def web_audit_query(
    store: AuditStore, *, since: str | None = None, on_date: str | None = None, agent: str | None = None,
    source: str | None = None, status: str | list[str] | tuple[str, ...] | None = None, operation: str | None = None,
    session_label: str | None = None, project_id: str | None = None, page: int = 1, page_size: int = 50,
) -> dict[str, Any]:
    """查询审计 Web 安全模型，统一复用事实源读取、筛选、调用合并和汇总逻辑。

    ``store`` 只要求实现 ``read_events``，因此后端可注入隔离存储或测试替身。返回
    的损坏信息只有数量/布尔标志，fallback 只以计数和单项布尔状态表达，绝不返回路径。
    """
    page, page_size = _validate_web_paging(page, page_size)
    loaded = store.read_events()
    events = loaded.get("events", []) if isinstance(loaded, dict) else []
    damaged = loaded.get("damaged", []) if isinstance(loaded, dict) else []
    combined = combine_invocations(events if isinstance(events, list) else [])
    selected = filter_events(
        combined, since=since, on_date=on_date, agent=agent, source=source, status=status, operation=operation,
    )
    selected = _web_filters(selected, session_label=session_label, project_id=project_id)
    # 审计页面默认展示最新活动；排序发生在分页前，避免第一页落在最旧历史。
    # 最新活动在前；同一时间使用 invocation_id/event_id 升序稳定打散。
    selected.sort(key=lambda item: _audit_sort_key(item)[1:])
    selected.sort(key=lambda item: _audit_sort_key(item)[0], reverse=True)
    summary = _web_summary(audit_summary(
        selected,
        damaged=damaged if isinstance(damaged, list) else [],
        # read_events 的 fallback_count 是全局原始记录数；重新按筛选后的合并调用计算，
        # 防止“只看 mcp/某 Agent”时把其他来源的 fallback 误计入当前 Web 汇总。
        fallback_count=sum(1 for item in selected if item.get("_fallback")),
    ))
    total = len(selected)
    start = (page - 1) * page_size
    items = [web_audit_item(item) for item in selected[start:start + page_size]]
    total_pages = (total + page_size - 1) // page_size if total else 0
    return {
        "items": items,
        "summary": summary,
        "pagination": {
            "page": page, "page_size": page_size, "total": total, "total_pages": total_pages,
            "has_next": start + len(items) < total, "has_previous": page > 1 and total > 0,
        },
    }


def web_audit_summary(
    store: AuditStore, *, since: str | None = None, on_date: str | None = None, agent: str | None = None,
    source: str | None = None, status: str | list[str] | tuple[str, ...] | None = None, operation: str | None = None,
    session_label: str | None = None, project_id: str | None = None,
) -> dict[str, Any]:
    """返回 Web 审计安全汇总；与列表使用完全相同的筛选和兼容逻辑。"""
    return web_audit_query(
        store, since=since, on_date=on_date, agent=agent, source=source, status=status, operation=operation,
        session_label=session_label, project_id=project_id, page=1, page_size=WEB_AUDIT_MAX_PAGE_SIZE,
    )["summary"]


def web_audit_detail(store: AuditStore, identifier: str) -> dict[str, Any] | None:
    """按 event_id 或 invocation_id 读取单项安全详情；不存在时返回 ``None``。

    详情仍只经过 ``web_audit_item``，因此不会因为单项接口而暴露原始 payload、
    diagnostic 附件、traceback、物理路径或完整异常。标识匹配不替缺失 session 信息。
    """
    safe_identifier = _web_identifier(identifier, 160)
    if not safe_identifier:
        return None
    loaded = store.read_events()
    events = loaded.get("events", []) if isinstance(loaded, dict) else []
    for item in combine_invocations(events if isinstance(events, list) else []):
        if safe_identifier in {
            str(item.get("event_id") or ""), str(item.get("finish_event_id") or ""),
            str(item.get("invocation_id") or ""),
        }:
            return web_audit_item(item)
    return None


def render_markdown(items: list[dict[str, Any]], summary: dict[str, Any], title_date: str) -> str:
    """暂时弃用：保留 Markdown 审计报告，供已有自动化和兼容性使用。

    新的人类审计入口使用 :func:`write_excel_report` 生成可筛选的 Excel 工作簿。
    Markdown 仍是可重建派生物，且不会反向修改 JSONL 审计事实源。
    """
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
    lines.extend(["", "## 调用明细", "", "| 时间 | 会话 | Agent | 来源 | 动作说明 | 结果说明 | 耗时 |", "|---|---|---|---|---|---|---:|"])
    for item in items:
        timestamp = item.get("started_at") or item.get("timestamp") or ""
        duration = "" if item.get("duration_ms") is None else f"{item['duration_ms']} ms"
        lines.append(
            f"| {esc(timestamp)} | {esc(item.get('session_label'))} | {esc(item.get('agent'))} | {esc(item.get('source'))} | "
            f"{esc(item.get('action_text'))} | {esc(item.get('result_text'))} | {esc(duration)} |"
        )
    if summary["damaged"]:
        lines.extend(["", "## 无法解析的记录", ""] + [f"- `{path}`" for path in summary["damaged"]])
    return "\n".join(lines) + "\n"


def write_report(path: Path, content: str) -> None:
    """暂时弃用：原子写入 Markdown 报告，供 ``audit report-md`` 兼容入口使用。"""
    _validate_report_path(path, suffix=".md", format_name="Markdown")
    _write_report_bytes(path, content.encode("utf-8"))


def _validate_report_path(path: Path, *, suffix: str, format_name: str) -> Path:
    """校验派生报告输出位置，避免把原子替换目标误传成目录。

    ``os.replace`` 面对目录目标会在 Windows 报出不直观的 ``WinError 5``。在写入前
    统一验证目标类型和扩展名，使用户能直接修正命令，并防止将 Excel 内容保存为 Markdown
    等错误后缀。返回值是解析后的绝对路径，供所有报告格式复用。
    """
    path = path.expanduser().resolve()
    if path.exists() and path.is_dir():
        raise ValueError(f"--output 必须是报告文件路径，不能是目录：{path}")
    if path.parent.exists() and not path.parent.is_dir():
        raise ValueError(f"--output 的父路径不是目录：{path.parent}")
    if path.suffix.lower() != suffix:
        raise ValueError(f"--output 必须使用 {suffix} 扩展名以生成 {format_name} 报告：{path}")
    return path


def _write_report_bytes(path: Path, content: bytes) -> None:
    """原子写入二进制报告；失败时保留原报告且不遗留临时文件。"""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _excel_column(index: int) -> str:
    """把从零开始的列序号转换为 OOXML 使用的 A1 列名。"""
    result = ""
    while True:
        index, remainder = divmod(index, 26)
        result = chr(ord("A") + remainder) + result
        if index == 0:
            return result
        index -= 1


def _excel_text(value: Any) -> str:
    """将审计值转换成安全、可显示的单元格文本，避免 XML 和公式注入边界。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    # 使用 inlineStr 强制作为文本保存，避免以 =、+、-、@ 开头的外部输入被 Excel 当成公式。
    text = _redact_text(str(value), limit=2_000)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _xlsx_cell(reference: str, value: Any, style: int = 0, *, numeric: bool = False) -> str:
    """生成单个 OOXML 单元格；审计文本统一使用 inlineStr，不信任外部输入。"""
    style_attr = f' s="{style}"' if style else ""
    if numeric and isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{reference}"{style_attr}><v>{value}</v></c>'
    text = xml_escape(_excel_text(value))
    preserve = ' xml:space="preserve"' if text[:1].isspace() or text[-1:].isspace() else ""
    return f'<c r="{reference}"{style_attr} t="inlineStr"><is><t{preserve}>{text}</t></is></c>'


def _xlsx_row(row_number: int, values: list[tuple[Any, int, bool]]) -> str:
    """生成一行 OOXML，参数依次为值、样式索引和是否按数值写入。"""
    cells = "".join(
        _xlsx_cell(f"{_excel_column(column)}{row_number}", value, style, numeric=numeric)
        for column, (value, style, numeric) in enumerate(values)
    )
    return f'<row r="{row_number}">{cells}</row>'


def _xlsx_sheet_xml(
    rows: list[str], *, columns: list[float], freeze_row: int | None = None, freeze_columns: int | None = None,
    auto_filter: str | None = None, merges: list[str] | None = None,
) -> str:
    """封装通用工作表 XML，集中处理列宽、冻结窗格、筛选和合并区域。"""
    column_xml = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(columns, start=1)
    )
    pane = ""
    if freeze_row or freeze_columns:
        split_row = freeze_row or 0
        split_columns = freeze_columns or 0
        top_left = f"{_excel_column(split_columns)}{split_row + 1}"
        active_pane = "bottomRight" if split_row and split_columns else ("bottomLeft" if split_row else "topRight")
        pane = (
            f'<pane xSplit="{split_columns}" ySplit="{split_row}" topLeftCell="{top_left}" '
            f'activePane="{active_pane}" state="frozen"/>'
        )
    merge_xml = "".join(f'<mergeCell ref="{reference}"/>' for reference in (merges or []))
    merge_section = f'<mergeCells count="{len(merges or [])}">{merge_xml}</mergeCells>' if merges else ""
    filter_section = f'<autoFilter ref="{auto_filter}"/>' if auto_filter else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetViews><sheetView workbookViewId="0" showGridLines="0">{pane}</sheetView></sheetViews>'
        f'<cols>{column_xml}</cols><sheetData>{"".join(rows)}</sheetData>{merge_section}{filter_section}</worksheet>'
    )


def _xlsx_styles_xml() -> str:
    """提供审计工作簿所需的紧凑样式表；样式按角色而非逐格随意上色。"""
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="3"><font><sz val="10"/><name val="Aptos"/></font><font><b/><sz val="16"/><color rgb="FFFFFFFF"/><name val="Aptos Display"/></font><font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Aptos"/></font></fonts>
  <fills count="7"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFD9EAF7"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFE2F0D9"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFCE4D6"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFFE699"/><bgColor indexed="64"/></patternFill></fill></fills>
  <borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left style="thin"><color rgb="FFD9E2F3"/></left><right style="thin"><color rgb="FFD9E2F3"/></right><top style="thin"><color rgb="FFD9E2F3"/></top><bottom style="thin"><color rgb="FFD9E2F3"/></bottom><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="9"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf><xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf><xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0"/><xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0"/><xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0"/><xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0"/><xf numFmtId="0" fontId="0" fillId="6" borderId="1" xfId="0"/><xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''


def write_excel_report(path: Path, items: list[dict[str, Any]], summary: dict[str, Any], title_date: str) -> None:
    """将审计聚合结果生成可筛选的 Excel 工作簿，且不引入第三方运行时依赖。

    审计 CLI 需要在已安装的 Agent 环境中独立运行，不能依赖当前 Codex 会话的 Node
    包或 Python 扩展。因此仅使用 Python 标准库写入稳定的 OOXML/ZIP 容器；输出只包含
    已脱敏的审计聚合字段。生成失败向调用者报告，由 CLI 决定退出码，不影响审计事实源。
    """
    path = _validate_report_path(path, suffix=".xlsx", format_name="Excel")
    generated_at = _now().isoformat(timespec="seconds")
    overview_rows = [
        _xlsx_row(1, [(f"AIKB 审计报告：{title_date}", 1, False)]),
        _xlsx_row(3, [("报告日期", 3, False), (title_date, 4, False), ("生成时间", 3, False), (generated_at, 4, False)]),
        _xlsx_row(5, [("审计总览", 2, False), ("数量", 2, False)]),
        _xlsx_row(6, [("逻辑事件", 3, False), (summary["count"], 4, True)]),
    ]
    for offset, status in enumerate(("succeeded", "noop", "blocked", "failed", "incomplete"), start=7):
        style = {"succeeded": 5, "failed": 6, "blocked": 7}.get(status, 4)
        overview_rows.append(_xlsx_row(offset, [(status, 3, False), (summary["statuses"].get(status, 0), style, True)]))
    overview_rows.extend([
        _xlsx_row(12, [("fallback 记录", 3, False), (summary["fallback_records"], 4, True)]),
        _xlsx_row(13, [("损坏记录", 3, False), (summary["damaged_count"], 4, True)]),
        _xlsx_row(14, [("平均耗时 (ms)", 3, False), (summary["average_duration_ms"], 4, True)]),
        _xlsx_row(15, [("最近活动", 3, False), (summary["last_activity"], 4, False)]),
        _xlsx_row(17, [("Agent", 2, False), ("调用次数", 2, False), ("来源", 2, False), ("调用次数", 2, False)]),
    ])
    overview_rows.extend(
        _xlsx_row(row, [(agent, 4, False), (count, 4, True), (source, 4, False), (source_count, 4, True)])
        for row, ((agent, count), (source, source_count)) in enumerate(
            zip(sorted(summary["agents"].items()), sorted(summary["sources"].items())), start=18
        )
    )
    # Agent 和来源的行数可能不同，补齐未被 zip 覆盖的尾部，保证两个汇总表信息完整。
    tail_row = 18 + min(len(summary["agents"]), len(summary["sources"]))
    for agent, count in list(sorted(summary["agents"].items()))[len(summary["sources"]):]:
        overview_rows.append(_xlsx_row(tail_row, [(agent, 4, False), (count, 4, True), ("", 4, False), ("", 4, False)]))
        tail_row += 1
    for source, count in list(sorted(summary["sources"].items()))[len(summary["agents"]):]:
        overview_rows.append(_xlsx_row(tail_row, [("", 4, False), ("", 4, False), (source, 4, False), (count, 4, True)]))
        tail_row += 1

    headers = ["开始时间", "结束时间", "会话名称", "Agent", "来源", "操作", "动作说明", "结果说明", "状态", "耗时 (ms)", "项目 ID", "诊断级别", "调用 ID", "原始会话 ID", "技术动作摘要", "技术结果摘要", "错误类型", "Fallback"]
    detail_rows = [_xlsx_row(1, [(header, 2, False) for header in headers])]
    for row_number, item in enumerate(items, start=2):
        action = item.get("action") if item.get("action") is not None else ""
        result = item.get("result_summary") if item.get("result_summary") is not None else ""
        status = str(item.get("status") or "")
        status_style = {"succeeded": 5, "failed": 6, "blocked": 7}.get(status, 4)
        detail_rows.append(_xlsx_row(row_number, [
            (item.get("started_at") or item.get("timestamp"), 4, False), (item.get("finished_at"), 4, False),
            (item.get("session_label"), 4, False), (item.get("agent"), 4, False), (item.get("source"), 4, False),
            (item.get("operation"), 4, False), (item.get("action_text"), 8, False), (item.get("result_text"), 8, False),
            (status, status_style, False), (item.get("duration_ms"), 4, True), (item.get("project_id"), 4, False),
            (item.get("capture_level"), 4, False), (item.get("invocation_id") or item.get("event_id"), 4, False),
            (item.get("session_id"), 4, False), (action, 8, False), (result, 8, False),
            (item.get("error_type"), 4, False), ("是" if item.get("_fallback") else "否", 4, False),
        ]))
    damaged_rows = [_xlsx_row(1, [("无法解析的审计文件", 2, False)])]
    damaged_rows.extend(_xlsx_row(row, [(damaged, 4, False)]) for row, damaged in enumerate(summary["damaged"], start=2))

    overview = _xlsx_sheet_xml(overview_rows, columns=[24, 18, 20, 30], merges=["A1:D1"])
    details = _xlsx_sheet_xml(detail_rows, columns=[25, 25, 38, 18, 12, 26, 44, 44, 14, 14, 25, 16, 40, 28, 40, 36, 24, 12], freeze_row=1, freeze_columns=6, auto_filter=f"A1:R{max(1, len(items) + 1)}")
    damaged = _xlsx_sheet_xml(damaged_rows, columns=[100], freeze_row=1, auto_filter=f"A1:A{max(1, len(summary['damaged']) + 1)}")
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet3.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'''
    relationships = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'''
    workbook = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="概览" sheetId="1" r:id="rId1"/><sheet name="调用明细" sheetId="2" r:id="rId2"/><sheet name="损坏记录" sheetId="3" r:id="rId3"/></sheets></workbook>'''
    workbook_relationships = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet3.xml"/><Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'''
    with tempfile.TemporaryDirectory(prefix="aikb-audit-xlsx-") as temp_dir:
        archive_path = Path(temp_dir) / "report.xlsx"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in {
                "[Content_Types].xml": content_types, "_rels/.rels": relationships, "xl/workbook.xml": workbook,
                "xl/_rels/workbook.xml.rels": workbook_relationships, "xl/styles.xml": _xlsx_styles_xml(),
                "xl/worksheets/sheet1.xml": overview, "xl/worksheets/sheet2.xml": details,
                "xl/worksheets/sheet3.xml": damaged,
            }.items():
                archive.writestr(name, content.encode("utf-8"))
        _write_report_bytes(path, archive_path.read_bytes())
