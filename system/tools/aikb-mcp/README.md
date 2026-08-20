# AIKB 轻量 MCP 服务

本工具只依赖 Python 3.11 标准库，在 Windows 本机提供：

- Markdown Front Matter 验证与 SQLite FTS5/trigram 知识索引；
- `search_knowledge`、`read_knowledge` 两个只读知识工具；
- `get_work_state`、`checkpoint_work_state`、`close_work_state` 三个本机任务状态工具；
- Codex、Claude Code 和未来 Agent 共用的 stdio MCP 协议入口。

Markdown 是事实源，`workspace/db/*.db` 均可删除重建。服务不会连接外部 RAG、向量数据库或网络服务，也不提供正式知识写入工具。

在本目录执行：

```powershell
python -m aikb validate
python -m aikb rebuild
python -m aikb search "检索缓存"
python -m aikb serve
```

也可以从仓库根目录执行 `system/tools/aikb-mcp/scripts/aikb.ps1`。Agent 安装由 `system/adapters/` 中的显式安装器完成。
