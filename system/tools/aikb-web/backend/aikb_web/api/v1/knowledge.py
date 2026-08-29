"""第一阶段知识只读接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from aikb_web.core.gateway import KnowledgeNotFound

from .common import success, validate_logical_identifier, validate_logical_prefix


router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def _gateway(request: Request) -> Any:
    """取得应用级网关；应用初始化时保证该依赖存在。"""
    return request.app.state.knowledge_gateway


@router.get("/overview")
def overview(request: Request) -> dict[str, Any]:
    """返回 verified 知识总览。"""
    return success(_gateway(request).overview(), request)


@router.get("/tree")
def tree(
    request: Request,
    prefix: str | None = Query(default=None, max_length=500),
    entry_type: str | None = Query(default=None, alias="type", max_length=64),
) -> dict[str, Any]:
    """把 verified 文档逻辑路径组织为目录树，不触及物理文件系统。"""
    logical_prefix = validate_logical_prefix(prefix)
    documents = _gateway(request).list_documents(prefix=logical_prefix, entry_type=entry_type)
    root: dict[str, Any] = {"name": "content", "path": "content", "kind": "directory", "children": []}
    dirs: dict[str, dict[str, Any]] = {"content": root}
    for document in documents:
        path = str(document.get("path", ""))
        if not path.startswith("content/") or document.get("status") != "verified":
            continue
        parts = path.split("/")
        parent = root
        current = "content"
        for part in parts[1:-1]:
            current = f"{current}/{part}"
            node = dirs.get(current)
            if node is None:
                node = {"name": part, "path": current, "kind": "directory", "children": []}
                dirs[current] = node
                parent["children"].append(node)
            parent = node
        leaf = parts[-1]
        parent["children"].append(
            {"name": leaf, "path": path, "kind": "document", "id": document.get("id"), "title": document.get("title"), "type": document.get("type"), "status": "verified"}
        )
    return success({"root": root}, request)


@router.get("/tags")
def tags(request: Request) -> dict[str, Any]:
    """返回 verified 条目的标签及数量。"""
    return success({"tags": _gateway(request).list_tags(), "status": "verified"}, request)


@router.get("/search")
def search(
    request: Request,
    q: str | None = Query(default=None, min_length=1, max_length=200),
    query_param: str | None = Query(default=None, alias="query", min_length=1, max_length=200),
    entry_type: str | None = Query(default=None, alias="type", max_length=64),
    tags: str | None = Query(default=None, max_length=500),
    limit: int = Query(default=20, ge=1, le=20),
    excerpt_chars: int = Query(default=700, ge=120, le=1600),
) -> dict[str, Any]:
    """调用共享全文检索，固定只搜索 verified 条目。"""
    query = (q or query_param or "").strip()
    if not query:
        raise ValueError("搜索关键词不能为空")
    requested_tags = [item.strip() for item in tags.split(",") if item.strip()] if tags else None
    result = _gateway(request).search(query, entry_type=entry_type, tags=requested_tags, limit=limit, excerpt_chars=excerpt_chars)
    if isinstance(result, dict) and isinstance(result.get("results"), list):
        # 网关已经执行一次过滤；路由再做轻量防御，避免替换网关或旧核心
        # 意外把 candidate/deprecated 条目带入浏览器。
        result = {**result, "results": [item for item in result["results"] if isinstance(item, dict) and item.get("status") == "verified"]}
        result["count"] = len(result["results"])
    return success(result, request)


@router.get("/document")
def document(
    request: Request,
    id_or_path: str = Query(..., min_length=1, max_length=500),
    section: str | None = Query(default=None, max_length=200),
    max_chars: int = Query(default=500_000, ge=300, le=500_000),
) -> dict[str, Any]:
    """读取 verified Markdown；参数只能是稳定 ID 或 content/ 逻辑路径。"""
    identifier = validate_logical_identifier(id_or_path)
    try:
        result = _gateway(request).read(identifier, section=section, max_chars=max_chars)
    except KnowledgeNotFound:
        # 不区分不存在与非 verified，避免借接口探测候选或弃用条目。
        raise
    if not isinstance(result, dict) or result.get("status") != "verified":
        raise KnowledgeNotFound
    return success(result, request)
