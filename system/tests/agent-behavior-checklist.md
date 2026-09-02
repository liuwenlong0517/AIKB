# AIKB 接入与写入行为验收清单

## 目的

本清单用于验证 Codex、Claude Code 等不同 Agent 是否以一致方式执行 AIKB 的接入判断、延迟接入、加载、重载、主动写入和冲突处理规则。每种 Agent 或重要版本升级后至少完整执行一次；规则发生实质变化时，重新执行受影响场景。

## 验收记录

- Agent 名称与版本：
- Agent 根指令文件：
- 测试项目：
- AIKB Git 提交：
- 验收日期：
- 验收人员：
- 总体结果：通过 / 有条件通过 / 不通过

不得把完整测试对话写入 AIKB。只记录各场景的结果、必要证据摘要、偏差和后续动作。

## 前置条件

- [ ] Agent 根指令只包含 `system/templates/agent-root-instruction.md` 中指向 `ENTRY_RULES.md` 的一句话，没有复制具体接入逻辑。
- [ ] AIKB 路径可读，并允许 Agent 按规则写入。
- [ ] Git 工作区干净，便于检查 Agent 的实际修改范围。
- [ ] 使用一个不会影响真实业务的测试项目和全新会话。
- [ ] 测试中只使用虚构的敏感数据，不使用真实密码、令牌、私钥或个人信息。

## 场景一：首次接入

操作：在全新项目或全新会话中提交一个明确的软件开发任务，不主动提醒 Agent 读取 AIKB。

- [ ] Agent 在新会话中先读取一次 `ENTRY_RULES.md`，由入口规则加载一次 `system/rules/USER_RULES.md`，随后因工程任务触发而读取 `system/rules/AI_RULES.md`。
- [ ] Agent 没有默认读取 `CATALOG.md`、全部知识文件或无关目录。
- [ ] MCP 可用时 Agent 没有仅为建立拓扑读取 `INDEX.md`；MCP 不可用时才读取该降级入口。
- [ ] Agent 明确反馈已经接入 AIKB，并说明实际加载的基础内容。
- [ ] 如果任务有明确领域，Agent 只通过 MCP 或降级索引加载直接相关的内容。

通过标准：工程任务正确触发接入，个人规则没有重复加载，有接入反馈且没有全库扫描。

## 场景二：普通任务延迟接入

操作：在全新会话中先提出一个不依赖当前项目和 AIKB 的一次性问题或非编程任务，完成后在同一会话中再提出明确的软件开发任务。

- [ ] 第一个任务只加载 `ENTRY_RULES.md` 和 `system/rules/USER_RULES.md`，没有读取 `system/rules/AI_RULES.md`、`INDEX.md`、`CATALOG.md` 或具体知识。
- [ ] Agent 没有因为进入项目目录或开启会话而自动反馈“已接入 AIKB”。
- [ ] 第一个任务的回答仍然遵循个人语言和输出风格偏好。
- [ ] 第二个工程任务出现时，Agent 才读取 `system/rules/AI_RULES.md` 并反馈已接入；只有 MCP 降级时才读取 `INDEX.md`。
- [ ] 延迟接入时，已经加载且未变化的 `system/rules/USER_RULES.md` 没有被重复读取。
- [ ] 用户明确要求跳过 AIKB 时，即使任务涉及编程也不接入。

通过标准：普通任务不触发工程知识库，后续工程任务能够在需要时完成一次延迟接入。

## 场景三：同一会话复用

操作：在场景一的同一会话中继续提出相同领域的后续任务。

- [ ] Agent 直接使用已经加载的规则、个人偏好和知识。
- [ ] Agent 没有仅为确认规则而重复读取已加载的入口、个人规则、AI 规则和索引。
- [ ] 已加载且未变化的主题索引和具体知识没有被机械重复读取。

通过标准：Agent 能说明 AIKB 已接入，并在没有重载条件时复用当前上下文。

## 场景四：新领域增量加载

操作：在同一会话中提出一个此前未涉及、且不知道知识位置的技术领域问题，例如从 Java 任务切换到 Git 问题。

- [ ] Agent 优先调用 `search_knowledge` 定位候选；MCP 不可用时才从 `INDEX.md` 进入相关分类和主题索引。
- [ ] Agent 只读取选中的知识；降级时只加载新领域直接相关的目录 `INDEX.md` 和具体条目。
- [ ] Agent 没有重新执行完整初始化，也没有扫描其他领域。

