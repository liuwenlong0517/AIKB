# AIKB Agent 索引

本文件是控制仓的稳定降级入口，不登记具体知识。根目录 `README.md` 是人类维护手册，不属于 Agent 默认接入、检索或恢复上下文。

## 知识入口

知识仓根目录由 Windows 用户环境变量 `AIKB_KNOWLEDGE_HOME` 指定；未显式设置时使用 `%AIKB_HOME%\content`。MCP 不可用时读取知识仓中的 `INDEX.md`，再沿分类 README、主题 README 和具体知识文件逐级加载最少内容。

完整知识目录位于知识仓 `CATALOG.md`，仅用于全库治理和正式写入前查重。

## 控制面入口

- `system/rules/CONTRIBUTING.md`：正式新增、修订、归档或淘汰知识时读取。
- `system/README.md`：维护 Schema、MCP、适配器、模板或测试时读取。
- `workspace/README.md`：维护本机工作状态与派生索引时读取。

本文件只在控制仓到知识仓的稳定路由发生变化时更新；具体知识分类由知识仓 `INDEX.md` 维护。
