# AIKB 完整知识目录

本文件面向用户，集中登记 `content/` 中的全部知识内容，便于浏览、查找和写入前去重。Agent 首次接入时不默认加载本文件；新增、移动、重命名或删除知识内容时必须同步维护对应条目。规则、模板和测试统一由 `system/README.md` 导航，不在本文件登记。

## 内容入口

- [content/README.md](content/README.md)：知识内容面的范围、边界和分类入口。

## 通用知识

- [content/knowledge/README.md](content/knowledge/README.md)：通用知识的组织和写入规则。
- [content/knowledge/engineering/README.md](content/knowledge/engineering/README.md)：通用工程知识分类索引。
  - [架构](content/knowledge/engineering/architecture/README.md)：架构知识主题索引，暂无具体条目。
  - [编码风格](content/knowledge/engineering/coding-style/README.md)：编码风格知识主题索引，暂无具体条目。
  - [设计模式](content/knowledge/engineering/design-patterns/README.md)：设计模式知识主题索引，暂无具体条目。
  - [代码评审](content/knowledge/engineering/review-guide/README.md)：代码评审知识主题索引，暂无具体条目。
- [content/knowledge/languages/README.md](content/knowledge/languages/README.md)：编程语言知识分类索引。
  - [Java](content/knowledge/languages/java/README.md)：Java 知识主题索引，暂无具体条目。
  - [Python](content/knowledge/languages/python/README.md)：Python 知识主题索引，暂无具体条目。
  - [TypeScript](content/knowledge/languages/typescript/README.md)：TypeScript 知识主题索引，暂无具体条目。
- [content/knowledge/frameworks/README.md](content/knowledge/frameworks/README.md)：框架与平台知识分类索引。
  - [Docker](content/knowledge/frameworks/docker/README.md)：Docker 知识主题索引，暂无具体条目。
  - [React](content/knowledge/frameworks/react/README.md)：React 知识主题索引，暂无具体条目。
  - [Spring](content/knowledge/frameworks/spring/README.md)：Spring 知识主题索引，暂无具体条目。
- [content/knowledge/tools/README.md](content/knowledge/tools/README.md)：工程工具知识分类索引。
  - [Git](content/knowledge/tools/git/README.md)：Git 知识主题索引，暂无具体条目。
  - [IDE](content/knowledge/tools/ide/README.md)：IDE 知识主题索引，暂无具体条目。
  - [Linux](content/knowledge/tools/linux/README.md)：Linux 知识主题索引，暂无具体条目。

## 经验沉淀

- [content/experience/README.md](content/experience/README.md)：经验内容总入口。
- [候选知识](content/experience/inbox/README.md)：尚未验证或尚未归类的候选内容，暂无具体条目。
- [解决方案](content/experience/solutions/README.md)：已经验证的问题解决方案。
  - [统一 VS Code user-data-dir 修复重启后登录会话丢失](content/experience/solutions/vscode-mixed-user-data-dir-auth-loss.md)：Windows Installer 版混用默认与自定义用户数据目录时，统一启动入口并通过两次冷启动验证登录会话持久化。
  - [PowerShell profile 中实现 Linux 风格列表并区分预测建议与 Tab 补全](content/experience/solutions/powershell-profile-psreadline-completion.md)：修复 `ll` 的错误参数用法，并通过 PSReadLine 配置区分行内预测建议与命令补全菜单。
- [工程陷阱](content/experience/pitfalls/README.md)：已经验证的陷阱与规避方式，暂无具体条目。
- [工程决策](content/experience/decisions/README.md)：保留背景和取舍理由的工程决策，暂无具体条目。

## 工作流

- [content/workflows/README.md](content/workflows/README.md)：工作流总入口。
- [开发流程](content/workflows/development.md)：开发任务流程，待定义。
- [调试流程](content/workflows/debugging.md)：故障和回归问题调试流程，待定义。
- [代码评审流程](content/workflows/code-review.md)：代码评审流程，待定义。
- [发布流程](content/workflows/release.md)：发布准备、执行和复盘流程，待定义。

## 项目知识

- [content/projects/README.md](content/projects/README.md)：项目级知识的组织规则与项目索引。
- [Local Code RAG](content/projects/local-code-rag/README.md)：本地代码索引与检索基础设施的项目知识索引。
  - [本地生成式模型退出 RAG 主链路](content/projects/local-code-rag/local-llm-exclusion.md)：GPU 资源优先投入 embedding、索引和检索，生成与推理交给外部 Agent。
  - [异步索引任务队列与重启状态边界](content/projects/local-code-rag/async-index-task-queue.md)：索引任务的持久化状态、单工作线程约束、MCP 查询方式和自动恢复边界。
  - [索引一致性恢复、任务可观测性与自动项目注册](content/projects/local-code-rag/index-integrity-observability-registration.md)：三层索引检查、最小恢复、任务事件指标和只读项目自动登记。
  - [P1：安全上下文、容器监听与多项目检索基准](content/projects/local-code-rag/p1-safe-context-watcher-retrieval.md)：安全快照读取、任务进度/取消、按需监听、端口迁移和双项目评测结果。
  - [本地检索缓存、上下文预算与 MCP 检索契约](content/projects/local-code-rag/retrieval-cache-context-mcp.md)：本地 SQLite 缓存、索引 revision 失效、预算化上下文组装、来源排名和 MCP 显式参数。
- [ToolBox](content/projects/toolbox/README.md)：原生 JavaScript 开发者工具箱的项目知识索引。
  - [项目架构与功能全景](content/projects/toolbox/project-overview.md)：当前基线的技术形态、扩展机制、20 个工具、外部依赖、运行验证与已知边界。
