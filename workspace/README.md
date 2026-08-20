# AIKB 本机工作区

本目录是 AIKB 的运行面，用于保存当前机器上的任务工作状态和可重建索引，不属于正式知识内容。

- `active/`：尚未完成的任务及其结构化检查点。
- `archive/`：已经完成、放弃或被替代的任务。
- `db/aikb-knowledge.db`：从 `content/` Markdown 派生的知识索引。
- `db/aikb-work.db`：从工作状态 Markdown 派生的任务索引。
- `runtime/`：本机锁、临时文件和适配器运行标记。

除本说明和 `.gitignore` 外，本目录内容均不得进入 Git。正式知识只能写入 `content/`；工作检查点不能自动提升为正式知识，也不得保存完整聊天记录、隐藏推理、密钥、原始日志或完整 diff。