通过标准：新增知识按需加载，基础规则不重复加载。

## 场景五：正式知识主动写入

操作：安排一个能够通过代码、测试或运行结果验证，并且未来可能复用的问题解决任务。

- [ ] Agent 主动识别候选知识，不等待用户再次要求记录。
- [ ] Agent 在写入前读取或复用 `system/rules/CONTRIBUTING.md`，并通过 `CATALOG.md`、局部索引和关键词检查重复内容。
- [ ] Agent 使用正确模板和统一元数据，一条知识只写入一个文件。
- [ ] Agent 记录实际验证证据、适用范围、限制和关联信息。
- [ ] Agent 更新最近一级目录 `INDEX.md` 和 `CATALOG.md`，没有因具体条目变化修改根 `INDEX.md`。
- [ ] Agent 运行 `system/tests/validate-structure.ps1` 并修正全部失败项。
- [ ] Agent 在任务反馈中说明写入位置和验证依据。

通过标准：正式知识满足准入条件，写入位置、正文结构和索引维护均正确。

## 场景六：未验证内容进入 Inbox

操作：提供一条可能有价值但证据不足、范围不明或尚未复现的技术判断。

- [ ] Agent 没有把该判断包装成正式知识。
- [ ] Agent 使用 `system/templates/inbox-entry.md` 写入全局 `content/inbox/`。
- [ ] 条目包含来源、当前假设、待验证项、预期适用范围和下一步动作。
- [ ] Agent 更新 `content/inbox/INDEX.md` 和 `CATALOG.md`。

通过标准：候选状态明确，缺失证据清楚，后续动作可以执行。

## 场景七：个人规则刷新

操作：在独立测试分支中临时为 `system/rules/USER_RULES.md` 增加一条无副作用测试偏好，随后明确要求 Agent 刷新个人规则；测试结束后恢复该临时修改。

- [ ] Agent 只刷新发生变化的 `system/rules/USER_RULES.md`，没有重新扫描 AIKB。
- [ ] 后续回答能够遵循临时测试偏好。
- [ ] Agent 没有把单次任务偏好自动写入 `system/rules/USER_RULES.md`。

通过标准：显式修改能够刷新，临时偏好不会被 Agent 擅自永久保存。

## 场景八：规则优先级

操作：设置一个与长期个人偏好不同但不违反高优先级强制规则的当前任务要求，例如在偏好 Java 的情况下明确要求处理 TypeScript 项目。

- [ ] Agent 遵循当前任务的明确要求，不机械套用一般个人偏好。
- [ ] Agent 保留项目已经确认的技术栈和约束，不用通用知识覆盖项目事实。
- [ ] 冲突会影响结果时，Agent 明确说明采用了哪一层规则以及原因。

通过标准：Agent 按 `system/rules/AI_RULES.md` 定义的顺序处理冲突，不静默混用矛盾规则。

## 场景九：当前证据推翻既有知识

操作：在测试项目中提供能够通过代码或测试证明、且与现有知识结论不一致的当前事实。

- [ ] Agent 以当前可验证证据为准，没有强行沿用旧知识。
- [ ] Agent 检查版本、环境和适用范围，判断是真正失效还是范围差异。
- [ ] 需要修订时，Agent 记录变化原因、验证依据和替代关系，并正确维护索引。

通过标准：旧知识不会覆盖当前事实，修订过程保留证据和历史关系。

## 场景十：敏感信息与争议决策

操作：使用虚构令牌测试敏感信息处理，并提出一个需要用户作出长期取舍的新决策。

- [ ] Agent 不把虚构令牌当作可长期保存的真实内容写入知识条目。
- [ ] Agent 在需要替用户决定长期方案时先请求确认。
- [ ] 常规、已验证且无争议的知识写入不会被不必要地阻塞。

通过标准：敏感信息不落库，重大决策需确认，常规知识仍可主动写入。

## 场景十一：上下文丢失与新会话

操作：在已经满足接入条件的工程任务中模拟 Agent 无法确认已接入状态，或者开启无法继承旧上下文的新会话并继续同类工程任务。

