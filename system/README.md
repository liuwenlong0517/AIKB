# AIKB 系统控制面

本目录定义 AIKB 如何接入、检索、恢复、写入和验收，不存放具体工程知识。只有维护 AIKB 控制面时才应修改本目录；常规知识沉淀不得写入这里，也不得把本目录文件登记到根目录 `CATALOG.md`。

## 目录索引

- [rules/AI_RULES.md](rules/AI_RULES.md)：Agent 接入后的初始化、加载、重载和主动写入规则。
- [rules/USER_RULES.md](rules/USER_RULES.md)：用户跨 Agent、跨项目共用的个人偏好与协作规则。
- [rules/CONTRIBUTING.md](rules/CONTRIBUTING.md)：知识准入、验证、归档、维护和淘汰标准。
- [schemas/knowledge-entry.schema.json](schemas/knowledge-entry.schema.json)：正式知识元数据契约。
- [schemas/work-checkpoint.schema.json](schemas/work-checkpoint.schema.json)：本机任务检查点契约。
- [schemas/adapter.schema.json](schemas/adapter.schema.json)：可插拔 Agent 适配器清单契约。
- [tools/aikb-mcp/README.md](tools/aikb-mcp/README.md)：SQLite FTS、MCP 和工作状态服务。
- [tools/set-aikb-home.ps1](tools/set-aikb-home.ps1)：将当前仓库路径幂等登记到 Windows 用户环境变量 `AIKB_HOME`。
- [tools/setup-aikb.ps1](tools/setup-aikb.ps1)：一键编排环境设置、测试、根指令、适配器、索引和诊断；各独立脚本仍保留。
- [adapters/README.md](adapters/README.md)：Codex、Claude Code 及未来 Agent 的 Windows 适配入口。
- [templates/README.md](templates/README.md)：Agent 根指令及知识写入模板入口。
- [tests/README.md](tests/README.md)：不同 Agent 执行 AIKB 规则的行为验收入口。

根目录 `ENTRY_RULES.md` 是唯一对外稳定入口，不移动到本目录。系统文件发生路径调整时，应优先保持该入口兼容，并同步更新规则内引用和行为验收。
