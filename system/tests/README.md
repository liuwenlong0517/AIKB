# Agent 行为验收

本目录保存用于验证不同 AI Agent 是否一致执行 AIKB 规则的人工验收方案。验收文件只定义测试场景和通过标准，不保存具体 Agent 的原始对话记录。

## 验收索引

- [AIKB 接入与写入行为验收清单](agent-behavior-checklist.md)：验证按需与延迟接入、会话复用、增量加载、主动写入、冲突处理、分类扩展、目录边界和重载行为。
- [`validate-structure.ps1`](validate-structure.ps1)：自动检查根目录白名单、控制面与内容面结构、Markdown 本地链接、知识目录覆盖和单句入口模板。
- [`validate-adapters.ps1`](validate-adapters.ps1)：在临时用户目录和 Process 级 `AIKB_HOME` 中验证适配器安装、重复安装、无绝对仓库路径、MCP/hook 实际启动和精确卸载，不接触真实用户环境变量或 Agent 配置。
- [`validate-setup.ps1`](validate-setup.ps1)：在临时用户目录中运行一键配置两次，验证独立脚本编排、根指令内容保留、一次性备份、诊断和幂等性。
- `system/tools/aikb-mcp/tests/`：验证 Front Matter、SQLite 中文检索、关系读取、MCP 协议、工作状态、脱敏和上下文预算。

在仓库根目录执行：

```powershell
pwsh -NoProfile -File system/tests/validate-structure.ps1
pwsh -NoProfile -File system/tests/validate-adapters.ps1
pwsh -NoProfile -File system/tests/validate-setup.ps1
python -m unittest discover -s system/tools/aikb-mcp/tests -v
```