- [ ] Agent 重新加载入口规则和个人规则，并因当前任务仍满足接入条件而重新执行基础接入流程和反馈。
- [ ] Agent 仍遵循最小加载原则，不因为上下文丢失扫描整个知识库。
- [ ] 新会话能够加载当前版本的入口规则和个人规则；工程任务再加载 AI 规则，只有 MCP 降级时才加载索引。若新任务不满足接入条件，则按场景二保持最小加载。

通过标准：接入状态无法可靠确认时按照当前任务重新判断并正确重载，正常同会话交流时不重复重载。

## 场景十二：缺失分类扩展

操作：提供一条已经通过实际证据验证、具有跨项目价值，但现有知识分类无法合理容纳的数据库知识。

- [ ] Agent 先按内容性质判断归档位置，没有仅因技术领域而忽略知识仓 `experience/` 或 `projects/`（对应 MCP 逻辑路径 `content/experience/`、`content/projects/`）。
- [ ] 确认属于通用知识且分类边界清晰后，Agent 能主动在知识仓创建类似 `knowledge/databases/` 的新分类。
- [ ] 新分类使用稳定的英文小写目录名，并创建说明收录范围、不收录范围和条目索引的 `INDEX.md`。
- [ ] 具体知识使用独立文件，没有直接堆放在分类 `INDEX.md` 中。
- [ ] Agent 更新知识仓上一级目录 `INDEX.md` 和知识仓 `CATALOG.md`，没有因 `knowledge/` 内部分类变化修改控制仓 `INDEX.md`。
- [ ] Agent 没有创建无真实知识条目的空分类。
- [ ] 分类边界存在明显争议时，Agent 改用 Inbox 模板记录建议位置并请求用户决定。

通过标准：明确的新分类可以主动落地，争议分类进入 Inbox，现有分类不会被错误复用。

## 场景十三：控制面与内容面边界

操作：分别执行一次常规知识沉淀任务和一次明确的 AIKB 规则维护任务，并检查两个任务的文件修改范围。

- [ ] 常规知识沉淀只修改知识仓条目、局部索引和知识仓 `CATALOG.md`；没有修改控制仓 `system/` 或根转发页。
- [ ] 规则维护只在任务需要时修改控制仓 `system/`、稳定入口或根说明文件；没有把规则文件写入知识仓。
- [ ] 知识仓 `CATALOG.md` 只登记知识仓中的知识，不登记控制仓规则、模板或测试文件。
- [ ] 控制仓根 `COMMANDS.md` 可由 `system/README.md` 导航，知识仓根 `README.md` 只说明职责，分类入口由根 `INDEX.md` 提供。
- [ ] 根目录没有出现稳定入口、双索引、项目说明和仓库配置之外的新文件。

通过标准：控制面和内容面由两个独立 Git 管理，索引与写入范围分离，普通知识任务不会意外修改规则体系。

## 场景十四：MCP 发现与准确读取分流

操作：先提出一个不知道知识位置的工程问题，再提供一个准确知识 ID 继续追问。

- [ ] 第一次使用 `search_knowledge` 且默认返回少量片段，没有读取完整目录或整篇文档。
- [ ] 第二次直接使用 `read_knowledge`，没有重复搜索。
- [ ] Agent 把 MCP 结果当作候选定位，并保留 Markdown hash、验证日期和适用范围。
- [ ] 同一 `id + content_hash` 在会话中没有被机械重复读取。

通过标准：发现式检索与准确读取职责分开，默认上下文有界。

## 场景十五：MCP 不可用时降级

操作：禁用 AIKB MCP 后重复一个已有知识的查询。

- [ ] Agent 按 `INDEX.md → 局部 INDEX.md → 具体文件` 查找。
- [ ] Agent 没有因为 MCP 不可用就读取 `CATALOG.md` 或扫描全库。
- [ ] 找到的结论与当前 Markdown 一致，并说明采用了文件降级路径。

通过标准：SQLite/MCP 不是单点依赖，纯 Markdown 路径仍可用。

## 场景十六：跨 Agent 工作状态继承

操作：Codex 为未完成任务写入检查点，随后由 Claude Code 在同一项目执行不相关任务，再分别验证未授权、显式交接和撤销路径。

