---
status: verified
tags: [local-code-rag, rag, ollama, embedding, gpu, architecture-decision]
applicable_versions: local-code-rag 0.3.0 and later
last_verified: 2026-08-07
review_when: 引入新的本地模型、修改 RAG 服务职责，或需要重新评估 GPU 资源分配时
supersedes: []
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

2026-08-07 检查 `src/local_code_rag/config.py`、全部 `config/*.toml`、HTTP API 与 MCP 实现后确认：14B 相关字段只存在于配置，未被索引、检索或 MCP 调用。移除字段后应运行完整单元测试，并重建 `local-code-rag` 容器确认 `/readyz` 仍为 `ready`。

## 适用范围

仅适用于 Windows + Docker Desktop 上的 `E:\CodeSpace\local-code-rag`。`qwen3-embedding:0.6b` 仍是本地 RAG 的推荐 embedding 模型；“不使用本地语言大模型”不等于停止使用 embedding 模型。

## 关联信息

- `E:\CodeSpace\local-code-rag\docs\local-code-rag-manual.md`
- `E:\CodeSpace\local-code-rag\docs\semantic-retrieval-benchmark.md`
