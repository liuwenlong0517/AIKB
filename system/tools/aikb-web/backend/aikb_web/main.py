"""FastAPI 应用入口和跨路由安全中间件。"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from aikb_web.api.v1.common import error_body, valid_request_id
from aikb_web.api.v1.knowledge import router as knowledge_router
from aikb_web.api.v1.runtime import router as runtime_router
from aikb_web.api.v1.audit import router as audit_router
from aikb_web.api.v1.actions import router as actions_router
from aikb_web.api.v1.tasks import router as tasks_router
from aikb_web.api.v1.system import router as system_router
from aikb_web.api.v1.rules import router as rules_router
from aikb_web.core.actions import ActionRegistry, ConfirmationTokenService
from aikb_web.core.gateway import GatewayError, KnowledgeNotFound, KnowledgeGateway, CoreKnowledgeGateway
from aikb_web.core.orchestrator import TaskOrchestrator
from aikb_web.core.windows_actions import WindowsActionsExecutor, WindowsActionsUnavailable
from aikb_web.core.rule_preview import RulePreviewService, RuleServiceError
from aikb_web.platform import platform_state


LOGGER = logging.getLogger("aikb_web")
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


class SPAStaticFiles(StaticFiles):
    """托管前端构建产物，并把非文件页面路由回退到 ``index.html``。"""

    async def get_response(self, path: str, scope: dict[str, Any]) -> Any:
        """优先返回真实静态文件；页面路由不存在时交给 React Router。"""
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and "." not in Path(path).name:
                return await super().get_response("index.html", scope)
            raise
        if response.status_code == 404 and "." not in Path(path).name:
            return await super().get_response("index.html", scope)
        return response


class UnavailableGateway:
    """共享核心不可初始化时的安全占位，确保健康接口仍能报告降级状态。"""

    settings = None

    def _raise(self) -> None:
        """抛出不含底层异常详情的统一内部错误。"""
        raise GatewayError("共享知识服务不可用")

    def overview(self) -> dict[str, Any]:
        self._raise()
        return {}

    def list_documents(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._raise()
        return []

    def list_tags(self) -> list[dict[str, Any]]:
        self._raise()
        return []

    def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self._raise()
        return {}

    def read(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        self._raise()
        return {}

    def web_active_work_states(self, **kwargs: Any) -> dict[str, Any]:
        """在核心未就绪时保持运行状态接口的统一 503 行为。"""
        self._raise()
        return {}

    def web_work_state(self, work_id: str) -> dict[str, Any]:
        """在核心未就绪时拒绝读取任务详情。"""
        self._raise()
        return {}

    def web_checkpoints(self, work_id: str, **kwargs: Any) -> dict[str, Any]:
        """在核心未就绪时拒绝读取检查点列表。"""
        self._raise()
        return {}

    def web_checkpoint(self, work_id: str, checkpoint_id: str) -> dict[str, Any]:
        """在核心未就绪时拒绝读取检查点详情。"""
        self._raise()
        return {}

    def web_repository_summary(self) -> dict[str, Any]:
        """在核心未就绪时拒绝读取双仓摘要。"""
        self._raise()
        return {}

    def web_audit_query(self, **kwargs: Any) -> dict[str, Any]:
        """在核心未就绪时拒绝查询审计事实源。"""
        self._raise()
        return {}

    def web_audit_summary(self, **kwargs: Any) -> dict[str, Any]:
        """在核心未就绪时拒绝生成审计汇总。"""
        self._raise()
        return {}

    def web_audit_detail(self, identifier: str) -> dict[str, Any] | None:
        """在核心未就绪时拒绝读取审计详情。"""
        self._raise()
        return None


def _json_error(request: Request, status_code: int, code: str, message: str, details: Any | None = None) -> JSONResponse:
    """生成统一错误 JSON；details 只接收已脱敏的字段级信息。"""
    return JSONResponse(status_code=status_code, content=error_body(code, message, request, details))


def create_app(gateway: KnowledgeGateway | None = None, orchestrator: TaskOrchestrator | None = None) -> FastAPI:
    """创建可注入网关的 FastAPI 应用，便于契约测试和未来核心替换。"""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """在服务真正启动时创建编排器，退出时广播取消并回收线程。"""
        if app.state.task_orchestrator is None and app.state.task_settings is not None:
            current_platform = platform_state()
            if current_platform.platform == "windows" and current_platform.supported:
                try:
                    adapter = WindowsActionsExecutor(app.state.task_settings)
                    adapter.validate([item["action_id"] for item in app.state.action_registry.list()])
                    app.state.platform_action_available = True
                    app.state.task_orchestrator = TaskOrchestrator(
                        app.state.task_settings,
                        registry=app.state.action_registry,
                        token_service=app.state.confirmation_tokens,
                        executor=adapter,
                        audit_sink=getattr(app.state.knowledge_gateway, "web_audit_write", None),
                    )
                except Exception:
                    app.state.platform_action_available = False
                    app.state.task_orchestrator = None
            else:
                app.state.platform_action_available = False
        try:
            yield
        finally:
            service = app.state.task_orchestrator
            if service is not None:
                service.shutdown()

    app = FastAPI(title="AIKB WebUI API", version="0.1.0", docs_url="/docs", redoc_url=None, lifespan=lifespan)
    dev_origins = ["http://127.0.0.1:5173", "http://localhost:5173"] if (
        os.environ.get("AIKB_WEB_DEV_ORIGIN") == "1" or os.environ.get("AIKB_WEB_DEV_MODE") == "1"
    ) else []
    app.add_middleware(
        CORSMiddleware,
        allow_origins=dev_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Accept", "Content-Type", "X-Request-ID", "X-AIKB-Request"],
    )
    init_error = False
    if gateway is None:
        try:
            gateway = CoreKnowledgeGateway.create_default()
        except GatewayError:
            gateway = UnavailableGateway()
            init_error = True
    app.state.knowledge_gateway = gateway
    app.state.gateway_init_error = init_error
    # 注册表和令牌服务独立于 workspace，保证只读动作目录/预览在知识服务降级时仍可诊断。
    app.state.action_registry = getattr(orchestrator, "registry", None) or ActionRegistry()
    app.state.confirmation_tokens = getattr(orchestrator, "tokens", None) or ConfirmationTokenService()
    app.state.task_settings = None if init_error else getattr(gateway, "settings", None)
    # 注入编排器代表调用方已提供并验证适配器（测试/后续平台实现）；默认服务仍须在
    # lifespan 中完成 Windows 程序解析后才将此标记置为 True。
    app.state.platform_action_available = orchestrator is not None
    # None 表示尚未启动或任务服务不可用；导入阶段不创建 TaskStore、不恢复任务。
    app.state.task_orchestrator = orchestrator
    # 规则预览服务只在已有可信设置时创建；它不注册任务执行器，也不写正式规则。
    try:
        app.state.rule_preview_service = RulePreviewService(getattr(gateway, "settings", None))
    except RuleServiceError:
        app.state.rule_preview_service = None

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Any]) -> JSONResponse:
        """建立请求标识并在响应头回传；非法客户端标识不会进入日志。"""
        supplied = valid_request_id(request.headers.get("X-Request-ID"))
        request.state.request_id = supplied or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """将 Pydantic 错误压缩为字段和消息，不回显输入值或内部路径。"""
        details = []
        for item in exc.errors():
            location = [str(part) for part in item.get("loc", []) if part != "query"]
            details.append({"field": ".".join(location), "message": str(item.get("msg", "invalid input"))})
        return _json_error(request, 422, "invalid_request", "请求参数无效", details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """把框架 HTTP 异常映射为固定错误码，避免透传任意 detail。"""
        if exc.status_code == 404:
            return _json_error(request, 404, "not_found", "资源不存在")
        if exc.status_code == 405:
            return _json_error(request, 405, "method_not_allowed", "请求方法不被支持")
        return _json_error(request, exc.status_code, "http_error", "请求无法处理")

    @app.exception_handler(ValueError)
    async def value_error(request: Request, exc: ValueError) -> JSONResponse:
        """把边界参数错误映射为客户端错误，不暴露核心校验文本。"""
        return _json_error(request, 400, "invalid_request", "请求参数无效")

    @app.exception_handler(KnowledgeNotFound)
    async def knowledge_not_found(request: Request, exc: KnowledgeNotFound) -> JSONResponse:
        """统一隐藏不存在和非 verified 知识的区别。"""
        return _json_error(request, 404, "not_found", "资源不存在")

    @app.exception_handler(KeyError)
    async def key_error(request: Request, exc: KeyError) -> JSONResponse:
        """把共享核心的未知工作项、检查点或审计调用统一映射为 404。"""
        return _json_error(request, 404, "not_found", "资源不存在")

    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        """将共享核心故障安全降级，不把 traceback 或物理路径发给浏览器。"""
        LOGGER.warning("knowledge gateway unavailable; request_id=%s", getattr(request.state, "request_id", "unknown"))
        return _json_error(request, 503, "service_unavailable", "知识服务暂不可用")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """最后一道异常边界；详细错误仅交给服务端日志，不进入 API。"""
        LOGGER.exception("unexpected web api error; request_id=%s", getattr(request.state, "request_id", "unknown"))
        return _json_error(request, 500, "internal_error", "服务内部错误")

    @app.get("/api/v1/health", tags=["system"])
    def health(request: Request) -> dict[str, Any]:
        """返回进程级健康状态；核心未就绪时以 degraded 表达而非泄漏初始化错误。"""
        return {
            "data": {
                "status": "degraded" if request.app.state.gateway_init_error else "ok",
                "service": "aikb-web",
                "knowledge_read_only": True,
                "controlled_actions": bool(getattr(request.app.state, "platform_action_available", False)),
                # 兼容旧客户端；新客户端应读取 knowledge_read_only。
                "read_only": True,
            },
            "meta": {"request_id": request.state.request_id, "api_version": "v1"},
        }

    app.include_router(system_router, prefix="/api/v1")
    app.include_router(knowledge_router, prefix="/api/v1")
    app.include_router(runtime_router, prefix="/api/v1")
    app.include_router(audit_router, prefix="/api/v1")
    app.include_router(actions_router, prefix="/api/v1")
    app.include_router(tasks_router, prefix="/api/v1")
    app.include_router(rules_router, prefix="/api/v1")

    @app.api_route("/api/{path:path}", methods=["GET"])
    def unknown_api(path: str) -> None:
        """阻止未知 API 落入单页应用回退，保持结构化 404 契约。"""
        raise HTTPException(status_code=404)

    if FRONTEND_DIST.is_dir():
        app.mount("/", SPAStaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    return app


app = create_app()
