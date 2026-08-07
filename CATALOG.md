# AIKB 完整内容目录

本文件面向用户，集中登记 AIKB 中的全部内容，便于浏览和查找。Agent 首次接入时不默认加载本文件；新增、移动、重命名或删除内容时必须同步维护对应条目。

## 基础规则与说明

- [README.md](README.md)：AIKB 的用途、目录结构和维护方式。
- [ENTRY_RULES.md](ENTRY_RULES.md)：所有 Agent 共用的统一入口，集中维护会话初始化、接入判断和延迟接入规则。
- [AI_RULES.md](AI_RULES.md)：所有 Agent 共用的接入、加载、重载和主动写入规则。
- [USER_RULES.md](USER_RULES.md)：用户跨 Agent、跨项目共用的长期偏好与协作规则。
- [CONTRIBUTING.md](CONTRIBUTING.md)：知识准入、验证、归档、维护与淘汰标准。
- [INDEX.md](INDEX.md)：面向 Agent 的轻量分层导航入口。
- [CATALOG.md](CATALOG.md)：面向用户的完整内容目录，即本文件。

## 通用知识

- [knowledge/README.md](knowledge/README.md)：通用知识的组织和写入规则。
- [knowledge/engineering/README.md](knowledge/engineering/README.md)：通用工程知识分类索引。
  - [架构](knowledge/engineering/architecture/README.md)：架构知识主题索引，暂无具体条目。
  - [编码风格](knowledge/engineering/coding-style/README.md)：编码风格知识主题索引，暂无具体条目。
  - [设计模式](knowledge/engineering/design-patterns/README.md)：设计模式知识主题索引，暂无具体条目。
  - [代码评审](knowledge/engineering/review-guide/README.md)：代码评审知识主题索引，暂无具体条目。
- [knowledge/languages/README.md](knowledge/languages/README.md)：编程语言知识分类索引。
  - [Java](knowledge/languages/java/README.md)：Java 知识主题索引，暂无具体条目。
  - [Python](knowledge/languages/python/README.md)：Python 知识主题索引，暂无具体条目。
  - [TypeScript](knowledge/languages/typescript/README.md)：TypeScript 知识主题索引，暂无具体条目。
- [knowledge/frameworks/README.md](knowledge/frameworks/README.md)：框架与平台知识分类索引。
  - [Docker](knowledge/frameworks/docker/README.md)：Docker 知识主题索引，暂无具体条目。
  - [React](knowledge/frameworks/react/README.md)：React 知识主题索引，暂无具体条目。
  - [Spring](knowledge/frameworks/spring/README.md)：Spring 知识主题索引，暂无具体条目。
- [knowledge/tools/README.md](knowledge/tools/README.md)：工程工具知识分类索引。
  - [Git](knowledge/tools/git/README.md)：Git 知识主题索引，暂无具体条目。
  - [IDE](knowledge/tools/ide/README.md)：IDE 知识主题索引，暂无具体条目。
  - [Linux](knowledge/tools/linux/README.md)：Linux 知识主题索引，暂无具体条目。

## 经验沉淀

- [experience/README.md](experience/README.md)：经验内容总入口。
- [候选知识](experience/inbox/README.md)：尚未验证或尚未归类的候选内容，暂无具体条目。
- [解决方案](experience/solutions/README.md)：已经验证的问题解决方案。
  - [统一 VS Code user-data-dir 修复重启后登录会话丢失](experience/solutions/vscode-mixed-user-data-dir-auth-loss.md)：Windows Installer 版混用默认与自定义用户数据目录时，统一启动入口并通过两次冷启动验证登录会话持久化。
  - [PowerShell profile 中实现 Linux 风格列表并区分预测建议与 Tab 补全](experience/solutions/powershell-profile-psreadline-completion.md)：修复 `ll` 的错误参数用法，并通过 PSReadLine 配置区分行内预测建议与命令补全菜单。
- [工程陷阱](experience/pitfalls/README.md)：已经验证的陷阱与规避方式，暂无具体条目。
- [工程决策](experience/decisions/README.md)：保留背景和取舍理由的工程决策，暂无具体条目。

## 工作流

- [workflows/README.md](workflows/README.md)：工作流总入口。
- [开发流程](workflows/development.md)：开发任务流程，待定义。
- [调试流程](workflows/debugging.md)：故障和回归问题调试流程，待定义。
- [代码评审流程](workflows/code-review.md)：代码评审流程，待定义。
- [发布流程](workflows/release.md)：发布准备、执行和复盘流程，待定义。

## 模板

- [templates/README.md](templates/README.md)：模板总入口。
- [Agent 根指令接入模板](templates/agent-root-instruction.md)：可复制到不同 Agent 根指令文件中的单句入口指令。
- [候选知识模板](templates/inbox-entry.md)：记录尚未完成验证或归类的候选内容。
- [通用知识模板](templates/knowledge-entry.md)：新增通用知识条目时使用。
- [决策记录模板](templates/decision-record.md)：记录工程决策时使用。
- [故障排查模板](templates/troubleshooting.md)：记录故障排查和解决过程时使用。
- [项目记忆模板](templates/project-memory.md)：记录项目级事实和解决方案时使用。

## 项目知识

- [projects/README.md](projects/README.md)：项目级知识的组织规则与项目索引。
- [Local Code RAG](projects/local-code-rag/README.md)：本地代码索引与检索基础设施的项目知识索引。
  - [本地生成式模型退出 RAG 主链路](projects/local-code-rag/local-llm-exclusion.md)：GPU 资源优先投入 embedding、索引和检索，生成与推理交给外部 Agent。
  - [异步索引任务队列与重启状态边界](projects/local-code-rag/async-index-task-queue.md)：索引任务的持久化状态、单工作线程约束、MCP 查询方式和自动恢复边界。
  - [索引一致性恢复、任务可观测性与自动项目注册](projects/local-code-rag/index-integrity-observability-registration.md)：三层索引检查、最小恢复、任务事件指标和只读项目自动登记。
  - [P1：安全上下文、容器监听与多项目检索基准](projects/local-code-rag/p1-safe-context-watcher-retrieval.md)：安全快照读取、任务进度/取消、按需监听、端口迁移和双项目评测结果。
  - [本地检索缓存、上下文预算与 MCP 检索契约](projects/local-code-rag/retrieval-cache-context-mcp.md)：本地 SQLite 缓存、索引 revision 失效、预算化上下文组装、来源排名和 MCP 显式参数。
- [ToolBox](projects/toolbox/README.md)：原生 JavaScript 开发者工具箱的项目知识索引。
  - [项目架构与功能全景](projects/toolbox/project-overview.md)：当前基线的技术形态、扩展机制、20 个工具、外部依赖、运行验证与已知边界。

## Agent 行为验收

- [tests/README.md](tests/README.md)：不同 Agent 执行 AIKB 规则的验收入口。
- [AIKB 接入与写入行为验收清单](tests/agent-behavior-checklist.md)：覆盖按需与延迟接入、会话复用、增量加载、主动写入、冲突处理、分类扩展和重载行为。

## 仓库配置

- [.gitignore](.gitignore)：Git 忽略规则。
- [.gitattributes](.gitattributes)：文本文件属性和换行规则。
