# AIKB

AIKB（AI Knowledge Base）是一个独立于具体 AI Agent 和具体项目的个人工程知识库。它用于长期沉淀经过验证、具有明确适用范围的工程知识，供人类开发者以及 OpenAI Codex、Claude Code 等不同 AI Coding Agent 按需读取。

当前仓库已经包含完整的接入与写入规则、Agent 行为验收、经过验证的工程经验和项目知识。尚无具体条目的主题目录仅作为导航骨架，不代表已有知识结论。

## 目录结构

- `ENTRY_RULES.md`：所有 Agent 共用且路径长期稳定的统一入口。
- `INDEX.md`：面向 Agent 的轻量知识导航，只提供稳定的分层入口。
- `CATALOG.md`：面向用户的完整知识目录，只登记 `content/` 中的内容。
- `system/`：AIKB 的控制面，集中存放规则、模板和行为验收。
- `content/`：AIKB 的内容面，集中存放通用知识、工程经验、工作流和项目知识。

`system/` 与 `content/` 必须保持边界：前者定义 AIKB 如何工作，后者记录 AIKB 知道什么。正常知识沉淀不得写入 `system/`；维护规则体系时也不得把规则文件登记为知识条目。

## 维护方式

1. 新内容先按照 `system/rules/CONTRIBUTING.md` 判断是否值得收录，并确认来源、背景、验证结果和适用范围。
2. 未整理材料先进入 `content/experience/inbox/`，验证后再归档到合适目录。
3. 新增、移动、重命名或删除内容后，更新最近一级目录的 `README.md` 和根目录 `CATALOG.md`；只有全局入口发生变化时才更新 `INDEX.md`。
4. 保持条目简短、可检索、可独立理解；不要保存聊天记录、临时想法或未验证结论。
5. 项目专属事实放入 `content/projects/`，通用工程知识放入 `content/knowledge/` 或 `content/experience/`。
6. 根目录只保留稳定入口、双索引、项目说明和 Git 配置文件，不直接新增规则或知识文件。

## 与 AI Agent 配合

外部 Agent 的根配置只需用一句话指向 `ENTRY_RULES.md`，不复制具体接入逻辑；AIKB 本身不创建或修改任何 Agent 专属配置文件。入口规则在新会话中加载 `system/rules/USER_RULES.md`，并仅在任务涉及实际软件工程工作、项目知识或用户明确要求时读取 `system/rules/AI_RULES.md` 和 `INDEX.md` 完成接入。普通一次性问答和非编程任务不接入，也不应默认扫描整个知识库。
