# AIKB 轻量 MCP 服务

本工具只依赖 Python 3.11 标准库，在 Windows 本机提供：

- Markdown Front Matter 验证与 SQLite FTS5/trigram 知识索引；
- `search_knowledge`、`review_knowledge`、`read_knowledge` 三个只读知识工具；
- `get_work_state`、`checkpoint_work_state`、`close_work_state`、`claim_work_state`、`authorize_work_participant` 五个本机任务状态工具（共 8 个 MCP 工具）；
- Codex、Claude Code 和未来 Agent 共用的 stdio MCP 协议入口。
- 按日 JSONL 的 MCP/hook 本机审计，以及按需 Excel 汇总报告。

知识 Markdown 是长期事实源，`workspace/db/*.db` 均可删除重建；`workspace/audit/events/*.jsonl` 是独立的本机操作审计事实源，不是知识或 Working State。服务不会连接外部 RAG、向量数据库或网络服务，也不提供正式知识写入工具。

服务优先从环境变量 `AIKB_HOME` 定位仓库。首次使用或移动仓库后，从仓库根目录运行 `system/tools/set-aikb-home.ps1` 写入 Windows 用户环境变量，并重启 Agent。仓库内手工调用启动器时可以从脚本位置回退定位，但 Agent 的 MCP 和 hooks 依赖有效的用户级 `AIKB_HOME`。

在本目录执行：

```powershell
python -m aikb validate
python -m aikb rebuild
python -m aikb search "检索缓存"
python -m aikb review
python -m aikb serve --agent codex
python -m aikb audit list --since 24h
python -m aikb audit summary --since 7d
# 默认写入 workspace/audit/reports/2026-08-27.xlsx
python -m aikb audit report --date 2026-08-27
# 指定自定义 .xlsx 文件路径
python -m aikb audit report --date 2026-08-27 --output C:\Reports\aikb-audit-2026-08-27.xlsx
# 暂时弃用：保留 Markdown 兼容报告
python -m aikb audit report-md --date 2026-08-27
# 读取诊断输入输出（需在服务启动前设置 AIKB_AUDIT_CAPTURE_LEVEL）
python -m aikb audit diagnostic <调用ID>
```

`serve --agent` 是 Working State 写入的服务端 Agent 边界：必须显式绑定当前进程
的 Agent，不能由请求参数自报另一身份。`session_id` 是 Hook 提供的会话关联标签，
命令示例和 MCP 调用必须原样传递和比较该值（保留大小写及合法标点，1 至 160 个
字符；NUL、换行及其他控制字符拒绝）；它不是密码学凭据，无法抵御同一用户下能
直接读写 `workspace/` 的恶意进程。平台 Hook 没有提供会话 ID 时，系统宁可不自动
恢复、不执行归属门禁，也不会退化成仅按 Agent 接管。

Working State v2 把 `owner_agent/owner_session_id` 与每次检查点的
`author_agent/author_session_id/role` 分开。跨 Agent 续写必须由 owner 显式授权
`shared` 或 `handed-off` participant；可用 `revoke` 撤销精确的 Agent/会话。缺少
owner 的 legacy 工作项标记为 `legacy-unbound`，需要先由当前会话显式
`claim_work_state`，不会自动注入或阻断。
新任务和显式认领使用 `ownership_binding=agent+exact-session`。早期
`agent+declared-session` 任务保持兼容，但不会自动迁移；只有显式传入
`upgrade_legacy_session=true`，且完整会话经旧 32 字符算法恰好对应 owner、没有
participant 时才可升级。有 participant 时必须先撤销并重新授权，迁移失败保持原状。

`review`/`review_knowledge` 是只读审查入口：返回 candidate 总数、逾期、无 owner、
声明可能重复和已结案仍留 Inbox 的摘要；v2 candidate 只投影有限生命周期字段，
legacy candidate 仍可见但不因缺少治理字段硬失败。它不会自动晋升、删除、关闭或
判断自然语言 `review_when` 是否满足。

审计默认级别为 `safe`：只保存工具/事件白名单字段的脱敏摘要、可读会话标签及中文动作/结果说明。把 `AIKB_AUDIT_CAPTURE_LEVEL` 设为 `diagnostic` 或 `full-local` 后，MCP/hook 的输入输出会以同一调用 ID 保存至独立 `workspace/audit/diagnostic/`；两个级别都会脱敏常见密钥、移除 NUL 并限制大小，`full-local` 仅提高预算，不记录隐藏推理或二进制附件。`audit report` 默认写入 `workspace/audit/reports/<YYYY-MM-DD>.xlsx`，提供概览、可筛选调用明细和损坏记录；`audit report-md` 暂时弃用，但会继续生成兼容的 Markdown 报告。MCP 与 hook 本身不会自动清理审计；WebUI 启动恢复完成后可按固定默认保留期低频维护，并沿用活动、不确定、链接及恢复材料保护规则。

`system/schemas/audit-event.schema.json` 定义兼容 v1/v2 的 JSONL schema。v2 新增 `session_label`、`action_text`、`result_text` 和 `capture_level`，但保留原始 `session_id` 作为技术关联字段。一次调用以同一 `invocation_id` 的 `invocation_started` / `invocation_finished` 配对；缺少结束事件时报告为 `incomplete`。平台没有提供的真实 Session ID 保持 `null`，AIKB 会显示明确的降级会话标签而不会伪造 ID。

也可以从仓库根目录执行 `system/tools/aikb-mcp/scripts/aikb.ps1`。Agent 安装由 `system/adapters/` 中的显式安装器完成。
