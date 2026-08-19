# Agent 行为验收

本目录保存用于验证不同 AI Agent 是否一致执行 AIKB 规则的人工验收方案。验收文件只定义测试场景和通过标准，不保存具体 Agent 的原始对话记录。

## 验收索引

- [AIKB 接入与写入行为验收清单](agent-behavior-checklist.md)：验证按需与延迟接入、会话复用、增量加载、主动写入、冲突处理、分类扩展、目录边界和重载行为。
- [`validate-structure.ps1`](validate-structure.ps1)：自动检查根目录白名单、控制面与内容面结构、Markdown 本地链接、知识目录覆盖和单句入口模板。

在仓库根目录执行：

```powershell
pwsh -NoProfile -File system/tests/validate-structure.ps1
```
