---
id: aikb:projects:local-code-rag:local-llm-exclusion
type: project-memory
status: verified
tags: [local-code-rag, rag, ollama, embedding, gpu, architecture-decision]
applicable_versions: local-code-rag 0.3.0 and later
last_verified: 2026-08-07
review_when: 引入新的本地模型、修改 RAG 服务职责，或需要重新评估 GPU 资源分配时
supersedes: []
relations:
  - type: related_to
    target: aikb:projects:local-code-rag:p1-safe-context-watcher-retrieval
---

# 本地生成式模型退出 RAG 主链路

## 背景

`E:\CodeSpace\local-code-rag` 已使用 Ollama、Qdrant、SQLite FTS5 和 stdio MCP 为 Claude Code、Codex 等 Agent 提供本地代码索引与检索。早期配置中保留了 `qwen2.5-coder:14b`、`qwen3:14b` 的代码生成与推理字段，但实际索引、HTTP API 和 MCP 均未调用这些字段。

## 问题

RTX 4080 Super 的 16GB 显存需要优先保障持续索引、embedding 和检索响应。若将 14B 生成式模型加入 RAG 主链路，会与高频 embedding 和索引争用 GPU，增加延迟和运行复杂度，而 Claude Code/Codex 已承担推理和代码生成。

## 解决方案

从 local-code-rag 0.3.0 后续维护开始，项目只保留 embedding、索引、向量检索、FTS5 和可选非生成式重排序能力：

- 配置与 `Settings` 不再声明 `code_model`、`reasoning_model`；
- 本地生成式模型不加入 FastAPI、MCP 或索引调用链；
- Claude Code、Codex 等外部 Agent 负责推理、分析、代码生成和修改；
- GPU 资源优先用于 embedding、批量索引、查询向量生成和未来非生成式 reranker。

已安装的 14B 模型不因本决策自动删除；它们不属于项目运行依赖。删除模型是可恢复性较差的磁盘清理操作，需要用户另行明确确认。

## 验证

2026-08-07 检查 `src/local_code_rag/config.py`、全部 `config/*.toml`、HTTP API 与 MCP 实现后确认：14B 相关字段只存在于配置，未被索引、检索或 MCP 调用。移除字段后运行 37 个单元测试并重建 `local-code-rag` 容器，`/readyz` 保持 `ready`。

同日以 CRM 的 20 条中文业务问题比较 embedding 配置：`qwen3-embedding:4b` + FTS5 的 Top-10 命中为 20/20、MRR 为 0.677、平均首个匹配排名为 2.10，优于 0.6B 的 19/20、0.652 和 2.21。4B 全量首次索引耗时约 1535 秒，慢于 0.6B；运行时约占用 6.2GB GPU 显存，在不加载本地生成式模型的 RTX 4080 Super 16GB 环境中可稳定完成索引。

## 适用范围

仅适用于 Windows + Docker Desktop 上的 `E:\CodeSpace\local-code-rag`。`qwen3-embedding:4b` 是当前默认 embedding 模型，使用独立的 2560 维 Qdrant collection 与 SQLite 状态库；0.6B 与 nomic 索引可保留作回退，但不得混用不同向量维度。"不使用本地语言大模型"不等于停止使用 embedding 模型。

## 关联信息

- `E:\CodeSpace\local-code-rag\docs\local-code-rag-manual.md`
- `E:\CodeSpace\local-code-rag\docs\semantic-retrieval-benchmark.md`
