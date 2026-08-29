# 第一阶段架构

## 数据流

```text
React → HTTP JSON → FastAPI → CoreKnowledgeGateway → aikb-mcp/aikb → Markdown / SQLite
```

浏览器不访问文件系统。FastAPI 不编写知识业务 SQL，只通过共享 `KnowledgeService` 获取总览、目录、标签、搜索和正文。SQLite/FTS 位于 `workspace/db/`，可以依据 Markdown 重建。

## 工程边界

- `frontend/`：页面、API Client、类型和安全 Markdown 渲染；
- `backend/aikb_web/api/`：HTTP 参数和响应；
- `backend/aikb_web/core/`：共享核心适配；
- `backend/aikb_web/platform/`：平台扩展契约；
- `scripts/`：Windows 构建、启动和验证入口；
- `docs/`：架构、接口、安全和扩展说明。

第一阶段只读，不包含检查点、审计查询、规则修改或控制动作。
