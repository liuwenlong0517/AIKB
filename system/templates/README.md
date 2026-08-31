# 模板

本目录提供 AIKB 接入和内容写入模板。新增内容时选择最接近的模板，不加载无关模板。

知识条目模板以治理 v2 为默认：正式新增或修订应声明 `change_class`、`authority`、
结构化 `evidence` 及必要的独立审查/审批字段；Inbox candidate 还应维护 owner、
captured_at、next_action_due 和 review_state。无治理版本的既有 legacy 条目仍可被
索引和 Web 只读读取，但不代表已经完成 v2 审查；迁移时需补齐字段并保留可复核依据。

正式知识模板遵循 `system/schemas/knowledge-entry.schema.json`，必须为条目分配长期稳定的 `id`、受控 `type` 和显式 `relations`。文件移动时不得修改稳定 ID。

## 模板索引

- [Agent 根指令接入模板](agent-root-instruction.md)：复制到具体 Agent 根指令文件中的单句入口指令。
- [候选知识模板](inbox-entry.md)：记录尚未完成验证或归类的候选内容。
- [通用知识模板](knowledge-entry.md)：新增通用知识条目时使用。
- [决策记录模板](decision-record.md)：记录工程决策时使用。
- [故障排查模板](troubleshooting.md)：记录故障排查和解决过程时使用。
- [工程陷阱模板](pitfall.md)：记录已验证的触发条件、风险和规避方式时使用。
- [工作流模板](workflow.md)：记录已验证、可重复执行的工程流程时使用。
- [项目记忆模板](project-memory.md)：记录项目级事实和解决方案时使用。
