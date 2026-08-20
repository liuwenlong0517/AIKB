# AIKB Agent 索引

本文件是 MCP 不可用时的轻量降级拓扑，不登记具体知识。已知准确文件或稳定 ID 时直接读取；未知位置按下列入口逐级加载最少内容。根目录 `README.md` 是人类维护手册，不属于 Agent 默认接入、检索或恢复上下文。

## 内容入口

- `content/knowledge/README.md`：通用工程、语言、框架和工具知识。
- `content/experience/README.md`：候选知识、解决方案、陷阱和决策。
- `content/workflows/README.md`：开发、调试、评审和发布流程。
- `content/projects/README.md`：项目级知识；仅在任务涉及已登记项目时继续加载。

## 按需文件

- `system/rules/CONTRIBUTING.md`：正式新增、修订、归档或淘汰知识时读取。
- `CATALOG.md`：全量内容目录，仅用于全库治理和正式写入前查重。
- `system/README.md`：维护 Schema、MCP、适配器、模板或测试时读取。
- `workspace/README.md`：维护本机工作状态与派生索引时读取。

具体知识只登记在主题 README 和 `CATALOG.md`；本文件只在上述稳定入口变化时更新。
