---
id: aikb:projects:local-code-rag:retrieval-cache-context-mcp
type: project-memory
status: verified
tags: [local-code-rag, retrieval-cache, context-budget, mcp, sqlite, fts5, qdrant, windows]
applicable_versions: local-code-rag 0.3.0 and later
last_verified: 2026-08-07
review_when: 修改检索缓存键、索引 revision、上下文组装预算、search_code MCP 参数或 /search 请求契约时
supersedes: []
relations:
  - type: depends_on
    target: aikb:projects:local-code-rag:p1-safe-context-watcher-retrieval
---

# 本地检索缓存、上下文预算与 MCP 检索契约

## 背景

`E:\CodeSpace\local-code-rag` 面向 Windows 本机个人开发 Agent，HTTP API 运行在 `127.0.0.1:8870`，stdio MCP 运行在宿主机并转发到容器 API。默认检索已经能够同时使用 Qdrant 向量和 SQLite FTS5，但 Agent 还需要控制上下文规模，并避免重复查询反复调用 embedding。

## 问题

仅返回单个定位片段时，Agent 分析调用链需要多次工具调用；直接扩大返回范围又可能把相邻重复切片和无关关系边送入上下文。API 服务每次请求创建检索服务对象，单纯使用进程内缓存不能跨请求复用；索引变更后若缓存没有明确失效边界，还可能返回旧项目结果。

## 解决方案

### 上下文预算与去重

`POST /search` 和 MCP `search_code` 在 `expand_context=true` 时组装上下文包，参数为：

- `adjacent_chunks`：同文件相邻切片组数，范围 0～5，默认 2；
- `relation_limit`：每个命中最多返回关系边，范围 0～50，默认 12；
- `max_context_lines`：每个命中代码行预算，范围 1～480，默认 240；
- `max_context_chars`：每个命中字数预算，范围 256～100000，默认 24000。

组装规则是主命中优先、相邻片段按距离排序、与主命中行范围相交的片段去重，并按合法行边界和字符数双重裁剪。响应中的 `budget` 返回最大/实际行数、字符数和 `truncated`。`relations` 只返回当前命中文件的已索引关系元数据，并标记 `direction=outgoing`。主命中和相邻片段都来自 FTS5 安全快照，不直接读取宿主机业务文件。

命中本身继续返回 `sources`、`vector_rank`、`lexical_rank`；`score` 是最终 hybrid RRF 分数，不是百分比置信度。

### 本地 embedding 与检索缓存

缓存由 `LocalRetrievalCache` 写入状态库旁边的独立 SQLite 文件，例如：

```text
data/qwen3-4b-state.cache.db
```

缓存保存查询 embedding 和已经安全过滤的检索结果/诊断字段，不保存 Qdrant API Key，也不向外部服务发送额外数据。默认配置为：

```toml
[cache]
enabled = true
embedding_ttl_seconds = 86400
search_ttl_seconds = 300
max_entries = 5000
```

检索缓存键包含项目 `repo_id`、项目索引 `revision`、模型/指令、collection、查询、过滤器、检索模式、limit、RRF 参数和权重。`repositories.revision` 在每次向量/词法索引提交后递增，因此项目索引变化会使旧检索缓存自然失效。缓存关闭或删除只影响加速，不影响 Qdrant、FTS5 和业务源码。

### MCP 显式契约

`search_code` 现在显式对齐 HTTP `/search`，支持：

```text
repo_id, query, language?, path_prefix?, limit=8,
max_per_file=1, mode="hybrid", expand_context=false,
adjacent_chunks=2, relation_limit=12,
max_context_lines=240, max_context_chars=24000
```

默认调用只返回定位命中和来源排名；只有显式开启 `expand_context` 时才返回上下文包。MCP Adapter 默认请求保持兼容，扩展字段只在启用上下文组装时转发。

## 验证

2026-08-07，项目提交 `97d0a64` 完成代码和文档更新：

- 本地单元测试与静态检查：81/81 通过；
- Docker 镜像 `local-code-rag:0.3.0` 重建后容器 healthy，`/readyz` 为 `ready`；
- 真实 `/search` 请求验证返回 `sources`、`vector_rank`、`lexical_rank`、`contexts` 和预算字段；60 行/6000 字符预算下能够返回 `used_lines`、`used_chars` 和 `truncated`；
- 同一 vector 查询连续执行两次：第一次 `cache_hit=false`、`embedding_cache_hit=false`，第二次 `cache_hit=true`、`embedding_cache_hit=true`，第二次耗时约 6 ms；
- `ordertools` 检索评测 25/25，Top-10 命中率 100%，MRR 0.522；CRM 检索评测 30/30，Top-10 命中率 100%，MRR 0.693；
- `ordertools` 关系评测 11/11、目标路径 9/9；CRM 关系评测 12/12、目标路径 10/10；
- `ordertools` 和 `crm-order-app` 的 Qdrant、manifest、FTS5 完整性均为 `consistent`。

## 适用范围

适用于 Windows + Docker Desktop 上的 `local-code-rag 0.3.0` 及后续版本、本机回环 API、Qwen3 embedding 4B 配置和当前 SQLite/Qdrant 双层检索架构。缓存文件属于本地 `data/`，不应提交 Git、上传或写入 AIKB。上下文预算是每个命中的独立预算，不等同于 Agent 模型的完整 token 上限；Agent 仍需根据任务复杂度选择是否扩展。

当前不改变局域网访问边界：API 仍只绑定 `127.0.0.1:8870`，MCP 只接受 loopback URL；尚未引入 TLS、远程多用户鉴权或生成式 reranker。

## 关联信息

- `E:\CodeSpace\local-code-rag\src\local_code_rag\cache.py`
- `E:\CodeSpace\local-code-rag\src\local_code_rag\context.py`
- `E:\CodeSpace\local-code-rag\src\local_code_rag\search.py`
- `E:\CodeSpace\local-code-rag\src\local_code_rag\mcp_adapter.py`
- `E:\CodeSpace\local-code-rag\src\local_code_rag\mcp_server.py`
- `E:\CodeSpace\local-code-rag\docs\api-reference.md`
- [P1：安全上下文、容器监听与多项目检索基准](p1-safe-context-watcher-retrieval.md)