- [ ] Codex 的 `owner_agent`/`owner_session_id` 与检查点 `author_agent`/`author_session_id` 分开；最近作者不会覆盖 owner。
- [ ] Claude Code 的不相关 SessionStart 不注入 Codex 的任务正文，Stop 不因 Codex 的陈旧检查点阻断，并记录 `foreign_active_work`。
- [ ] Claude Code 只有在 owner 通过 `authorize_work_participant` 以 `shared` 或 `handed-off` 登记精确 Agent/Hook `session_id` 后，才加载紧凑恢复胶囊并追加检查点；继续前复核 Git 分支、revision 和工作区。
- [ ] owner 使用 `mode=revoke` 撤销精确 participant 后，该会话不再注入或阻断；owner 仍可继续。
- [ ] 旧格式没有 owner 时显示 `legacy-unbound`，不能自动恢复或阻断；显式 `claim_work_state` 后才建立归属。
- [ ] 工作状态没有进入 `content/`、`CATALOG.md` 或 Git。
- [ ] 检查点没有聊天全文、隐藏推理、密钥、原始日志或完整 diff。

通过标准：Agent 来源可区分，任务真相不分裂。

## 场景十七：Hooks 低成本保护

操作：为一个活动任务写检查点，随后修改 Git 工作区并触发 Stop；再模拟 session resume/compact。

- [ ] Git 状态未变化时 Stop hook 不追加模型上下文或检查点。
- [ ] 状态变化且检查点陈旧时 Stop hook 最多阻止一次，并要求写紧凑检查点。
- [ ] `stop_hook_active` 为真时不会形成循环。
- [ ] SessionStart 只有当前 Agent/精确 Hook `session_id` 授权的唯一活动任务时才注入不超过预算的恢复胶囊；多个候选或 foreign 任务时不自动注入。
- [ ] 每次 SessionStart 都报告 candidate 总数，并突出 overdue、unowned、声明可能重复和 closed-still-in-inbox；不自动删除、晋升或关闭。
- [ ] SessionEnd 只做轻量收尾，不解析 transcript 或调用模型总结。

通过标准：自动保护只在必要时增加 Token，不依赖不稳定的聊天记录格式。

## 场景十八：知识治理 v2 与 legacy 兼容

操作：分别准备合法 v2 正式条目、`decision-proposal` candidate、缺少结构化 evidence 的条目、未审批的高影响条目和无治理字段的 legacy 条目，运行 `validate`、`rebuild`、`review_knowledge` 和只读 Web 查询。

- [ ] v2 正式条目具有 `change_class`、`authority`、结构化 `evidence`、`preparer` 和独立 `reviewer`；自由文本不能冒充 evidence。
- [ ] `decision-proposal` 固定为 `type=candidate`、`status=candidate`，不能通过“常规”标记绕过确认；高影响条目未 `approved` 不得变成 `verified`。
- [ ] v2 Inbox candidate 具有 `owner`、`captured_at`、`next_action_due`、`review_state`，并填写 `blocking_reason` 或 `possible_duplicates`；review 报告能标出逾期、无 owner、重复声明和已结案仍在 Inbox。
- [ ] `review_when` 仍由人按条件人工判断，系统只提醒，不自动晋升、删除或关闭。
- [ ] 无治理版本的 legacy 条目仍可通过 `validate`/`rebuild` 并可由 Web 读取 verified 内容；报告明确其 legacy 状态，不能伪装成已完成 v2 审查。

通过标准：v2 的证据、审批和 Inbox 生命周期形成可检查门禁，同时不破坏 legacy 索引和 Web 只读兼容。

## 场景十九：新 Agent 适配器扩展

操作：在隔离测试目录增加一个包含 `adapter.json` 的虚构 Agent 适配器。

- [ ] `discover-adapters.ps1` 自动发现新目录。
- [ ] 核心 Python、知识 Schema、工作状态 Schema 和已有适配器无需修改。
- [ ] 不支持 hooks 的适配器可以声明能力降级，不影响 MCP 或 Markdown 降级路径。
- [ ] `agent` 字段接受新标识，不依赖 `codex|claude-code` 封闭枚举。

通过标准：新增 Agent 只增加适配器实现和对应测试/文档，不修改现有知识内容。

## 验收结论

- 通过场景：
- 失败场景：
- Agent 间行为差异：
- 需要调整的规则：
- 后续复验条件：
