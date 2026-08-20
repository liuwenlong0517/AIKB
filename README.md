# AIKB

AIKB（AI Knowledge Base）是一个独立于具体 AI Agent 和具体项目的个人工程知识库。它用于长期沉淀经过验证、具有明确适用范围的工程知识，供人类开发者以及 OpenAI Codex、Claude Code 等不同 AI Coding Agent 按需读取。

当前仓库已经包含完整的接入与写入规则、Agent 行为验收、经过验证的工程经验和项目知识。尚无具体条目的主题目录仅作为导航骨架，不代表已有知识结论。

## 目录结构

- `ENTRY_RULES.md`：所有 Agent 共用且路径长期稳定的统一入口。
- `INDEX.md`：面向 Agent 的轻量知识导航，只提供稳定的分层入口。
- `CATALOG.md`：面向用户的完整知识目录，只登记 `content/` 中的内容。
- `system/`：AIKB 的控制面，集中存放规则、Schema、MCP、Agent 适配器、模板和验收。
- `content/`：AIKB 的内容面，集中存放通用知识、工程经验、工作流和项目知识。
- `workspace/`：AIKB 的本机运行面，保存不进 Git 的任务检查点与可重建 SQLite 索引。

三个平面必须保持边界：`system/` 定义 AIKB 如何工作，`content/` 记录 AIKB 知道什么，`workspace/` 记录当前机器尚未完成的工作状态。正常知识沉淀不得写入 `system/` 或 `workspace/`；工作检查点也不能自动提升为正式知识。

## 维护方式

1. 新内容先按照 `system/rules/CONTRIBUTING.md` 判断是否值得收录，并确认来源、背景、验证结果和适用范围。
2. 未整理材料先进入 `content/experience/inbox/`，验证后再归档到合适目录。
3. 新增、移动、重命名或删除内容后，更新最近一级目录的 `README.md` 和根目录 `CATALOG.md`；只有全局入口发生变化时才更新 `INDEX.md`。
4. 保持条目简短、可检索、可独立理解；不要保存聊天记录、临时想法或未验证结论。
5. 项目专属事实放入 `content/projects/`，通用工程知识放入 `content/knowledge/` 或 `content/experience/`。
6. 根目录只保留稳定入口、双索引、项目说明、Git 配置和运行面入口，不直接新增规则或知识文件。

## 与 AI Agent 配合

外部 Agent 的根配置只需用一句话指向 `ENTRY_RULES.md`，不复制具体接入逻辑。入口规则仅在工程任务需要时完成接入；未知知识位置通过轻量 MCP 发现，MCP 不可用时仍按 `INDEX.md` 分层读取。`system/adapters/` 提供 Codex、Claude Code 的显式、幂等用户级安装器；安装器未被执行时不会修改任何 Agent 配置。未来 Agent 通过新增自描述适配器目录接入，不修改知识内容和核心协议。
