# AIKB WebUI 阶段 4B 安装与修复前置契约

本文档冻结阶段 4B 的任务分发条件。阶段 4A 已完成 Windows 本地规则治理闭环，但其单文件规则事务不能直接等价为用户环境和多个 Agent 配置文件的安装事务。4B 必须先建立服务端固定目标、无副作用预览、跨文件补偿事务、崩溃恢复和真实 handler 验收，再允许 WebUI 修改用户级配置。

当前只完成前置条件和开发波次设计，不代表安装、修复或卸载能力已经开放。

## 1. 现有实现审计结论

现有 CLI 安装链路具备可复用的受管区块、冲突拒绝和单文件原子写入基础，但不能直接注册成 Web 动作：

- `setup-aikb.ps1` 同时设置环境、运行测试、写根指令、写 MCP/hooks、重建索引并执行诊断，副作用范围过大且不能一次完整预览；
- `install-all.ps1` 和 `install-root-instructions.ps1` 会依次修改多个用户文件，后续文件失败时不会补偿回滚已成功文件；
- `.aikb-backup` 只保存首次修改前快照，重复安装后可能不再代表本次事务前状态，不能作为事务回滚依据；
- `set-aikb-home.ps1` 依次写入两个用户环境变量，任一后续验证失败时没有成组恢复旧值；
- `doctor.ps1` 会执行 MCP/hook 并写审计 probe，不是纯读取诊断，不能用于预览；
- 现有脚本允许调用方传入 `CodexHome`、`ClaudeHome` 和 `ClaudeUserConfig`，Web API 不能继承这种任意路径能力；
- 现有卸载只清理 MCP/hooks，不清理根指令，也没有跨文件事务，因此首版不提供卸载。

4B 可以复用配置解析、受管标记、期望内容生成和真实 probe 的语义，但必须实现专用 Web 规划器与事务执行器，不能通过 Shell 包装现有组合脚本。

## 2. 首批能力边界

### 2.1 固定维护目标

首版只注册三个静态目标：

| 目标 ID | 页面名称 | 风险 | 可预览 | 可应用 | 固定副作用 |
|---|---|---:|---:|---:|---|
| `environment` | AIKB 用户环境 | 高 | 是 | 是 | 写入当前 Windows 用户的 `AIKB_HOME`、`AIKB_KNOWLEDGE_HOME` |
| `agent.codex` | Codex 安装修复 | 高 | 是 | 是 | 写入 Codex 根指令、AIKB MCP 受管区块和 AIKB hooks |
| `agent.claude-code` | Claude Code 安装修复 | 高 | 是 | 是 | 写入 Claude Code 根指令、AIKB MCP 受管对象和 AIKB hooks |

目标注册表位于后端代码，不能通过配置文件、查询参数或浏览器动态扩展。动作风险新增 `user_config_write`，副作用分别固定为 `write:user_environment:aikb`、`write:agent_config:codex` 和 `write:agent_config:claude-code`。

### 2.2 不进入首版的能力

- 卸载、删除备份、清理用户目录或恢复任意历史快照；
- 接受任意路径、环境变量名称、命令、脚本、配置正文或上传文件；
- 自动运行 `setup-aikb.ps1`、`install-all.ps1`、`uninstall-all.ps1` 或完整 `doctor.ps1`；
- 自动执行 Git 操作、索引重建、知识修改、规则修改或审计报告生成；
- 自动关闭或重启 Codex、Claude Code、终端或其他用户进程；
- 修改非 AIKB 受管的同名 MCP、hooks 或根指令内容；
- macOS 实现或兼容性声明。

## 3. Windows 固定目标解析

浏览器和公共 API 只接触目标 ID、状态、哈希和语义差异，不接触物理路径。Windows 平台适配器在服务端解析以下叶子目标：

| 目标 | 服务端固定叶子 |
|---|---|
| Codex 根指令 | 当前 Windows 用户配置根下的 `AGENTS.md` |
| Codex MCP | 同一配置根下的 `config.toml` |
| Codex hooks | 同一配置根下的 `hooks.json` |
| Claude Code 根指令 | 当前用户 `.claude` 配置根下的 `CLAUDE.md` |
| Claude Code MCP | 当前用户级 `.claude.json` |
| Claude Code hooks | 当前用户 `.claude` 配置根下的 `settings.json` |
| AIKB 环境 | 当前用户的两个固定 AIKB 环境变量 |

