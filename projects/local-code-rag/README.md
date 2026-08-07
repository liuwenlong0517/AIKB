# Local Code RAG 项目知识

本目录记录 `E:\CodeSpace\local-code-rag` 的长期项目事实、运行边界和已验证决策；不保存 Qdrant API Key、运行时索引数据或业务仓库源码。

## 项目索引

- [本地生成式模型退出 RAG 主链路](local-llm-exclusion.md)：本项目只保留本地 RAG 资源职责，代码生成和推理交给外部 Agent。
- [异步索引任务队列与重启状态边界](async-index-task-queue.md)：长耗时索引的任务查询、单写入者约束与容器重启后的状态处理。
