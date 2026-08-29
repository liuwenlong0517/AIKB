# 第一阶段 API

所有接口使用 `/api/v1`，成功响应为 `{data, meta}`，失败响应为 `{error, meta}`。`meta.request_id` 同时通过 `X-Request-ID` 响应头返回。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 进程和共享核心健康状态 |
| GET | `/system/info` | 平台、Python、双仓 Git 和索引摘要 |
| GET | `/system/capabilities` | 当前平台和阶段能力 |
| GET | `/knowledge/overview` | verified 知识统计与最近条目 |
| GET | `/knowledge/tree` | verified 知识逻辑目录树 |
| GET | `/knowledge/tags` | verified 标签统计 |
| GET | `/knowledge/search?q=...` | 全文搜索，支持 `type`、`tags`、`limit` |
| GET | `/knowledge/document?id_or_path=...` | 按稳定 ID 或 `content/...` 路径读取正文 |

文档接口默认允许返回完整正文，并以 500000 字符作为本机响应安全上限；MCP 的 `read_knowledge` 仍保持 12000 字符上下文预算，两者不能混用。搜索结果、摘要和查询长度均由后端限制。客户端不能指定 `status`，服务始终固定为 `verified`。