第一小版本的 Codex 配置根只接受服务进程启动时解析并验证的当前用户配置根；如果 `CODEX_HOME` 指向用户配置边界之外、包含重解析点或无法证明属于当前用户，目标显示为“不支持通过 Web 修复”，引导使用 CLI。Claude Code 目标不接受覆盖路径。

所有文件目标必须逐段拒绝符号链接、junction 和其他重解析点；父目录和叶子文件必须仍位于固定用户配置根。已有目标必须是普通文件，新建目标的父目录必须是普通目录。环境目标值固定为当前 Web 服务已经验证的控制仓和知识仓，不允许浏览器提供新路径。

## 4. 状态检查与无副作用预览

每个目标公开固定状态：

- `ready`：受管内容与当前版本一致；
- `missing`：目标或受管内容缺失，可以安装；
- `drifted`：AIKB 受管内容存在但与当前模板不一致，可以修复；
- `conflict`：存在同名但非 AIKB 管理的 MCP/区块，拒绝覆盖；
- `invalid`：JSON/TOML 损坏、目标类型异常、重解析点或边界校验失败；
- `unsupported`：平台或配置根不满足首版约束；
- `restart_required`：写入已成功，但运行中的 Agent 仍需人工重启。

预览必须完全无副作用，只读取配置和用户环境，不创建事务目录、备份、临时文件、任务、审计 probe 或 Agent 进程。预览响应只包含：

- 目标 ID、当前状态和安全说明；
- 本次将执行的固定步骤；
- 受影响逻辑叶子列表，不返回绝对路径；
- AIKB 受管片段的结构化差异和前后哈希；
- 是否新建文件、是否要求重启、冲突和阻塞原因；
- `base_fingerprint`、`change_id`、`preview_digest` 和五分钟单次确认令牌。

用户配置可能包含密钥和私有插件信息。预览不得返回整个 TOML/JSON/Markdown 文件，只返回 AIKB 自有受管区块、固定对象和固定 hook handler 的差异；非受管内容只参与整文件哈希和冲突判断，不进入响应、任务或审计。

预览请求只允许 `base_fingerprint`，不接受路径、正文、环境值、命令或自由参数。目标为 `ready` 时不生成无意义变更；目标为 `conflict`、`invalid` 或 `unsupported` 时只返回修复建议，不签发令牌。

## 5. REST 与页面契约

阶段 4B 新增独立维护资源：

- `GET /api/v1/maintenance/targets`：返回三个静态目标及平台能力；
- `GET /api/v1/maintenance/targets/{target_id}`：返回安全状态、受管叶子和 `base_fingerprint`；
- `POST /api/v1/maintenance/targets/{target_id}/preview`：生成服务端期望状态和安全差异；
- `POST /api/v1/maintenance/changes/{change_id}/apply`：只接收确认令牌；
- `GET /api/v1/maintenance/changes/{change_id}`：返回事务、任务和恢复状态，不返回备份或物理路径。

所有 POST 继续要求回环监听、严格同源 `Host`/`Origin`、`Content-Type: application/json`、`X-AIKB-Request: 1`、精确 schema、五分钟单次令牌和不可重放确认。未知目标、路径状 ID、额外字段和陈旧指纹一律在创建事务前拒绝。

页面新增“安装与修复”模块，按“环境 → Codex → Claude Code”展示状态。用户必须先查看受管差异，再逐个目标确认；不提供“一键全部修复”。成功后只提示需要人工重启对应 Agent，不尝试控制用户进程。

## 6. 跨文件补偿事务

4B 使用独立于规则事务的 `MaintenanceTransactionStore`。事实源位于 `workspace/runtime/web/maintenance-transactions/<change_id>/`，至少保存安全 `transaction.json`、每个目标的事务前字节备份、期望字节和环境旧值。该目录被 Git 忽略、拒绝重解析点，并应收紧为当前 Windows 用户可访问；正文材料永不进入 API、任务 JSONL 或审计 JSONL。

状态机固定为：

```text
prepared -> applying -> verifying -> succeeded
                  \-> rolling_back -> rolled_back
                                   \-> recovery_required
```

应用顺序：

1. 获取全局维护锁，确认没有其他规则或维护写事务；
2. 验证令牌、`base_fingerprint`、文件整体验证哈希和环境旧值，冲突发生在令牌消费前；
3. 成功写入审计开始事实；失败时拒绝修改；
4. 在首次写入前为所有叶子保存“存在/不存在、原始字节、哈希、文件属性”和环境变量“缺失/空值/具体值”状态，写盘并强制刷新事务日志；
5. 逐个使用同目录临时文件原子替换；每完成一个叶子立即刷新进度；两个环境变量作为同一逻辑组写入并回读；
6. 验证 JSON/TOML 可解析、受管内容唯一、非受管内容未变化、环境值正确，并运行目标专属固定 probe；
7. 成功写终态审计后转为 `succeeded`；任何失败按相反顺序恢复；
8. 回滚后再次验证原始哈希、缺失状态和环境旧值；无法证明安全恢复时进入 `recovery_required` 并阻止新的维护写入。

