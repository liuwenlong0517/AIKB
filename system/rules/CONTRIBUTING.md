# AIKB 贡献指南

本文件在正式新增、修订、归档或淘汰知识时按需读取。目标是保存未来仍能减少重复调查、降低错误概率或解释重要决策的信息，而不是保存所有任务结果。

## 1. 准入标准

候选内容至少应具有一种长期价值：经过验证的解决方案或工作流；调查成本较高的结论；反直觉的约束或陷阱；稳定的工程事实；包含背景和权衡的重要决策；跨项目知识；或项目专属但长期有效的事实。

进入正式知识区必须同时满足：

1. **真实且可验证**：来自当前代码、配置、测试、运行结果、权威文档或用户确认，不是模型推测，并记录验证依据。
2. **可独立理解**：删除原始聊天后，背景、问题和结论仍完整清楚。
3. **边界明确**：写明项目、模块、技术栈、版本、环境、前置条件、限制和复核条件。
4. **可行动且可维护**：能指导判断、实施或验证，并具有合理的长期价值。
5. **不重复**：已检索现有知识；能够修订旧条目时不新建重复条目。
6. **安全**：不包含密钥、令牌、私钥、个人隐私、完整原始日志或其他不必要的敏感信息。

未经验证的方案、临时想法、原始聊天或逐字稿、没有背景和范围的片段、容易从官方文档获得且没有实践增量的内容、失效且无历史价值的结论，以及无法执行的空泛建议，不得作为正式知识。

## 2. 候选与确认边界

有潜在价值但证据、范围或归类不足的内容进入知识仓 `experience/inbox/`（逻辑路径 `content/experience/inbox/`），使用控制仓 `system/templates/inbox-entry.md`，记录来源、当前假设、待验证项、预期范围和下一步动作。候选使用 `status: candidate`，不得伪造验证日期；验证完成后晋升，确认重复、错误或无价值后删除。

低风险且证据完整的 `factual-update` 或 `operational-solution` 符合条件时无需逐次确认，可按项目授权自动处理；涉及敏感信息、decision、supersession、分类或迁移、或需要替用户作出新的长期决策时，先使用 Inbox 保存非敏感候选并请求用户决定。分类边界不能由 Agent 以“常规”或“无争议”自行放宽，具体枚举和审批状态见第 7 节。

## 3. 归档位置

- 知识仓 `knowledge/`：跨项目的通用工程、语言、框架和工具知识。
- 知识仓 `experience/solutions/`：已验证的问题解决方案。
- 知识仓 `experience/pitfalls/`：容易重复触发的陷阱及规避方式。
- 知识仓 `experience/decisions/`：包含背景、备选方案和权衡的决策。
- 知识仓 `workflows/`：可重复执行的开发、调试、评审和发布流程。
- 知识仓 `projects/<project>/`：只对特定项目成立的长期事实和约定。
- 知识仓 `experience/inbox/`：尚未满足正式准入条件的候选。

所有候选和正式知识都位于 `AIKB_KNOWLEDGE_HOME`；对外仍使用 `content/...` 逻辑路径。不得写入控制仓根目录、`system/` 或 `workspace/`。除非任务正在维护 AIKB 控制面，否则不得修改控制仓。

现有目录无法准确容纳已满足准入标准的知识时，可以创建职责清晰、名称稳定且不会引起大范围迁移的新分类。目录名使用英文小写短横线形式并包含说明范围和条目索引的 `README.md`；新分类不得引入新 `type`，只能沿用 schema 允许的 7 种类型及其现有目录语义，例如 `knowledge/databases/` 仍使用 `type: knowledge`。不得预建没有真实条目的空分类，边界有争议时先进入 Inbox。

## 4. 条目与元数据

选择 `system/templates/` 中最接近的模板。正式条目一条知识一个文件，文件名使用简短、可检索的英文小写短横线；正文使用中文，并包含背景、问题、解决方案、验证、适用范围和关联信息。标题直接表达问题或结论，主题过大时拆分并通过关系连接。

Front Matter 必须符合 `system/schemas/knowledge-entry.schema.json`：

- `id` 是以 `aikb:` 开头的全库唯一稳定标识，移动、重命名和分类调整时不变。
- `type` 与实际目录和语义一致；`status` 只在已验证、已废弃和候选状态间按 schema 选择。
- `tags` 服务检索；正式条目必须记录适用版本、最近验证日期和复核条件。
- `relations` 使用 schema 允许的关系类型和稳定目标 ID；修改时检查 ID 唯一性及目标存在性。

模板提供结构，schema 定义机器约束，本指南定义准入语义；三者冲突时先修复控制面，不绕过校验写入知识。

## 5. 索引维护

知识仓 `INDEX.md` 只保存稳定分类入口；知识仓 `CATALOG.md` 登记全部知识文件；各级 `README.md` 负责局部导航。控制仓根 `INDEX.md` 和 `CATALOG.md` 只是稳定转发页，普通知识变化不得修改。

