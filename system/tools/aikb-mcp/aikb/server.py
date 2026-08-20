from __future__ import annotations

import json
import sys
import traceback
from typing import Any

from .config import Settings
from .knowledge import KnowledgeService, compact_json
from .workstate import WorkStateStore


SERVER_INSTRUCTIONS = (
    "AIKB 是本机工程知识与任务状态服务。未知知识位置时调用 search_knowledge；已知稳定 ID 后调用 read_knowledge。"
    "继续历史任务时调用 get_work_state。只有形成有意义的工程状态时才写 checkpoint，禁止保存聊天全文、隐藏推理、密钥、原始日志和完整 diff。"
    "正式知识 Markdown 只读；MCP 不可用时按 INDEX.md 和局部 README 降级。"
)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_knowledge",
        "description": "按关键词和元数据发现 AIKB 知识；默认返回少量定位片段，不返回整篇文档。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "中文或英文查询"},
                "type": {"type": "string", "description": "可选知识类型过滤"},
                "status": {"type": "string", "default": "verified"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                "excerpt_chars": {"type": "integer", "minimum": 120, "maximum": 1600, "default": 700},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "read_knowledge",
        "description": "按稳定 ID 或准确路径读取当前 Markdown；可限于一个章节和字符预算。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id_or_path": {"type": "string"},
                "section": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 300, "maximum": 12000, "default": 4000},
                "include_relations": {"type": "boolean", "default": True},
            },
            "required": ["id_or_path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "get_work_state",
        "description": "查找本机活动任务并返回紧凑恢复胶囊；不读取聊天记录。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string"},
                "work_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "checkpoint_work_state",
        "description": "为任务追加结构化本机检查点；只写 workspace/，不会写正式知识。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string"}, "work_id": {"type": "string"}, "goal": {"type": "string"},
                "status": {"type": "string", "enum": ["planned", "active", "blocked"], "default": "active"},
                "agent": {"type": "string"}, "session_id": {"type": "string"}, "role": {"type": "string"},
                "decisions": {"type": "array", "items": {"type": "string"}},
                "verified_facts": {"type": "array", "items": {"type": "string"}},
                "current_state": {"type": "string"}, "completed": {"type": "array", "items": {"type": "string"}},
                "changed_files": {"type": "array", "items": {"type": "string"}},
                "verification": {"type": "array", "items": {"type": "string"}},
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "blockers": {"type": "array", "items": {"type": "string"}},
                "next_steps": {"type": "array", "items": {"type": "string"}},
                "candidate_knowledge": {"type": "array", "items": {"type": "string"}},
                "resume_checks": {"type": "array", "items": {"type": "string"}},
                "based_on": {"type": "string"}, "sensitivity": {"type": "string", "default": "normal"},
            },
            "required": ["project_path", "agent", "session_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "close_work_state",
        "description": "完成、放弃或替代一个活动任务，并将其移入本机归档。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "work_id": {"type": "string"},
                "status": {"type": "string", "enum": ["completed", "abandoned", "superseded"]},
                "agent": {"type": "string"}, "session_id": {"type": "string"}, "note": {"type": "string"},
            },
            "required": ["work_id", "status", "agent", "session_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
]


class MCPServer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.knowledge = KnowledgeService(settings)
        self.work = WorkStateStore(settings)

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            return None
        try:
            if method == "initialize":
                requested = message.get("params", {}).get("protocolVersion")
                protocol = requested if requested in {"2024-11-05", "2025-03-26", "2025-06-18"} else "2025-06-18"
                result = {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "aikb", "version": "0.1.0"},
                    "instructions": SERVER_INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = message.get("params") or {}
                result = self.call_tool(str(params.get("name") or ""), params.get("arguments") or {})
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method == "logging/setLevel":
                result = {}
            else:
                return self._error(request_id, -32601, f"Method not found: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:  # MCP boundary must turn failures into tool/protocol errors.
            return self._error(request_id, -32603, str(exc))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "search_knowledge":
                value = self.knowledge.search(
                    str(arguments.get("query") or ""), entry_type=arguments.get("type"),
                    status=str(arguments.get("status") or "verified"), tags=arguments.get("tags"),
                    limit=int(arguments.get("limit", 5)), excerpt_chars=int(arguments.get("excerpt_chars", 700)),
                )
            elif name == "read_knowledge":
                value = self.knowledge.read(
                    str(arguments.get("id_or_path") or ""), section=arguments.get("section"),
                    max_chars=int(arguments.get("max_chars", 4000)),
                    include_relations=bool(arguments.get("include_relations", True)),
                )
            elif name == "get_work_state":
                value = self.work.get(
                    project_path=arguments.get("project_path"), work_id=arguments.get("work_id"),
                    limit=int(arguments.get("limit", 5)),
                )
            elif name == "checkpoint_work_state":
                value = self.work.checkpoint(arguments)
            elif name == "close_work_state":
                value = self.work.close(
                    str(arguments.get("work_id") or ""), status=str(arguments.get("status") or ""),
                    agent=str(arguments.get("agent") or "unknown"), session_id=str(arguments.get("session_id") or ""),
                    note=str(arguments.get("note") or ""),
                )
            else:
                raise KeyError(f"未知工具：{name}")
            return {"content": [{"type": "text", "text": compact_json(value)}], "isError": False}
        except Exception as exc:
            return {"content": [{"type": "text", "text": compact_json({"error": str(exc)})}], "isError": True}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def run(self) -> None:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n", write_through=True)
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
                response = self.handle(message)
                if response is not None:
                    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)
            except Exception:
                traceback.print_exc(file=sys.stderr)


def run_server(settings: Settings | None = None) -> None:
    MCPServer(settings or Settings.load()).run()