Windows 文件系统不提供跨多个配置文件和注册表值的单次原子提交，因此这里采用持久化日志与补偿回滚，而不是宣称全局原子。事务备份不能复用 `.aikb-backup`；后者继续只作为 CLI 的历史兼容快照。

首版不自动删除成功、回滚或人工恢复事务的备份材料；由既有 workspace 显式清理流程在未来增加可审阅策略。任何自动保留期实现都必须先保证活动和 `recovery_required` 事务永不被清理。

## 7. 崩溃恢复与第三方修改

服务启动时扫描非终态维护事务，并按每个叶子的哈希判断：

- 当前仍是事务前状态：该叶子无需恢复；
- 当前等于本事务期望状态：允许从事务前备份恢复；
- 当前既不是事务前也不是期望状态：视为第三方修改，绝不覆盖，事务进入 `recovery_required`；
- 原目标不存在且事务新建了目标：仅当当前哈希等于本事务期望值时才允许移回“缺失”状态；
- 原环境变量缺失时回滚必须恢复为缺失，不能写成空字符串；
- 任何备份缺失、损坏、ACL/占用失败或日志不一致都进入人工恢复，不猜测内容。

恢复完成后必须补写安全任务终态和审计关联。终态审计失败时不能删除事务材料或解除全局写锁状态。

## 8. 目标专属验证

### 8.1 AIKB 用户环境

- 两个用户环境变量均与当前服务验证根一致；
- 控制仓存在稳定入口，知识仓契约版本兼容；
- 广播环境变化失败要明确报告，但持久值验证成功后可标为“已写入、重启后生效”；
- 不修改系统级环境，不读取或返回其他环境变量。

### 8.2 Codex

- 根指令受管标记恰好一组，用户其他内容逐字节语义保持；
- TOML 中只有一个 AIKB 受管 MCP 区块，发现非受管同名服务时拒绝；
- hooks JSON 可解析，只替换引用固定 `aikb-hook.ps1` 的受管 handlers；
- 使用固定可信 `pwsh` 和生成后的真实 handler 验证中文 UTF-8、MCP initialize/tools/list 和 Stop fail-open；
- 不依赖正在运行的 Codex 进程，也不修改 Codex 其他 MCP、hooks 或设置。

### 8.3 Claude Code

- 根指令受管标记恰好一组；
- `.claude.json` 只写带 `AIKB_MANAGED=1` 的 `mcpServers.aikb`，非受管同名对象拒绝；
- `settings.json` 只替换 AIKB hook handlers，保留其他 hooks；
- 使用生成后的真实 handler 验证中文 UTF-8、MCP initialize/tools/list 和生命周期事件；
- 不修改 Claude Code 其他服务器、项目配置或权限规则。

固定 probe 的命令和参数由平台适配器生成，不能来自浏览器。probe 输出只保留步骤、通过/失败、退出码分类和限长脱敏摘要。

## 9. 审计、安全投影与权限

维护审计沿用兼容 schema v4，并增加固定字段：`maintenance_target_id`、`change_id`、`before_fingerprint`、`after_fingerprint`、`rollback_status`、`restart_required`。环境值、绝对路径、整文件哈希明细、配置正文、非受管 diff、备份、ACL、安全标识符和底层异常不得进入审计。

任务参数只保存 `change_id`；事件只显示“预检、备份、写入根指令、写入 MCP、写入 hooks、验证、回滚”等语义步骤。公开错误使用固定代码：

- `MAINTENANCE_TARGET_UNSUPPORTED`；
- `MAINTENANCE_CONFLICT`；
- `MAINTENANCE_STALE_PREVIEW`；
- `MAINTENANCE_LOCKED`；
- `MAINTENANCE_APPLY_FAILED`；
- `MAINTENANCE_ROLLED_BACK`；
- `MAINTENANCE_RECOVERY_REQUIRED`。

配置备份可能包含用户私密信息，必须采用比普通任务更严格的本机权限和审计禁入规则。应用前不能写审计开始事实时 fail-closed；审计终态暂时失败时事务保持非终态并阻止新的维护写入。