- 新增、移动、重命名或删除条目：更新最近一级 `README.md` 和 `CATALOG.md`。
- 新增或删除主题：同时更新上一级分类 `README.md`。
- 标题、状态、适用范围或目录摘要变化：同步相关索引；仅正文细节变化时不机械更新。
- 只有 `INDEX.md` 已列出的稳定入口变化时才更新 `INDEX.md`。

控制面和工作状态不得登记到 `CATALOG.md`。

## 6. 写入闭环

每次正式写入依次完成：

1. 用 MCP、局部 README、关键词和 `CATALOG.md` 查重；`search_knowledge` 默认只查 `verified`，因此至少分别以 `status: verified` 和 `status: candidate` 搜索（必要时再查 `deprecated`），不能只依赖默认结果。
2. 用当前权威证据验证结论，确定适用范围；不满足条件则转入 Inbox。
3. 选择归档位置和模板，填写稳定元数据、正文、来源及关系。
4. 更新必要的局部 README 和 `CATALOG.md`，仅在稳定入口变化时更新 `INDEX.md`。
5. 在控制仓运行 `pwsh -NoProfile -File system/tests/validate-structure.ps1 -KnowledgePath $env:AIKB_KNOWLEDGE_HOME`；失败时修正，不发布未通过校验的正式知识。
6. 在知识仓审查并提交本次知识变更；维护 AIKB 控制面时，控制仓与知识仓分别提交。
7. 向用户报告写入位置、验证依据、适用边界、提交和校验结果。

发现既有条目过期或被推翻时，更新并记录验证依据；具有历史决策价值的旧结论标记为已废弃并关联替代条目，否则删除。定期清理 Inbox、过期和重复知识。

## 7. 客观变更分类与审批

每次新增或修改先声明一个 `change_class`，只使用以下枚举：`factual-update`（当前事实）、`operational-solution`（可复现解决方案）、`decision-record`（记录已有决定）、`decision-proposal`（提出新取舍）、`supersession`（替代旧条目）、`taxonomy-change`（分类或迁移）和 `sensitive-change`（敏感边界）。分类由变更内容和影响决定，不能由 Agent 以“常规”“已验证”自行降低风险。

`decision-proposal` 必须保持 `status: candidate`，直到获得用户确认、项目既有决策记录或其他明确权威来源；新的技术栈、架构、接口、安全边界和长期流程选择均属于该类。`supersession` 指向仍在使用的正式条目、`taxonomy-change`、`sensitive-change` 和改变未来 Agent 行为的规则，必须先请求用户决定或通过独立审查。正式 `decision-record` 必须记录其 `authority`，不能仅凭 Agent 的判断写成 `verified`。

审批结果使用固定状态：`auto-eligible`、`user-approval-required`、`independent-review-required` 或 `candidate-only`。高风险审批须绑定本次候选正文的内容哈希、目标 ID 和范围；批准后正文或范围发生变化必须重新审批。提交者不得把自己同时当作高风险变更的唯一审查者。

## 8. 结构化验证证据

正式条目的“验证”章节必须同时提供结构化证据：`kind`、`locator`、`observed_at`，并按类型补充 `result`、`revision`、适用版本或审批引用。`kind` 只能是 `test`、`command`、`runtime-observation`、`source-code`、`configuration`、`commit`、`official-documentation`、`user-confirmation` 或 `existing-decision-record`。例如命令应记录可复现命令、工作范围和结果；测试应记录测试名和通过/失败；代码或配置应记录逻辑路径及 revision；权威文档应记录 URL、访问日期和适用版本。

验证依据不得只写“已依据官方文档验证”、模型知识或无法定位的自由文本，不得伪造日期、提交号、测试结果或用户确认。validator 可以检查字段、格式、日期、逻辑路径边界、提交可解析性和证据是否留痕，但不能声称机器已经证明命令执行过、URL 支持全部结论或自然语言结论必然正确；无法复核的内容必须进入 Inbox。

## 9. 查重、Inbox 生命周期与提交门

每次新增或修订固定检查 `verified`、`candidate` 和 `deprecated` 三类状态；尤其是 decision、pitfall、solution，以及可能恢复、改名、迁移或替代旧条目的变更，不得省略 deprecated。查重结果记录在本次变更清单中，不能只在回复里口头声称已检查。

候选必须记录 `owner`、`captured_at`、`next_action_due`、`review_state`、阻塞原因和可能重复项。review 至少覆盖逾期、无 owner、重复候选、已被替代或关联入口仍在引用的条目；清理节奏由定期 review 任务执行，不以 Agent 的“必要时”判断代替。证据或归类不足时进入 Inbox，不能为了绕过复核宣布“证据充分”。

每次正式提交前向用户或独立 reviewer 暴露文件清单、变更结论、`change_class`、证据摘要、查重结果、索引影响和内容哈希。低风险且结构完整的 factual/operational 变更可按项目授权自动提交；decision、supersession、taxonomy、sensitive 和规则边界变更必须经过用户确认或独立审查。validator 通过不等于获得审批，也不等于允许替用户作长期决定。
