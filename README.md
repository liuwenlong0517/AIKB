# AIKB

AIKB（AI Knowledge Base）是一个独立于具体 AI Agent 和具体项目的个人工程知识库。它用于长期沉淀经过验证、具有明确适用范围的工程知识，供人类开发者以及 OpenAI Codex、Claude Code 等不同 AI Coding Agent 按需读取。

当前版本只初始化知识库框架，不包含任何未经验证的技术经验、项目事实或最佳实践。

## 目录结构

- `AI_RULES.md`：所有 Agent 共用的加载与写入规则。
- `USER_RULES.md`：用户在不同 Agent 和项目之间共用的长期偏好与协作规则。
- `INDEX.md`：面向 Agent 的知识导航索引。
- `CONTRIBUTING.md`：知识准入、验证、归档与淘汰标准。
- `knowledge/`：按工程主题、语言、框架和工具组织的可复用知识。
- `experience/`：待整理经验、已验证方案、踩坑记录和决策记录。
- `workflows/`：开发、调试、评审和发布流程。
- `templates/`：新增知识、决策、故障排查和项目记忆时使用的模板。
- `projects/`：项目级知识入口；项目内容应与通用知识分离。

## 维护方式

1. 新内容先按照 `CONTRIBUTING.md` 判断是否值得收录，并确认来源、背景、验证结果和适用范围。
2. 未整理材料先进入 `experience/inbox/`，验证后再归档到合适目录。
3. 新增、移动或删除知识文件后，同步更新 `INDEX.md`。
4. 保持条目简短、可检索、可独立理解；不要保存聊天记录、临时想法或未验证结论。
5. 项目专属事实放入 `projects/`，通用工程知识放入 `knowledge/` 或 `experience/`。

## 与 AI Agent 配合

外部 Agent 配置可以引用本知识库，但 AIKB 本身不创建或修改任何 Agent 专属配置文件。Agent 首次使用时应依次读取 `AI_RULES.md`、`USER_RULES.md` 和 `INDEX.md`，随后只加载当前任务需要的文件，不应默认扫描整个知识库。