## 10. Windows 与 macOS 平台边界

公共核心定义 `MaintenancePlatformAdapter`，至少提供 `inspect`、`plan`、`apply_step`、`verify`、`rollback_step` 和 `recover`。Windows 实现负责用户配置根、重解析点、环境存储、原子替换、文件占用和环境广播。

macOS 当前只注册 `supported=false`，不得复用 Windows 路径或假定 Agent 配置位置相同。设备就位后另行实现文件权限、符号链接、大小写敏感路径、`launchctl`/shell 环境传播和真实 Agent handler 回归；在真实设备通过前页面持续显示“不支持”。

## 11. 威胁与失败矩阵

| 场景 | 预期处理 |
|---|---|
| 浏览器提交路径、命令、配置正文或额外字段 | 422，事务目录不存在 |
| 非受管同名 MCP/根指令冲突 | 409，不覆盖 |
| JSON/TOML 已损坏 | 标记 `invalid`，不尝试猜测修复 |
| 预览后任一文件或环境值变化 | 409，令牌不消费 |
| 事务备份中途失败 | 正式目标保持不变 |
| 第二或第三文件写入失败 | 按相反顺序恢复已写目标 |
| 环境第二个值写入失败 | 恢复两个旧值及缺失语义 |
| 文件被占用或权限不足 | 回滚；无法确认时人工恢复 |
| 写入后服务崩溃 | 启动扫描按哈希补偿恢复 |
| 第三方在崩溃窗口修改文件 | 不覆盖，`recovery_required` |
| probe 失败 | 回滚全部本次变更 |
| 审计开始失败 | 拒绝修改 |
| 审计终态失败 | 保留材料并阻止后续维护写入 |
| 两个服务进程同时应用 | 全局锁和原子 claim 只允许一个成功 |
| 恶意重解析点替换父目录或目标 | 立即拒绝，不跟随链接 |
| Agent 正在运行 | 允许写入后提示人工重启，不控制进程 |

## 12. 开发波次与规模控制

4B 明显大于 4A 的单文件事务，必须拆为六个波次，不能在一个批次内同时实现环境、两个 Agent 和真实用户配置终验。

| 波次 | 范围 | 进入下一波的门槛 |
|---|---|---|
| 0 | 静态目标、平台 SPI、安全模型、预览/事务 schema、审计字段 | 任意路径/正文不可进入契约，macOS 明确 unsupported |
| 1 | 纯读取 inspect/plan、维护 API、页面状态与受管片段差异 | 预览零副作用，敏感非受管配置不回显 |
| 2 | 多目标事务、备份、全局锁、补偿回滚和启动恢复 | 故障注入下原配置保持或可证明恢复 |
| 3 | Windows 用户环境目标 | 两个变量成组写入、缺失语义和广播/回滚通过 |
| 4 | Codex 与 Claude Code 安装修复，按 Agent 分任务实现 | 临时用户配置根的冲突、幂等、回滚和真实 handler 通过 |
| 5 | Windows 多进程、浏览器、真实用户配置等价往返与发布终审 | 用户逐目标授权，最终配置指纹一致，无越界副作用 |

波次 4 内 Codex 与 Claude Code 可以并行开发，但必须共享同一事务与安全核心。每波由主会话审查修改范围、失败恢复和安全投影，再运行统一门禁。

### 12.1 波次 0 实施状态（2026-08-31）

波次 0 已完成但尚未开放任何维护 API 或真实写入能力：

- 冻结 `environment`、`agent.codex`、`agent.claude-code` 三个目标及其动作、逻辑叶子、语义步骤和 `user_config_write` 风险；
- 建立无物理路径、命令和配置正文的平台 SPI、安全规划/结果模型，以及补偿事务状态机；
- Windows 与 macOS 均以 `reserved_not_implemented` 对外声明，后续波次实现并通过验收前不得报告支持；
- 审计 schema 增加目标、前后指纹和重启标志，写入与 Web 投影均执行固定枚举和组合约束；
- 新增契约测试覆盖跨目标组合、非法 ID、大小写指纹、未知字段、状态迁移和敏感材料禁入。

本波次只建立后续实现必须遵守的静态边界，没有新增路由、事务目录、备份、平台读写适配器，也没有修改用户环境或 Agent 配置。波次 1 仍须独立实现并验证纯读取 `inspect/plan`、维护 API 和页面预览零副作用。

### 12.2 波次 1 实施状态（2026-08-31）

波次 1 已完成纯读取检查、结构化规划、维护 API 和“安装与修复”页面：

