# Local Code RAG 项目知识

本目录记录 `E:\CodeSpace\local-code-rag` 的长期项目事实、运行边界和已验证决策；不保存 Qdrant API Key、运行时索引数据或业务仓库源码。

## 项目索引

- [本地生成式模型退出 RAG 主链路](local-llm-exclusion.md)：本项目只保留本地 RAG 资源职责，代码生成和推理交给外部 Agent。
- [异步索引任务队列与重启状态边界](async-index-task-queue.md)：长耗时索引的任务查询、单写入者约束与容器重启后的状态处理。
- [索引一致性恢复、任务可观测性与自动项目注册](index-integrity-observability-registration.md)：三层索引检查、最小恢复、事件指标和只读多项目自动登记。
- [P1：安全上下文、容器监听与多项目检索基准](p1-safe-context-watcher-retrieval.md)：安全快照读取、任务进度/取消、按需监听、端口迁移和双项目评测结果。
- [本地检索缓存、上下文预算与 MCP 检索契约](retrieval-cache-context-mcp.md)：本地 SQLite 缓存、索引 revision 失效、预算化上下文组装、来源排名和 MCP 显式参数。
