# AIKB 本机工作区

本目录是 AIKB 的运行面，用于保存当前机器上的任务工作状态和可重建索引，不属于正式知识内容。

- `active/`：尚未完成的任务及其结构化检查点。
- `archive/`：已经完成、放弃或被替代的任务。
- `audit/events/`：按本地日期轮换的 UTF-8 JSONL 审计事实源，记录 MCP tool 与 hook 的安全摘要和结果。
- `audit/fallback/`：Python handler 无法正常落盘或启动时的独立 JSON 兜底事件。
- `audit/reports/`：从审计事件按需生成、可重建的 Markdown 报告。
- `db/aikb-knowledge.db`：从 `AIKB_KNOWLEDGE_HOME` Markdown 派生的知识索引。
- `db/aikb-work.db`：从工作状态 Markdown 派生的任务索引。
- `runtime/`：本机锁、临时文件和适配器运行标记。

除本说明和 `.gitignore` 外，本目录内容均不得进入控制仓或知识仓。正式知识只能写入知识仓；工作检查点不能自动提升为正式知识，也不得保存完整聊天记录、隐藏推理、密钥、原始日志或完整 diff。`audit/` 是独立的操作审计面，不属于 Working State：它不得保存 prompt、transcript、完整 MCP 返回值、完整 hook payload、知识正文或完整 traceback，也不得自动进入正式知识。审计历史不自动清理。
