# AIKB Index

本文件只提供导航，不复制具体知识内容。Agent 应先遵守 `AI_RULES.md`，再根据任务选择最少且相关的文件。

## 根目录

- `README.md`：面向人类的用途、结构和维护说明。
- `AI_RULES.md`：Agent 初始化、知识加载与知识写入规则。每次接入 AIKB 时优先读取。
- `USER_RULES.md`：用户跨 Agent、跨项目共用的长期偏好和协作规则。每次首次接入时读取，仅按用户明确要求修改。
- `CONTRIBUTING.md`：知识准入和质量标准。准备新增、修订、归档或淘汰知识时读取。

## knowledge

- `knowledge/README.md`：通用知识的文件粒度、目录组织和写入规则。新增或调整通用知识结构时读取。
- `knowledge/engineering/`：架构、设计模式、编码风格和代码评审等通用工程知识。处理跨语言工程设计或评审任务时读取相关文件。
- `knowledge/languages/`：Java、TypeScript、Python 等语言知识。任务涉及对应语言时只读取对应文件。
- `knowledge/frameworks/`：Spring、React、Docker 等框架或平台知识。任务明确涉及对应技术时读取。
- `knowledge/tools/`：Git、Linux、IDE 等工具知识。执行工具相关操作或排查工具问题时读取。

## experience

- `experience/inbox/`：尚未验证或尚未归类的候选材料。整理知识时读取，解决常规任务时不默认读取。
- `experience/solutions/`：已经验证的问题解决方案。遇到相似问题时按标题和适用范围检索。
- `experience/pitfalls/`：已经验证的常见陷阱及规避方式。实施、调试或评审高风险改动时按需读取。
- `experience/decisions/`：带背景和验证信息的工程决策记录。需要理解历史取舍或做兼容决策时读取。

## workflows

- `workflows/development.md`：开发任务流程；开始实现或规划改动时读取。
- `workflows/debugging.md`：调试流程；定位故障或回归问题时读取。
- `workflows/code-review.md`：代码评审流程；审查变更时读取。
- `workflows/release.md`：发布流程；准备、执行或复盘发布时读取。

## templates

- `templates/agent-root-instruction.md`：可复制到 Codex、Claude Code 等具体 Agent 根指令文件中的 AIKB 最小接入规则。
- `templates/knowledge-entry.md`：新增通用知识条目时使用。
- `templates/decision-record.md`：记录工程决策时使用。
- `templates/troubleshooting.md`：记录故障排查与解决过程时使用。
- `templates/project-memory.md`：记录项目级事实时使用。

## projects

- `projects/README.md`：项目级知识的组织规则。只有任务涉及已登记项目时，才加载对应项目内容。
