# AIKB Agent 索引

本文件是面向 Agent 的轻量拓扑和最终降级入口，不登记全部具体条目。需要发现未知位置的知识时优先使用 AIKB MCP；已知准确文件或稳定 ID 时直接读取；MCP 不可用时再沿本文件逐级加载最少内容。

## 基础文件

- `ENTRY_RULES.md`：所有 Agent 共用的稳定入口。新会话通过该文件加载个人规则并判断是否需要完整接入 AIKB。
- `README.md`：面向人类的用途、结构和维护说明。
- `system/rules/AI_RULES.md`：Agent 接入后的初始化、知识加载与主动写入规则。由入口规则在任务满足接入条件时加载。
- `system/rules/USER_RULES.md`：用户跨 Agent、跨项目共用的长期偏好和协作规则。每个新会话可以单独读取，仅按用户明确要求修改。
- `system/rules/CONTRIBUTING.md`：知识准入和质量标准。准备新增、修订、归档或淘汰知识时读取。
- `CATALOG.md`：面向用户的完整内容目录。常规任务不默认加载；查找全部内容或维护知识时按需读取。
- `system/README.md`：Schema、MCP、Agent 适配器、模板和测试的控制面导航。
- `workspace/README.md`：本机工作状态和可重建索引的运行面边界说明；普通知识检索不读取工作状态。

## 分层入口

- `content/README.md`：知识内容面的总入口和边界说明。
- `content/knowledge/README.md`：通用工程知识入口，继续导航到工程、语言、框架和工具分类。
- `content/experience/README.md`：候选知识、解决方案、陷阱和决策记录入口。
- `content/workflows/README.md`：开发、调试、代码评审和发布流程入口。
- `content/projects/README.md`：项目级知识入口；只有任务涉及已登记项目时，才继续加载对应项目索引。

## 导航原则

具体知识只登记在主题 README 和 `CATALOG.md` 中，不复制到本文件。检索优先级为“已加载内容 → 准确文件/稳定 ID → MCP 搜索 → 本文件与局部 README → CATALOG 全量治理”。只有本文件列出的基础文件或分层入口变化时才更新本文件；控制面和运行面不混入知识目录。