- Windows 只读适配器按固定用户边界读取三个目标；用户环境只查询 `HKCU\Environment` 的两个固定 AIKB 值，Agent 配置只解析受管片段；
- `CODEX_HOME` 只接受当前用户目录内位置，Claude Code 使用当前用户固定配置位置；缺失路径仍检查既有父目录重解析点，孤立受管标记按无效配置处理；
- `GET /api/v1/maintenance/targets`、目标详情和 `POST .../preview` 只返回静态目标、安全状态、逻辑叶子、固定差异码和 SHA-256；
- 预览 POST 复用 JSON、请求标记、回环 Host 和同源 Origin 门禁，只接受 `base_fingerprint`，陈旧指纹在规划前拒绝；
- 平台能力明确区分完整维护、只读检查、预览和应用：Windows 只读检查/预览可用，但 `supported=false`、`apply_supported=false`；macOS 继续不支持；
- 页面新增固定侧栏入口和三个目标状态，只展示受管结构化摘要，没有应用、修复、卸载、路径、命令或配置正文输入；
- 应用初始化只构造惰性只读适配器，不扫描配置，不创建事务、任务、备份、临时文件或审计 probe。

统一门禁通过 MCP 66 项、后端 156 项、前端 47 项及类型检查、Lint、生产构建和结构校验。真实浏览器验收覆盖 Windows 状态读取、目标切换和结构化预览，控制台无错误。本波次没有修改任何用户环境或 Agent 配置；波次 2 才允许实现多目标事务、备份、全局锁、补偿回滚和启动恢复。

### 12.3 波次 2-4 开发完成状态（2026-09-01）

阶段 4B 的剩余生产代码已经接通，但尚未执行第 5 波发布验收：

- 多目标维护事务、私有材料、全局锁、执行审计门禁、逆序补偿和启动恢复已统一装配；
- `environment` 支持两个固定用户环境值的成组写入、验证、广播结果和缺失/空值精确恢复；
- Codex 与 Claude Code 支持固定根指令、MCP 和 hooks 叶子的合并、原子写入、验证和回滚；
- JSON 结构键与共享 `AdapterConfig.psm1` 对齐：精确优先、大小写不敏感兜底、受管字段多变体拒绝，并保留非受管对象的原键名及大小写变体；
- Agent 验证实现服务端固定 MCP initialize、tools/list、四个生命周期 hook 和中文 UTF-8 probe；恢复扫描不启动 probe；
- 维护 preview 保持零磁盘副作用，只在进程内暂存安全计划和五分钟单次令牌；apply 重新 inspect/plan 后才创建事务和材料；
- `POST /maintenance/changes/{change_id}/apply` 与 `GET /maintenance/changes/{change_id}` 已接入安全任务投影，令牌在全局锁内完成材料和 preflight 校验后才消费；
- 页面已实现逐目标高风险确认、任务跳转、事务轮询、回滚/人工恢复和人工重启提示，不提供一键全部、卸载或任意输入。

当前自动化门禁为 MCP 91 项通过（1 项跳过）、Web 后端 272 项通过（16 项按 Windows 权限条件跳过）、前端 51 项通过，类型检查、Lint、生产构建、适配器和结构校验均通过。该结果只证明隔离 fixture 与自动化开发门禁完成，不替代真实 HKCU、真实 handler、真实浏览器、多进程竞争和发布终审。

## 13. 发布门槛

阶段 4B 第一小版本只有同时满足以下条件才可声明完成：

- 三个固定目标均有零副作用预览、单次确认和安全事务事实源；
- 临时用户目录覆盖缺失、已有、漂移、冲突、损坏、占用、权限、重解析点和部分失败；
- 多进程竞争、崩溃恢复和第三方修改均不造成静默覆盖；
- 配置备份、正文、绝对路径和秘密不进入 API、任务或审计；
- Codex 与 Claude Code 生成后的真实 handler/MCP 在 Windows 上完成中文 UTF-8 回归；
- 浏览器覆盖逐目标预览、确认、任务跳转、恢复提示和重启提示；
- 真实用户配置终验只能在列出准确目标、外部备份和恢复方式并获得用户逐次授权后执行；
- 最终用户配置恢复到验收前指纹或保留用户明确选择的新状态，控制仓与知识仓保持 clean；
- 结构、核心、后端、前端、构建、安全和 Windows 显式验收全部通过。

完成上述门槛不代表卸载或 macOS 已实现。卸载必须在 4B 安装修复稳定后另建精确删除、恢复和用户授权契约。
