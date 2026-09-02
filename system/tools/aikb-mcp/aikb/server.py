"""实现面向本机 Agent 的最小 JSON-RPC MCP stdio 服务。"""

from __future__ import annotations

import json
import sys
from typing import Any

from .audit import AuditStore, audit_project_id, summarize_tool_action, summarize_tool_result
from .config import Settings
from .indexer import review_report
from .knowledge import KnowledgeService, compact_json
from .workstate import WorkStateStore


SERVER_INSTRUCTIONS = (
    "AIKB 是本机工程知识与任务状态服务。未知知识位置时调用 search_knowledge；已知稳定 ID 后调用 read_knowledge。"
    "search_knowledge 默认只返回 verified；查重必须显式覆盖 verified 和 candidate，或调用 review_knowledge 查看审查队列。"
    "继续历史任务时调用 get_work_state。只有形成有意义的工程状态时才写 checkpoint，禁止保存聊天全文、隐藏推理、密钥、原始日志和完整 diff。"
    "正式知识 Markdown 只读；MCP 不可用时按根 INDEX.md 和各级 INDEX.md 降级。"
)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_knowledge",
        "description": "按关键词和元数据发现 AIKB 知识；默认只查 verified，查重时须显式再查 candidate；返回少量定位片段。",
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
        "name": "review_knowledge",
        "description": "列出 candidate 晋升队列和带 review_when 的正式条目；只读，不自动修改知识。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
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
        "description": "查找本机活动任务并返回紧凑恢复胶囊；不读取聊天记录。省略 project_path 时返回全部项目（跨项目）活动任务，必须自行核对项目。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "description": "可选项目路径；省略时查询全部项目，结果可能跨项目。"},
                "work_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "checkpoint_work_state",
        "description": "owner/participant 追加检查点；只写 workspace/。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string"}, "work_id": {"type": "string"}, "goal": {"type": "string"},
                "status": {"type": "string", "enum": ["planned", "active", "blocked"], "default": "active"},
                "agent": {"type": "string"}, "session_id": {"type": "string", "minLength": 1, "maxLength": 160}, "role": {"type": "string"},
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
                "repositories": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {"role": {"type": "string"}, "path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
                "based_on": {"type": "string"}, "sensitivity": {"type": "string", "default": "normal"},
            },
            "required": ["project_path", "agent", "session_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "close_work_state",
        "description": "owner/participant 关闭活动任务并归档。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "work_id": {"type": "string"},
                "status": {"type": "string", "enum": ["completed", "abandoned", "superseded"]},
                "agent": {"type": "string"}, "session_id": {"type": "string", "minLength": 1, "maxLength": 160}, "note": {"type": "string"},
            },
            "required": ["work_id", "status", "agent", "session_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "claim_work_state",
        "description": "显式认领旧活动任务。",
        "inputSchema": {
            "type": "object",
            "properties": {"work_id": {"type": "string"}, "agent": {"type": "string"}, "session_id": {"type": "string", "minLength": 1, "maxLength": 160}, "upgrade_legacy_session": {"type": "boolean", "default": False}},
            "required": ["work_id", "agent", "session_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "authorize_work_participant",
        "description": "owner 管理精确 Agent/会话续写；mode 支持 shared/handed-off/revoke。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "work_id": {"type": "string"}, "owner_agent": {"type": "string"}, "owner_session_id": {"type": "string", "minLength": 1, "maxLength": 160},
                "participant_agent": {"type": "string"}, "participant_session_id": {"type": "string", "minLength": 1, "maxLength": 160}, "role": {"type": "string"},
                "mode": {"type": "string", "enum": ["shared", "handed-off", "revoke"], "default": "shared"},
            },
            "required": ["work_id", "owner_agent", "owner_session_id", "participant_agent", "participant_session_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
]


def _validate_schema_value(value: Any, schema: dict[str, Any], path: str) -> None:
    """校验 MCP 工具声明实际使用的 JSON Schema 子集。

    当前工具契约只依赖 object/array/string/integer/boolean、required、
    additionalProperties、enum 和长度/数值上下限。这里在调用业务代码前严格
    校验这些约束，避免 Python 的 ``str()``/``int()`` 隐式强转掩盖客户端错误；
    若未来声明引入新关键字，应先扩展本函数及边界测试，而不是静默忽略。
    """
    expected_type = schema.get("type")
    type_matches = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        # bool 是 Python 的 int 子类，但 JSON Schema 明确区分 boolean/integer。
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
    }
    if expected_type in type_matches and not type_matches[expected_type](value):
        raise ValueError(f"{path} 必须是 {expected_type}")

    if "enum" in schema and value not in schema["enum"]:
        allowed = "、".join(str(item) for item in schema["enum"])
        raise ValueError(f"{path} 必须是以下值之一：{allowed}")

    if expected_type == "object":
        properties = schema.get("properties") or {}
        for required_name in schema.get("required") or []:
            if required_name not in value:
                raise ValueError(f"{path}.{required_name} 为必填字段")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ValueError(f"{path} 包含未声明字段：{', '.join(unknown)}")
        for name, item in value.items():
            child_schema = properties.get(name)
            if child_schema is not None:
                _validate_schema_value(item, child_schema, f"{path}.{name}")
    elif expected_type == "array":
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise ValueError(f"{path} 项数不得超过 {schema['maxItems']}")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                _validate_schema_value(item, item_schema, f"{path}[{index}]")
    elif expected_type == "string":
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            raise ValueError(f"{path} 长度不得小于 {schema['minLength']}")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise ValueError(f"{path} 长度不得超过 {schema['maxLength']}")
    elif expected_type == "integer":
        if "minimum" in schema and value < int(schema["minimum"]):
            raise ValueError(f"{path} 不得小于 {schema['minimum']}")
        if "maximum" in schema and value > int(schema["maximum"]):
            raise ValueError(f"{path} 不得超过 {schema['maximum']}")


class MCPServer:
    """把 MCP 协议方法路由到知识服务和 Working State 存储。"""

    def __init__(self, settings: Settings, agent: str = "unknown"):
        """初始化共享路径设置下的知识和任务状态服务。"""
        self.settings = settings
        self.agent = agent or "unknown"
        self.connection_id = AuditStore.new_id()
        self.client: dict[str, Any] = {}
        self.audit = AuditStore(settings)
        self.knowledge = KnowledgeService(settings)
        self.work = WorkStateStore(settings)

    def _validate_work_actor(self, name: str, arguments: dict[str, Any]) -> None:
        """校验写入 payload 与 MCP ``serve --agent`` 一致，拒绝自报他方身份。"""
        if name not in {
            "checkpoint_work_state", "close_work_state", "claim_work_state",
            "authorize_work_participant",
        }:
            return
        field = "owner_agent" if name == "authorize_work_participant" else "agent"
        declared = str(arguments.get(field) or "").strip().lower()
        bound = str(self.agent or "").strip().lower()
        if not bound or bound == "unknown":
            raise PermissionError("MCP 服务未绑定 Agent；请使用 serve --agent codex|claude-code")
        if not declared or declared != bound:
            raise PermissionError(f"{field} 与 MCP 服务绑定 Agent 不一致，拒绝 Working State 写入")

    @staticmethod
    def _tool_schema(name: str) -> dict[str, Any] | None:
        """按工具名返回对客户端公开的同一份输入契约，避免校验规则另起一套。"""
        return next((tool["inputSchema"] for tool in TOOLS if tool["name"] == name), None)

    def process_line(self, raw: str) -> dict[str, Any] | None:
        """解析并处理一行 JSON-RPC；语法错误返回标准 Parse error。

        JSON 语法错误尚未形成可关联的请求，因此响应 ID 固定为 ``null``；解析
        成功但顶层不是对象则属于 Invalid Request。两类输入都在协议边界内消化，
        不向 stderr 泄漏 traceback，也不影响后续行继续处理。
        """
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return self._error(None, -32700, "Parse error")
        if not isinstance(message, dict):
            return self._error(None, -32600, "Invalid Request: JSON-RPC 请求必须是对象")
        return self.handle(message)

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """处理一个 JSON-RPC 请求；通知类消息无 ID 时不返回响应。"""
        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            return None
        try:
            if method == "initialize":
                params = message.get("params") or {}
                requested = params.get("protocolVersion")
                client_info = params.get("clientInfo") or {}
                self.client = {
                    "name": str(client_info.get("name") or "unknown")[:120],
                    "version": str(client_info.get("version") or "")[:80] or None,
                }
                try:
                    self.audit.connection_initialized(
                        agent=self.agent, client=self.client, connection_id=self.connection_id
                    )
                except Exception:
                    pass
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
                # 字段缺省时使用空对象；字段已出现则保留原类型供协议校验，避免
                # ``[]``、空字符串等假值被 ``or {}`` 静默伪装成合法参数对象。
                params = message.get("params") if "params" in message else {}
                if not isinstance(params, dict):
                    return self._error(request_id, -32602, "Invalid params: params 必须是对象")
                name = str(params.get("name") or "")
                arguments = params.get("arguments") if "arguments" in params else {}
                if not isinstance(arguments, dict):
                    return self._error(request_id, -32602, "Invalid params: arguments 必须是对象")
                schema = self._tool_schema(name)
                if schema is not None:
                    try:
                        _validate_schema_value(arguments, schema, "arguments")
                    except ValueError as exc:
                        return self._error(request_id, -32602, f"Invalid params: {exc}")
                result = self.call_tool(name, arguments)
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method == "logging/setLevel":
                result = {}
            else:
                return self._error(request_id, -32601, f"Method not found: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:  # MCP 边界必须把实现异常转换为工具或协议错误。
            return self._error(request_id, -32603, str(exc))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """执行已声明的 MCP 工具，并将异常转换为客户端可读的工具错误。"""
        session_id = str(arguments.get("session_id") or "") or None
        project = audit_project_id(arguments.get("project_path"))
        session_label = self.audit.resolve_session_label(
            agent=self.agent, source="mcp", session_id=session_id, connection_id=self.connection_id, project_id=project,
        )
        invocation: dict[str, Any] | None = None
        try:
            invocation = self.audit.start(
                source="mcp", agent=self.agent, operation=name, action=summarize_tool_action(name, arguments),
                client=self.client, connection_id=self.connection_id, session_id=session_id, session_label=session_label, project_id=project,
            )
            self.audit.write_diagnostic(
                invocation_id=invocation["invocation_id"], source="mcp", agent=self.agent, operation=name, phase="input",
                session_id=session_id, session_label=session_label, payload={"arguments": arguments},
            )
        except Exception:
            pass
        try:
            self._validate_work_actor(name, arguments)
            if name == "search_knowledge":
                value = self.knowledge.search(
                    str(arguments.get("query") or ""), entry_type=arguments.get("type"),
                    status=str(arguments.get("status") or "verified"), tags=arguments.get("tags"),
                    limit=int(arguments.get("limit", 5)), excerpt_chars=int(arguments.get("excerpt_chars", 700)),
                )
            elif name == "review_knowledge":
                value = review_report(self.settings)
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
            elif name == "claim_work_state":
                value = self.work.claim(
                    str(arguments.get("work_id") or ""),
                    agent=str(arguments.get("agent") or ""),
                    session_id=str(arguments.get("session_id") or ""),
                    upgrade_legacy_session=bool(arguments.get("upgrade_legacy_session", False)),
                )
            elif name == "authorize_work_participant":
                mode = str(arguments.get("mode") or "shared")
                if mode not in {"shared", "handed-off", "revoke"}:
                    raise ValueError("mode 必须是 shared、handed-off 或 revoke")
                method = {
                    "shared": self.work.authorize_participant,
                    "handed-off": self.work.handoff,
                    "revoke": self.work.revoke_participant,
                }[mode]
                ownership_arguments = {
                    "owner_agent": str(arguments.get("owner_agent") or ""),
                    "owner_session_id": str(arguments.get("owner_session_id") or ""),
                    "participant_agent": str(arguments.get("participant_agent") or ""),
                    "participant_session_id": str(arguments.get("participant_session_id") or ""),
                }
                if mode != "revoke":
                    ownership_arguments["role"] = str(
                        arguments.get("role") or ("handoff" if mode == "handed-off" else "participant")
                    )
                value = method(str(arguments.get("work_id") or ""), **ownership_arguments)
            else:
                raise KeyError(f"未知工具：{name}")
            if invocation:
                outcome_code, result_summary = summarize_tool_result(name, value)
                try:
                    self.audit.finish(
                        invocation, source="mcp", agent=self.agent, operation=name, status="succeeded",
                        outcome_code=outcome_code, result_summary=result_summary, client=self.client,
                        connection_id=self.connection_id, session_id=session_id, session_label=session_label, project_id=project,
                    )
                    self.audit.write_diagnostic(
                        invocation_id=invocation["invocation_id"], source="mcp", agent=self.agent, operation=name, phase="output",
                        session_id=session_id, session_label=session_label, payload={"result": value},
                    )
                except Exception:
                    pass
            return {"content": [{"type": "text", "text": compact_json(value)}], "isError": False}
        except Exception as exc:
            if invocation:
                try:
                    self.audit.finish(
                        invocation, source="mcp", agent=self.agent, operation=name, status="failed",
                        outcome_code="tool_failed", error_type=type(exc).__name__, client=self.client,
                        connection_id=self.connection_id, session_id=session_id, session_label=session_label, project_id=project,
                    )
                    self.audit.write_diagnostic(
                        invocation_id=invocation["invocation_id"], source="mcp", agent=self.agent, operation=name, phase="error",
                        session_id=session_id, session_label=session_label,
                        payload={"error_type": type(exc).__name__, "error_message": str(exc)},
                    )
                except Exception:
                    pass
            return {"content": [{"type": "text", "text": compact_json({"error": str(exc)})}], "isError": True}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        """构造 JSON-RPC 错误响应，保持请求 ID 便于客户端关联。"""
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def run(self) -> None:
        """从 stdin 按行读取 JSON-RPC，向 stdout 输出响应并隔离坏请求。"""
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n", write_through=True)
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            response = self.process_line(raw)
            if response is not None:
                print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)


def run_server(settings: Settings | None = None, agent: str = "unknown") -> None:
    """加载默认设置并启动 MCP stdio 循环。"""
    MCPServer(settings or Settings.load(), agent=agent).run()
