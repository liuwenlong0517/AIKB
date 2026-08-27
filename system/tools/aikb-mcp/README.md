# AIKB 轻量 MCP 服务

本工具只依赖 Python 3.11 标准库，在 Windows 本机提供：

- Markdown Front Matter 验证与 SQLite FTS5/trigram 知识索引；
- `search_knowledge`、`read_knowledge` 两个只读知识工具；
- `get_work_state`、`checkpoint_work_state`、`close_work_state` 三个本机任务状态工具；
- Codex、Claude Code 和未来 Agent 共用的 stdio MCP 协议入口。
- 按日 JSONL 的 MCP/hook 本机审计，以及按需 Markdown 汇总报告。

知识 Markdown 是长期事实源，`workspace/db/*.db` 均可删除重建；`workspace/audit/events/*.jsonl` 是独立的本机操作审计事实源，不是知识或 Working State。服务不会连接外部 RAG、向量数据库或网络服务，也不提供正式知识写入工具。

服务优先从环境变量 `AIKB_HOME` 定位仓库。首次使用或移动仓库后，从仓库根目录运行 `system/tools/set-aikb-home.ps1` 写入 Windows 用户环境变量，并重启 Agent。仓库内手工调用启动器时可以从脚本位置回退定位，但 Agent 的 MCP 和 hooks 依赖有效的用户级 `AIKB_HOME`。

在本目录执行：

```powershell
python -m aikb validate
python -m aikb rebuild
python -m aikb search "检索缓存"
python -m aikb serve --agent codex
python -m aikb audit list --since 24h
python -m aikb audit summary --since 7d
python -m aikb audit report --date 2026-08-27
```

审计默认只保存工具/事件白名单字段的脱敏摘要，不保存知识正文、完整结果、prompt、transcript 或 hook 原始 payload。`audit report` 默认向终端输出 Markdown；只有显式传入 `--output` 才写入报告文件。审计历史不会自动清理。

`system/schemas/audit-event.schema.json` 定义 JSONL schema v1。稳定字段为 `schema_version`、`record_type`、`event_id`、`invocation_id`、`timestamp`、`source`、`agent`、`client`、`connection_id`、`session_id`、`project_id`、`operation`、`action`、`status`、`outcome_code`、`result_summary`、`duration_ms` 和 `error_type`。一次调用以同一 `invocation_id` 的 `invocation_started` / `invocation_finished` 配对；缺少结束事件时报告为 `incomplete`。平台没有提供的真实 Session ID 保持 `null`。

也可以从仓库根目录执行 `system/tools/aikb-mcp/scripts/aikb.ps1`。Agent 安装由 `system/adapters/` 中的显式安装器完成。
