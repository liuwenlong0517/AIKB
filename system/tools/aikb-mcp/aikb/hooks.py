from __future__ import annotations

import json
from typing import Any

from .config import Settings
from .workstate import WorkStateStore


def handle_hook(agent: str, event: str, payload: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    store = WorkStateStore(settings or Settings.load())
    project_path = str(payload.get("cwd") or payload.get("project_path") or "").strip()
    if not project_path:
        return {}
    state = store.get(project_path=project_path, limit=2)
    if not state["unique"]:
        return {}
    item = state["items"][0]
    normalized_event = event.lower().replace("_", "-")
    if normalized_event == "session-start":
        context = (
            "AIKB 发现一个本机活动任务。仅当用户当前请求是在继续该任务时使用；继续前核对 Git 分支、revision 和工作区。\n"
            + item["resume_capsule"]
        )[:1800]
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
    if normalized_event == "stop":
        already_active = bool(payload.get("stop_hook_active"))
        if not already_active and store.is_dirty_since_checkpoint(project_path, item):
            return {
                "decision": "block",
                "reason": (
                    "活动任务的 Git 状态已在最后检查点后发生变化。请先调用 AIKB checkpoint_work_state 写入紧凑状态；"
                    "不要保存聊天全文、隐藏推理、原始日志或完整 diff。"
                ),
            }
    return {}


def hook_json(agent: str, event: str, payload: dict[str, Any], settings: Settings | None = None) -> str:
    return json.dumps(handle_hook(agent, event, payload, settings), ensure_ascii=False, separators=(",", ":"))
