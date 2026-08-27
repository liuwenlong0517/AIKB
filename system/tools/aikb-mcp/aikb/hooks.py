"""把 Codex/Claude Code 生命周期事件转换为本机状态提示。"""

from __future__ import annotations

import json
from typing import Any

from .audit import AuditStore, audit_project_id
from .config import Settings
from .workstate import WorkStateStore


def handle_hook(agent: str, event: str, payload: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    """处理一个 hook 事件；仅唯一活动任务注入恢复信息，多候选只记录而不注入。"""
    resolved_settings = settings or Settings.load()
    audit = AuditStore(resolved_settings)
    project_path = str(payload.get("cwd") or payload.get("project_path") or "").strip()
    normalized_event = event.lower().replace("_", "-")
    normalized_event = {
        "sessionstart": "session-start", "precompact": "pre-compact", "sessionend": "session-end",
    }.get(normalized_event, normalized_event)
    session_id = str(
        payload.get("session_id") or payload.get("sessionId") or payload.get("conversation_id") or ""
    ) or None
    project = audit_project_id(project_path)
    session_label = audit.resolve_session_label(
        agent=agent, source="hook", session_id=session_id, connection_id=None, project_id=project,
        supplied_label=str(payload.get("session_name") or payload.get("conversation_title") or "") or None,
    )
    invocation: dict[str, Any] | None = None
    try:
        invocation = audit.start(
            source="hook", agent=agent, operation=normalized_event,
            action={"event": normalized_event, "project_id": project}, session_id=session_id, session_label=session_label, project_id=project,
        )
        audit.write_diagnostic(
            invocation_id=invocation["invocation_id"], source="hook", agent=agent, operation=normalized_event, phase="input",
            session_id=session_id, session_label=session_label, payload={"payload": payload},
        )
    except Exception:
        pass

    def finish(status: str, outcome_code: str, result_summary: dict[str, Any] | None = None) -> None:
        if not invocation:
            return
        try:
            audit.finish(
                invocation, source="hook", agent=agent, operation=normalized_event, status=status,
                outcome_code=outcome_code, result_summary=result_summary, session_id=session_id, session_label=session_label, project_id=project,
            )
            audit.write_diagnostic(
                invocation_id=invocation["invocation_id"], source="hook", agent=agent, operation=normalized_event, phase="output",
                session_id=session_id, session_label=session_label, payload={"outcome_code": outcome_code, "result_summary": result_summary},
            )
        except Exception:
            pass

    try:
        if not project_path:
            finish("noop", "invalid_project")
            return {}
        store = WorkStateStore(resolved_settings)
        state = store.get(project_path=project_path, limit=2)
        if not state["unique"]:
            outcome = "no_active_work" if state["count"] == 0 else "multiple_active_work"
            finish("noop", outcome, {"candidate_count": state["count"]})
            return {}
        item = state["items"][0]
        if normalized_event == "session-start":
            context = (
                "AIKB 发现一个本机活动任务。仅当用户当前请求是在继续该任务时使用；继续前核对 Git 分支、revision 和工作区。\n"
                + item["resume_capsule"]
            )[:1800]
            finish("succeeded", "resume_context_injected", {"work_id": item.get("work_id")})
            return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
        if normalized_event == "stop":
            if bool(payload.get("stop_hook_active")):
                finish("noop", "recursion_skipped", {"work_id": item.get("work_id")})
                return {}
            if store.is_dirty_since_checkpoint(project_path, item):
                finish("blocked", "checkpoint_required", {"work_id": item.get("work_id")})
                return {
                    "decision": "block",
                    "reason": (
                        "活动任务的 Git 状态已在最后检查点后发生变化。请先调用 AIKB checkpoint_work_state 写入紧凑状态；"
                        "不要保存聊天全文、隐藏推理、原始日志或完整 diff。"
                    ),
                }
            finish("noop", "git_unchanged", {"work_id": item.get("work_id")})
            return {}
        if normalized_event == "pre-compact":
            finish("succeeded", "pre_compact_observed", {"work_id": item.get("work_id")})
            return {}
        if normalized_event == "session-end":
            finish("succeeded", "session_end_observed", {"work_id": item.get("work_id")})
            return {}
        finish("noop", "unsupported_event", {"work_id": item.get("work_id")})
        return {}
    except Exception as exc:
        if invocation:
            try:
                audit.finish(
                    invocation, source="hook", agent=agent, operation=normalized_event, status="failed",
                    outcome_code="handler_failed", error_type=type(exc).__name__, session_id=session_id,
                    session_label=session_label, project_id=project,
                )
            except Exception:
                pass
        raise


def hook_json(agent: str, event: str, payload: dict[str, Any], settings: Settings | None = None) -> str:
    """将 ``handle_hook`` 的结果编码为紧凑 UTF-8 JSON，供 PowerShell 管道传递。"""
    return json.dumps(handle_hook(agent, event, payload, settings), ensure_ascii=False, separators=(",", ":"))
