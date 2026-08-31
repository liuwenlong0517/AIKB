"""把 Codex/Claude Code 生命周期事件转换为本机状态提示。"""

from __future__ import annotations

import json
from typing import Any

from .audit import AuditStore, audit_project_id
from .config import Settings
from .indexer import review_report
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

    def knowledge_review_reminder() -> str:
        """把审查队列压缩为可观察的 SessionStart 节奏；失败时保持 hook fail-open。

        这里只报告数量和有限状态，不自动晋升、关闭、删除或修改候选条目；正式
        ``review_when`` 仍需维护者按自然语言条件人工判断。
        """
        try:
            report = review_report(resolved_settings)
        except Exception:
            return ""
        candidates = report["candidates"]
        review_items = report["review_items"]
        summary = report.get("summary", {})
        messages: list[str] = []
        # 每次 SessionStart 都报告总数，即使当前队列为空，避免“没有提醒”被误解
        # 为没有审查机制；其余计数只在命中时突出显示，保持上下文紧凑。
        messages.append(
            f"candidate 总数 {summary.get('candidate_count', len(candidates))}（截至 {summary.get('as_of', '未知')}）。"
        )
        if not report["valid"]:
            # 校验失败时仍保留总数，让 SessionStart 具备稳定节奏；不把底层错误文本
            # 直接拼入上下文，维护者可通过 validate 命令查看完整定位。
            messages.append("知识元数据校验未通过，请运行 `aikb validate` 后再写入或晋升。")
            return "AIKB 知识审查提醒：" + "".join(messages)
        if summary.get("overdue_count"):
            messages.append(f"逾期 {summary['overdue_count']} 个。")
        if summary.get("unowned_count"):
            messages.append(f"无 owner {summary['unowned_count']} 个。")
        if summary.get("duplicate_declared_count"):
            messages.append(f"声明可能重复 {summary['duplicate_declared_count']} 个。")
        if summary.get("closed_still_in_inbox_count"):
            messages.append(f"已结案但仍留在 Inbox {summary['closed_still_in_inbox_count']} 个。")
        if candidates:
            messages.append("查重需显式搜索 status=verified 和 status=candidate。")
        if review_items:
            messages.append(
                f"有 {len(review_items)} 个正式条目记录 review_when 条件；请按条件人工复核，系统不自动判断自然语言条件是否满足。"
            )
        return "AIKB 知识审查提醒：" + "".join(messages)

    def session_binding_hint() -> str:
        """向 Agent 暴露本次 Hook 观测到的会话绑定；缺失时明确安全降级。"""
        if session_id:
            safe_session = session_id.replace("\r", " ").replace("\n", " ")[:120]
            return (
                f"AIKB 当前会话绑定：agent={agent}，session_id={safe_session}。"
                "创建或续写 checkpoint 时请原样传递该 session_id。"
            )
        return (
            "AIKB 当前 Hook 未提供 session_id，已降级为不自动注入/不执行任务归属门禁；"
            "不会按 Agent 单独接管 Working State。"
        )

    try:
        if not project_path:
            finish("noop", "invalid_project")
            if normalized_event == "session-start":
                # SessionStart 仍应保持审查节奏；缺少项目路径时不能恢复任务，
                # 但全局知识队列摘要不依赖项目路径，继续安全返回固定提醒。
                reminder = knowledge_review_reminder()
                if reminder:
                    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": reminder[:1800]}}
            return {}
        store = WorkStateStore(resolved_settings)
        # 项目级唯一任务不等于当前会话所属任务：先读取有限候选，再按持久
        # owner/participant 过滤，避免无关 Agent 被 SessionStart/Stop 牵连。
        all_state = store.get(project_path=project_path, limit=20)
        state = store.get(
            project_path=project_path, limit=20, actor_agent=agent,
            actor_session_id=session_id, authorized_only=True,
        )
        reminder = knowledge_review_reminder() if normalized_event == "session-start" else ""
        if all_state["count"] and not state["count"]:
            finish(
                "noop", "foreign_active_work",
                {
                    "candidate_count": all_state["count"],
                    "knowledge_review_reminder": bool(reminder),
                    # Hook 与 MCP 可能拿到不同会话标识；没有精确会话匹配时
                    # 只能降级为不触碰，绝不退化为 agent-only 自动接管。
                    "binding_strength": "agent+exact-session-required",
                    "session_observed": bool(session_id),
                },
            )
            if normalized_event == "session-start":
                context = (
                    session_binding_hint() + "\n"
                    "AIKB 检测到项目存在其他会话的活动任务，未自动恢复（binding_strength="
                    "agent+exact-session-required）。如需继续请先显式认领或交接。\n" + reminder
                )
                return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context[:1800]}}
            return {}
        if not state["unique"]:
            outcome = "no_active_work" if state["count"] == 0 else "multiple_active_work"
            finish("noop", outcome, {"candidate_count": state["count"], "knowledge_review_reminder": bool(reminder)})
            if normalized_event == "session-start":
                context = session_binding_hint()
                if reminder:
                    context += "\n" + reminder
                return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context[:1800]}}
            return {}
        item = state["items"][0]
        if normalized_event == "session-start":
            base_context = (
                session_binding_hint() + "\n"
                "AIKB 发现一个本机活动任务。仅当用户当前请求是在继续该任务时使用；继续前核对 Git 分支、revision 和工作区。\n"
                + item["resume_capsule"]
            )
            if reminder:
                # 给审查提醒保留固定空间，避免超长恢复胶囊把候选/复核提示截掉。
                context = base_context[: max(0, 1799 - len(reminder))] + "\n" + reminder
            else:
                context = base_context[:1800]
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
