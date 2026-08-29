"""知识查询网关。

网关刻意不把 SQLite 查询或 Markdown 扫描复制到 HTTP 路由中。目录、标签和
总览统一委托共享核心的查询契约；契约缺失时明确报告服务不可用，测试通过
注入 mock gateway 验证 HTTP 协议，而不是在 Web 层建立第二套事实源。
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class GatewayError(RuntimeError):
    """知识核心不可用或返回无法公开的数据时使用的内部错误。"""


class KnowledgeNotFound(KeyError):
    """请求的知识不存在，或存在但不是允许公开的 ``verified`` 条目。"""


class KnowledgeGateway(Protocol):
    """路由层依赖的最小查询契约，便于用 mock 隔离共享核心版本差异。"""

    def overview(self) -> dict[str, Any]: ...

    def list_documents(self, *, prefix: str | None = None, entry_type: str | None = None) -> list[dict[str, Any]]: ...

    def list_tags(self) -> list[dict[str, Any]]: ...

    def search(self, query: str, **kwargs: Any) -> dict[str, Any]: ...

    def read(self, identifier: str, **kwargs: Any) -> dict[str, Any]: ...


@dataclass
class CoreModules:
    """延迟加载的核心模块，避免测试或健康检查因可选安装顺序而无法启动。"""

    settings_cls: Any
    knowledge_service_cls: Any


def _load_core_modules() -> CoreModules:
    """加载共享核心；从 Web 子工程运行时补充 sibling 包的导入位置。"""
    try:
        config = importlib.import_module("aikb.config")
        knowledge = importlib.import_module("aikb.knowledge")
    except ModuleNotFoundError as first_error:
        # Web 后端不复制核心源码。开发者直接从 aikb-web/backend 启动时，
        # 仅把已知的 sibling 目录加入导入路径，错误响应仍不会泄漏物理路径。
        core_root = Path(__file__).resolve().parents[4] / "aikb-mcp"
        if str(core_root) not in sys.path:
            sys.path.insert(0, str(core_root))
        try:
            config = importlib.import_module("aikb.config")
            knowledge = importlib.import_module("aikb.knowledge")
        except ModuleNotFoundError as second_error:
            raise GatewayError("共享知识服务不可用") from first_error
        except Exception as second_error:
            raise GatewayError("共享知识服务初始化失败") from second_error
    except Exception as error:
        raise GatewayError("共享知识服务初始化失败") from error
    return CoreModules(config.Settings, knowledge.KnowledgeService)


def _verified_documents(documents: Any) -> list[dict[str, Any]]:
    """过滤核心结果，只允许 verified 条目进入 Web 公共协议。"""
    result: list[dict[str, Any]] = []
    if not isinstance(documents, list):
        return result
    for item in documents:
        if not isinstance(item, dict) or item.get("status") != "verified":
            continue
        # 只复制公开元数据，避免把核心对象、物理 Path 或内部错误带出边界。
        public = {key: value for key, value in item.items() if key not in {"body", "absolute_path", "filesystem_path"}}
        if "path" in public and (not isinstance(public["path"], str) or not public["path"].replace("\\", "/").startswith("content/")):
            # 物理路径不是公共协议的一部分；不尝试从它反推逻辑路径。
            public.pop("path", None)
        result.append(public)
    return result


class CoreKnowledgeGateway:
    """把共享 ``KnowledgeService`` 映射成 Web 只读查询契约。"""

    def __init__(self, service: Any, settings: Any, modules: CoreModules | None = None):
        """绑定共享服务和设置；服务可由测试注入，避免真实索引成为测试前置条件。"""
        self.service = service
        self.settings = settings
        self.modules = modules

    @classmethod
    def create_default(cls) -> "CoreKnowledgeGateway":
        """从当前 AIKB 环境创建网关，失败时抛出不含路径的 ``GatewayError``。"""
        modules = _load_core_modules()
        try:
            settings = modules.settings_cls.load()
            service = modules.knowledge_service_cls(settings)
        except Exception as error:
            raise GatewayError("共享知识服务初始化失败") from error
        return cls(service, settings, modules)

    def list_documents(self, *, prefix: str | None = None, entry_type: str | None = None) -> list[dict[str, Any]]:
        """列出 verified 文档；查询和过滤均由共享核心负责。"""
        method = getattr(self.service, "list_documents", None)
        if not callable(method):
            raise GatewayError("共享知识服务缺少目录查询能力")
        try:
            # 当前核心契约使用 path_prefix；status 固定由核心实现，Web 不接受覆盖。
            raw = method(path_prefix=prefix, entry_type=entry_type)
        except TypeError:
            # 保留新旧接口命名的窄适配，不在 Web 层复制查询逻辑。
            try:
                raw = method(prefix=prefix, entry_type=entry_type, status="verified")
            except TypeError as error:
                raise GatewayError("共享知识服务接口不兼容") from error
        documents = raw.get("documents", []) if isinstance(raw, dict) else raw
        filtered = _verified_documents(documents)
        if prefix:
            filtered = [item for item in filtered if str(item.get("path", "")).startswith(prefix)]
        if entry_type:
            filtered = [item for item in filtered if item.get("type") == entry_type]
        return sorted(filtered, key=lambda item: (str(item.get("path", "")), str(item.get("id", ""))))

    def overview(self) -> dict[str, Any]:
        """返回知识数量、类型分布和索引状态，不暴露数据库物理位置。"""
        method = getattr(self.service, "overview", None)
        if callable(method):
            try:
                data = method(status="verified")
            except TypeError:
                data = method()
            if isinstance(data, dict):
                data = dict(data)
                data["status"] = "verified"
                return data
        raise GatewayError("共享知识服务缺少总览能力")

    def list_tags(self) -> list[dict[str, Any]]:
        """列出 verified 文档使用的标签及计数。"""
        method = getattr(self.service, "list_tags", None)
        if not callable(method):
            raise GatewayError("共享知识服务缺少标签查询能力")
        try:
            raw = method()
        except TypeError:
            try:
                raw = method(status="verified")
            except TypeError as error:
                raise GatewayError("共享知识服务接口不兼容") from error
        if isinstance(raw, dict):
            raw = raw.get("tags", [])
        if not isinstance(raw, list):
            raise GatewayError("共享知识服务返回无效标签结果")
        normalized: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict) or item.get("status", "verified") != "verified":
                continue
            # 核心当前字段名是 tag，Web 协议兼容前端较直观的 name。
            if "name" not in item and "tag" in item:
                item = {**item, "name": item["tag"]}
            normalized.append(item)
        return normalized

    def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        """调用共享搜索并强制 status=verified，避免客户端扩大读取范围。"""
        try:
            result = self.service.search(query, status="verified", **{key: value for key, value in kwargs.items() if key != "status"})
        except (KeyError, ValueError):
            raise
        if not isinstance(result, dict):
            raise GatewayError("知识搜索返回无效结果")
        result = dict(result)
        result["results"] = _verified_documents(result.get("results", []))
        result["count"] = len(result["results"])
        result["status"] = "verified"
        return result

    def read(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        """读取单篇知识并拒绝非 verified 条目。"""
        method = getattr(self.service, "read_document", self.service.read)
        try:
            result = method(identifier, **kwargs)
        except KeyError as error:
            raise KnowledgeNotFound from error
        except ValueError:
            raise
        if not isinstance(result, dict) or result.get("status") != "verified":
            raise KnowledgeNotFound
        public = {key: value for key, value in result.items() if key not in {"absolute_path", "filesystem_path"}}
        if "path" in public and (not isinstance(public["path"], str) or not public["path"].replace("\\", "/").startswith("content/")):
            public.pop("path", None)
        return public
