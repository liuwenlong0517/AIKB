# AIKB

AIKB（AI Knowledge Base）是一套面向个人工程工作的长期知识与跨会话工作状态系统。控制面与知识内容由两个独立 Git 仓库管理，以知识仓 Markdown 作为长期知识事实源，同时由控制仓在本机提供 SQLite 全文检索、关系索引、MCP 工具和 Session 生命周期适配。

当前实现面向 Windows，正式支持 OpenAI Codex 和 Claude Code。系统的目标不是保存全部聊天内容，而是让不同 Agent 在需要时找到经过验证的工程知识，并让尚未完成的任务能够在后续 Session 中以紧凑、可核对的状态继续。

> 本文件是面向人类维护者的完整项目手册。它不属于 Agent 的默认接入、知识检索或会话恢复上下文。Agent 只有在用户明确要求阅读，或者任务本身是在维护 AIKB 控制面、安装流程或文档时才应读取本文件。Agent 的稳定入口是 `ENTRY_RULES.md`，不是本 README。

## 1. 项目解决什么问题

AIKB 将容易混淆的四类信息分开管理：

1. **长期工程知识**：经过验证、可复用、具有明确适用范围的知识，保存在独立知识仓，由知识仓 Git 管理。
2. **系统控制规则**：Agent 如何接入、检索、恢复任务和贡献知识，保存在控制仓 `system/`，由控制仓 Git 管理。
3. **当前工作状态**：未完成任务的目标、进度、验证结果、阻塞和下一步，保存在 `workspace/`，仅存在当前机器，不进入 Git。
4. **本机操作审计**：MCP tool 与 hook 的时间、Agent、动作安全摘要和结果，以按日 JSONL 保存在 `workspace/audit/`，不进入知识或 Git。

这种分离避免了两个常见问题：一是把临时对话或未经验证的结论当成长期知识；二是为了跨 Session 续接任务而把整段聊天重新塞入上下文。

AIKB 当前提供：

- Git + Markdown 的可审查长期知识库；
- 稳定 ID、类型、状态、标签和知识关系；
- SQLite FTS5 全文检索、元数据过滤和关系索引；
- 面向 Codex、Claude Code 的 stdio MCP 服务；
- 本机 Working State、检查点和任务归档；
- SessionStart、PreCompact、Stop、SessionEnd 生命周期挂接；
- MCP/hook 文本审计、故障兜底和按需 Markdown 报告；
- 可插拔 Agent 适配器，以及幂等安装、诊断和精确卸载；
- 结构、元数据、适配器和 MCP 行为自动测试；
- MCP 失效时沿 `INDEX.md` 和局部知识 README 逐级读取的降级路径。

## 2. 明确的设计边界

### 2.1 Markdown 是长期事实源

正式知识以 `AIKB_KNOWLEDGE_HOME` 中的 Markdown 为准；默认知识仓位置是 `%AIKB_HOME%\content`。`workspace/db/aikb-knowledge.db` 只是派生索引，可以删除并从 Markdown 重建。搜索结果只负责定位候选内容，Agent 最终使用的是当前 Markdown、元数据和内容哈希。

### 2.2 SQLite 不是第二份知识库

SQLite 保存全文索引、标签、关系、内容哈希和本机工作状态索引，不承担人工编辑和版本审查。数据库损坏、Schema 版本变化或内容指纹不一致时，工具会重建数据库。

### 2.3 Working State 不是聊天记忆

`workspace/` 只记录恢复任务所需的结构化状态，不保存完整聊天、隐藏推理、密钥、原始终端日志或完整 diff。工作状态中的“候选知识”也不能自动成为正式知识，必须经过贡献和验证流程后才能进入知识仓。

### 2.4 Agent 适配不侵入核心

MCP、知识模型和工作状态模型不依赖 Codex 或 Claude Code。平台差异集中在 `system/adapters/<agent>/`。未来增加 Agent 时，优先新增适配器目录，不修改现有知识内容和核心协议。

### 2.5 审计不是知识或 Working State

`workspace/audit/events/*.jsonl` 是本机操作审计事实源，Excel 日报是主可重建视图，Markdown 仅保留兼容入口。默认 `safe` 审计只保存白名单字段的脱敏摘要；用户显式开启 `diagnostic` 或 `full-local` 后，受预算和脱敏保护的 MCP/hook 输入输出会写入独立诊断目录。任何级别都不保存聊天全文、隐藏推理、transcript、二进制附件、未脱敏密钥或完整 traceback；写入失败必须 fail-open，历史不会自动清理。需要维护时由 `system/tools/clear-workspace.ps1` 明确预览并确认执行，绝不在 Agent 生命周期中自动触发。

### 2.6 根 README 不参与默认 Agent 上下文

本 README 可以随项目增长而保持详实，不承担低 token 接入职责。Agent 正常工作时采用以下最小路径：

```text
Agent 根指令
  -> 控制仓 ENTRY_RULES.md
      -> USER_RULES.md（每个新会话最小加载）
      -> AI_RULES.md（仅任务需要接入 AIKB 时）
          -> MCP 搜索或准确读取
          -> 控制仓 INDEX.md（仅 MCP 失败时）
              -> 知识仓 INDEX.md
                  -> 最少的局部 README/具体知识
```

只有用户明确要求、安装排障或维护 AIKB 自身时，Agent 才按需读取本 README。知识仓 `CATALOG.md` 同样不在常规初始化中加载，只用于人类浏览、全库治理或正式写入前查重。

## 3. 总体架构

```text
%AIKB_HOME%\                      控制仓 Git
├─ ENTRY_RULES.md                 Agent 唯一稳定入口
├─ INDEX.md / CATALOG.md          指向知识仓的稳定转发页
├─ README.md                      本文件，人类维护者手册
├─ system/                        规则、Schema、工具、适配器、模板、测试
└─ workspace/                     本机检查点、审计、归档和派生数据库，不进 Git

%AIKB_KNOWLEDGE_HOME%\            独立知识仓 Git；默认 %AIKB_HOME%\content
├─ .aikb-knowledge.json           知识仓类型与兼容契约
├─ INDEX.md / CATALOG.md          知识导航与完整目录
├─ knowledge/ / experience/
└─ workflows/ / projects/
```

典型调用链如下：

```text
Codex / Claude Code
  ├─ 根指令 -> 控制仓 ENTRY_RULES.md -> AI_RULES.md（双层 INDEX 仅按需降级）
  ├─ MCP stdio -> system/tools/aikb-mcp/
  │                 ├─ 知识仓 Markdown -> SQLite FTS/元数据/关系索引
  │                 ├─ workspace/*.md -> SQLite 工作状态索引
  │                 └─ workspace/audit/events/*.jsonl -> 本机操作审计
  └─ hooks -> system/adapters/shared/aikb-hook.ps1
                  -> SessionStart 恢复提示 / Stop 检查点提醒
```

## 4. 根目录文件

### `ENTRY_RULES.md`

所有 Agent 共用且应保持路径稳定的唯一入口。它只负责：

- 每个新会话加载个人规则；
- 判断当前任务是否需要接入 AIKB；
- 在需要时延迟加载 `AI_RULES.md`；
- 避免在同一会话中重复加载规则。

该文件必须保持紧凑，不承载完整架构说明。

### `INDEX.md`

控制仓根文件只保存到知识仓的稳定路由，不列出知识分类。MCP 不可用时，Agent 从该文件进入 `AIKB_KNOWLEDGE_HOME/INDEX.md`，再沿分类 README、主题 README 和具体知识文件逐级读取最少内容。

### `CATALOG.md`

控制仓根文件是兼容转发页；真正的完整知识目录位于 `AIKB_KNOWLEDGE_HOME/CATALOG.md`。新增、移动、重命名或删除知识时只维护知识仓目录，不修改控制仓转发页。

### `.gitignore`

排除默认装配位置 `/content/`、Python 缓存、虚拟环境和 `workspace/` 中的本机运行数据。控制仓不记录知识仓 gitlink；`content/` 是普通独立仓库而不是 submodule。`workspace/.gitignore` 进一步确保活动任务、归档、数据库和运行时文件不会进入任一 Git 仓库。

## 5. `system/`：系统控制面

`system/` 定义 AIKB 如何工作，不保存具体工程知识。除非任务是在维护 AIKB 本身，否则不应把普通知识写入这里。

### 5.1 `system/rules/`

#### `USER_RULES.md`

保存用户跨 Agent、跨项目共用的长期偏好和协作要求。每个新会话可最小加载一次。只有用户明确要求时才修改，当前任务的临时要求不能自动提升为长期个人规则。

#### `AI_RULES.md`

任务被 `ENTRY_RULES.md` 判定为需要接入 AIKB 后加载。主要定义：

- 指令和事实冲突的优先级；
- 首次接入和最小上下文策略；
- 已知 ID、未知位置、MCP 失败等情况下的检索路由；
- Working State 的创建、恢复、检查点和关闭边界；
- 候选知识识别和进入贡献流程的触发边界；
- 重载条件、目录职责和安全边界。

#### `CONTRIBUTING.md`

长期知识的准入标准。它规定哪些信息值得收录、正式条目需要什么证据、候选知识如何进入 Inbox、知识应该放在哪个目录、如何维护索引，以及知识失效后如何修订或淘汰。

### 5.2 `system/adapters/`：Agent 适配器

适配器负责把不同 Agent 的配置文件、MCP 注册格式和生命周期事件转换为 AIKB 核心能够理解的形式。

#### 目录发现与 SPI

`discover-adapters.ps1` 扫描直接子目录中的 `adapter.json`。一个有效适配器至少声明：

- 稳定的适配器 `id`；
- 人类可读名称；
- 支持的平台；
- MCP 和生命周期事件能力；
- 安装、卸载和诊断脚本入口。

新增 Agent 的推荐方式是创建：

```text
system/adapters/<new-agent>/
├─ adapter.json
├─ install.ps1
├─ uninstall.ps1
└─ doctor.ps1
```

如果新 Agent 支持 stdio MCP，它可以直接复用现有服务；如果 hooks 事件名称或输出格式不同，只在适配器内做转换。如果不支持 hooks，仍可只使用 MCP；如果也不支持 MCP，则保留 `INDEX.md` 文件检索降级路径。

#### 当前适配器

- `codex/`：注册 Codex 用户级 MCP 和 hooks。
- `claude-code/`：注册 Claude Code 用户级 MCP 和 hooks。

两个适配器当前都声明支持 `mcpStdio`、`sessionStart`、`preCompact`、`stop` 和 `sessionEnd`。

#### 公共脚本

- `install-all.ps1`：安装全部或指定适配器。
- `install-root-instructions.ps1`：从统一模板向 Codex/Claude Code 用户级根指令文件写入受管区块，保留原内容并创建一次备份。
- `uninstall-all.ps1`：只移除 AIKB 管理的配置。
- `doctor.ps1`：检查 Python、适配器清单以及目标配置中是否存在 AIKB MCP 和 hooks。
- `shared/AdapterConfig.psm1`：实现 JSON/TOML 配置合并、一次性备份、原子写入、冲突检测和精确卸载。
- `shared/aikb-hook.ps1`：把 Agent hook 的标准输入转交给 Python 核心；Python 或 AIKB 故障时 fail-open，不阻断普通 Agent 会话。

#### 安装器修改哪些用户文件

默认情况下：

| Agent | MCP 配置 | Hooks 配置 |
|---|---|---|
| Codex | `~/.codex/config.toml` | `~/.codex/hooks.json` |
| Claude Code | `~/.claude.json` | `~/.claude/settings.json` |

Codex MCP 使用带有 AIKB 起止标记的受管 TOML 区块；Claude Code MCP 使用 `AIKB_MANAGED=1` 标记受管对象。安装器保留无关配置，首次修改已有文件时创建同目录的 `.aikb-backup`。如果发现同名但不受 AIKB 管理的 `aikb` MCP，安装会停止而不是覆盖。

这些配置属于当前 Windows 用户，不属于仓库。生成的命令使用 PowerShell 7 的 `pwsh`，避免 Windows PowerShell 5.1 对 UTF-8 无 BOM 脚本的错误解码。换电脑后需要重新运行安装器，但不需要复制或修改适配器实现。

#### 生命周期事件的当前行为

- `SessionStart`：按当前项目查找活动任务；只有当前 Agent/精确会话 owner 或 participant 的唯一候选时返回紧凑恢复提示，不加载聊天记录。每次启动还报告 candidate 总数，并突出逾期、无 owner、声明可能重复和已结案仍留 Inbox 的条目；不自动晋升、删除或关闭。
- `PreCompact`：提供压缩前生命周期接入点；当前保持轻量，不自行解析或总结 transcript。
- `Stop`：如果 Git 工作区相对当前 owner/participant 的最近检查点发生变化，提醒 Agent 先保存一次检查点；外来会话不注入任务也不阻断当前会话，同一状态只阻止一次，避免循环。
- `SessionEnd`：使用短超时完成轻量结束处理，不承担长时间总结任务。

### 5.3 `system/schemas/`：数据契约

Schema 使用 JSON Schema Draft 2020-12，既约束当前实现，也为未来适配器和工具提供稳定契约。

#### `knowledge-entry.schema.json`

定义知识条目的 Front Matter：

- `id`：以 `aikb:` 开头的全库唯一稳定 ID；文件移动或重命名时保持不变。
- `type`：`knowledge`、`solution`、`pitfall`、`decision`、`workflow`、`project-memory` 或 `candidate`。
- `status`：`verified`、`deprecated` 或 `candidate`。
- `tags`：用于发现和过滤的唯一标签列表。
- `applicable_versions`、`last_verified`、`review_when`：正式知识的适用和复核边界。
- `supersedes`：兼容历史路径式替代关系。
- `relations`：稳定 ID 之间的显式关系。

允许的关系类型包括 `related_to`、`depends_on`、`implements`、`supersedes`、`verified_by`、`applies_to` 和 `part_of`。候选条目不要求伪造正式验证信息；非候选条目必须提供适用版本、最近验证时间和复核条件。

#### `work-checkpoint.schema.json`

定义本机检查点的核心元数据：任务 ID、项目 ID、状态、Agent、Session、角色、更新时间、检查点 ID、项目路径和工作区是否有未提交变化。

`owner_agent`/`owner_session_id` 是不可由普通检查点覆盖的任务归属；每次检查点另记录 `author_agent`/`author_session_id`/`role`。跨 Agent 只有 owner 显式登记 `shared` 或 `handed-off` participant 后才能续写；`revoke` 可撤销精确会话。`agent` 仍是开放字符串而不是封闭枚举。缺少 owner 的旧任务标记为 `legacy-unbound`，必须先显式 claim。会话 ID 由 Hook 提供，是技术关联标签而非密码学凭据。

任务状态分为：

- 进行中：`planned`、`active`、`blocked`；
- 已关闭：`completed`、`abandoned`、`superseded`。

#### `adapter.schema.json`

定义适配器清单的 ID、显示名称、Windows 平台、MCP/hook 能力以及安装入口。它的作用是保证新适配器可以被自动发现和验证，而不要求核心代码硬编码平台名称。

### 5.4 `system/tools/`：本机初始化与运行工具

`set-aikb-home.ps1` 是双仓位置初始化脚本。它从脚本位置定位控制仓，通过 `-KnowledgePath` 接受任意知识仓位置；未指定时使用控制仓下的 `content/`。脚本验证两个仓库契约后幂等写入当前用户的 `AIKB_HOME` 与 `AIKB_KNOWLEDGE_HOME`。`-Target Process` 只服务于自动测试和临时诊断。

`setup-aikb.ps1` 是首次使用的一键编排器。它不复制环境设置、根指令、适配器、索引或诊断逻辑，而是按顺序调用对应的独立脚本；因此每个步骤仍可单独运行、排障和重复执行。

`clear-workspace.ps1` 用于按最后写入时间维护过期审计文件、运行检查点和 `runtime/` 临时子项。它默认仅返回 JSON 预览，默认保留审计 90 天、检查点 180 天、runtime 30 天；只有带 `-Apply` 且通过 PowerShell 确认后才删除。它不会删除任何活动任务的 `work.md` 或当前检查点，并始终保留 `runtime/audit.lock`，也不会猜测性处理没有时间戳的审计会话标签注册表。

`aikb-mcp/` 是轻量 MCP 与索引核心。

该工具使用 Python 3.11 标准库实现，不依赖外部向量数据库、外部 RAG 服务或第三方 Python 包。入口既可以作为命令行工具使用，也可以作为 stdio MCP 服务器运行。

#### Python 模块职责

- `aikb/__main__.py`：命令行入口，提供 `serve`、`validate`、`rebuild`、`search`、`read`、`work-get`、`hook` 和 `audit` 子命令。
- `aikb/audit.py`：实现按日 JSONL、跨进程追加、fallback、事件聚合、过滤和 Markdown 报告。
- `aikb/config.py`：分别解析控制仓、知识仓和本机 `workspace/`，验证知识仓契约并创建运行目录。
- `aikb/frontmatter.py`：读取和渲染受控 YAML 风格 Front Matter；不依赖 PyYAML。
- `aikb/indexer.py`：扫描正式知识、验证元数据、构建 FTS/元数据/标签/关系表并原子替换数据库。
- `aikb/knowledge.py`：实现搜索、过滤、短词回退、按稳定 ID 或准确路径读取、章节裁剪和关系返回。
- `aikb/workstate.py`：创建检查点、脱敏、Git 状态签名、恢复胶囊、工作索引重建和任务归档。
- `aikb/server.py`：实现 JSON-RPC stdio MCP 协议、工具声明、参数边界和错误转换。
- `aikb/hooks.py`：把 Session 生命周期事件转换为工作状态恢复或检查点提醒。
- `scripts/aikb.ps1`：Windows 启动器，从 `AIKB_HOME` 定位控制面代码，从 `AIKB_KNOWLEDGE_HOME` 定位知识；知识变量缺失时回退到控制仓 `content/`。
- `tests/test_core.py`：核心行为单元测试。

#### 知识数据库

`workspace/db/aikb-knowledge.db` 由知识仓中带合法知识元数据的 Markdown 派生，主要包含：

- 文档元数据和内容哈希；
- 标签表；
- 出站关系表，可反查入站关系；
- 搜索片段；
- FTS5 虚拟表；
- Schema 版本和内容指纹。

服务优先探测 SQLite trigram tokenizer；不可用时使用 Unicode tokenizer。对过短查询或 tokenizer 不适合的内容，使用受参数约束的字段/正文回退查询。Agent 不需要判断当前使用哪种搜索算法。

#### 工作状态数据库

`workspace/db/aikb-work.db` 从 `workspace/active/**/work.md` 和 `workspace/archive/**/work.md` 派生，用于按项目、状态和更新时间查找任务。真正可阅读和恢复的工作状态仍是 Markdown。

每次检查点会记录 Git revision、分支、工作区 dirty 状态和签名。恢复任务时必须重新核对当前 Git 状态，因为检查点不是当前代码的替代证据。

#### MCP 工具

| 工具 | 类型 | 用途 |
|---|---|---|
| `search_knowledge` | 只读 | 按关键词、类型、状态和标签发现少量候选知识，返回受字符预算限制的片段。 |
| `read_knowledge` | 只读 | 按稳定 ID 或准确路径读取当前 Markdown，可限定章节、字符数和是否返回关系。 |
| `get_work_state` | 只读 | 按项目或 `work_id` 查找活动任务，返回最多 1500 字符的恢复胶囊。 |
| `checkpoint_work_state` | 本机写入 | 在 `workspace/` 追加结构化检查点，不修改正式知识。 |
| `close_work_state` | 本机移动/写入 | 将任务标记为完成、放弃或被替代，并移动到本机归档。 |
| `claim_work_state` | 本机写入 | 显式认领无 owner 的 legacy 活动任务，绑定当前 Agent/Hook 会话。 |
| `authorize_work_participant` | 本机写入 | owner 为精确 Agent/会话登记 `shared`、`handed-off` 或 `revoke`。 |

单个检查点最大 64 KiB；列表字段和文本字段还有更小的内部截断限制。写入前执行常见密钥和 Token 模式脱敏，并拒绝越过 `workspace/active` 或 `workspace/archive` 的路径。

#### MCP 协议边界

当前服务实现 stdio JSON-RPC，支持初始化、ping、工具列表和工具调用，并声明兼容 MCP `2024-11-05`、`2025-03-26` 和 `2025-06-18` 协议版本。写入类 Working State 工具要求服务以 `serve --agent <agent>` 启动，并校验请求中的 Agent；它不能替代操作系统权限。它不暴露资源或 Prompt，也不提供正式知识写入工具。

### 5.5 `system/templates/`

提供 Agent 根指令和不同知识类型的写作模板：

- `agent-root-instruction.md`：复制到 Agent 用户级根指令文件的一句稳定入口。
- `inbox-entry.md`：尚未完成验证的候选知识。
- `knowledge-entry.md`：通用知识。
- `decision-record.md`：包含背景、选项和取舍的工程决策。
- `troubleshooting.md`：故障现象、原因、解决和验证过程。
- `project-memory.md`：只对特定项目成立的长期事实。

模板帮助保持结构一致，但正式知识是否可收录仍由 `CONTRIBUTING.md` 和证据决定。

### 5.6 `system/tests/`

- `validate-structure.ps1`：检查控制仓与可外置知识仓白名单、Markdown 本地链接、知识元数据、稳定 ID、关系目标、适配器清单和规则文件预算。
- `validate-adapters.ps1`：在临时用户目录中安装两次并卸载，验证幂等性、配置合法性、无关配置保留和精确清理，不触碰真实用户配置。
- `validate-setup.ps1`：在临时用户目录中运行完整一键编排，再重复运行轻量编排，验证根指令迁移、原内容保留、备份、诊断和整体幂等性。
- `validate-performance.ps1`：预热派生索引后重复测量热搜索与 SessionStart hook，中位耗时默认必须分别不超过 500 ms；阈值可通过参数显式收紧。
- `agent-behavior-checklist.md`：供 Codex、Claude Code 或未来 Agent 执行的人工行为验收场景。
- `system/tools/aikb-mcp/tests/test_core.py`：验证 Front Matter、数据库损坏重建、中文检索、关系读取、MCP 协议、工作状态、脱敏、归档和上下文预算。

## 6. 独立知识仓：长期知识内容面

`AIKB_KNOWLEDGE_HOME` 是唯一允许保存长期知识的仓库；默认装配在 `%AIKB_HOME%\content`，也可放在其他目录或磁盘：

- `knowledge/`：跨项目通用的工程知识；
- `experience/inbox/`：未完成验证或归类的候选知识；
- `experience/solutions/`：经过验证的问题解决方案；
- `experience/pitfalls/`：容易重复触发的陷阱；
- `experience/decisions/`：需要保留背景和取舍的重要决策；
- `workflows/`：可重复执行的开发、调试、评审和发布流程；
- `projects/<project>/`：只对特定项目成立的长期事实。

主题目录中的 README 是局部导航，不集中堆放知识正文。正式知识遵循“一条知识一个文件”，必须包含证据、验证结果、适用范围和复核条件。MCP 对外保留 `content/...` 逻辑路径，因此知识仓物理移动不会改变稳定引用。

## 7. `workspace/`：本机运行面

`workspace/` 除 README 和 `.gitignore` 外全部被 Git 忽略：

- `active/`：尚未完成的任务和历史检查点；
- `archive/`：已完成、放弃或被替代的任务；
- `audit/events/`：按日 JSONL 审计事实源；
- `audit/fallback/`：主写入或 hook wrapper 启动失败时的独立 JSON；
- `audit/diagnostic/`：按调用 ID 关联、仅在显式级别开启时写入的本机诊断输入输出；
- `audit/reports/`：显式生成、可重建的 Excel 报告；Markdown 兼容报告暂时弃用；
- `db/aikb-knowledge.db`：知识派生索引；
- `db/aikb-work.db`：工作状态派生索引；
- `runtime/`：锁、临时文件和运行标记。

工作状态默认不跨机器共享。克隆仓库到新电脑可以重建知识数据库，但不会带回旧电脑上的未完成任务。

## 8. 初次使用流程

以下步骤都在 Windows PowerShell 7 中执行。先选择最终存放位置，再通过初始化脚本把位置登记为用户环境变量；后续说明不依赖固定盘符。

### 8.1 一键配置

控制仓和知识仓已经放到最终位置后，在控制仓根目录执行：

```powershell
& .\system\tools\setup-aikb.ps1
```

默认一键流程依次执行：

1. 检查 Git、PowerShell 7 和 Python；
2. 调用 `set-aikb-home.ps1` 写入用户级 `AIKB_HOME` 与 `AIKB_KNOWLEDGE_HOME`；
3. 调用结构测试、Python 核心测试和适配器测试；
4. 调用 `install-root-instructions.ps1` 配置 Codex/Claude Code 根指令；
5. 调用 `install-all.ps1` 安装 MCP 和 hooks；
6. 调用 MCP 启动器验证知识元数据并重建本机索引；
7. 调用 `doctor.ps1` 做最终诊断。

一键脚本会验证两个仓库并修改当前用户环境变量和所选 Agent 的用户配置，但不会自动 pull、checkout 或覆盖知识仓，也不会覆盖已有根指令或其他 MCP/hooks 配置。已有文件首次修改前会创建 `.aikb-backup`；重复执行保持幂等。完成后需要重启 Agent。

知识仓不在默认 `content/` 时显式传入：

```powershell
& .\system\tools\setup-aikb.ps1 -KnowledgePath <知识仓目录>
```

只配置一个 Agent：

```powershell
& .\system\tools\setup-aikb.ps1 -Agents codex
& .\system\tools\setup-aikb.ps1 -Agents claude-code
```

排障时可以跳过耗时或已经确认的阶段：

```powershell
& .\system\tools\setup-aikb.ps1 -SkipTests
& .\system\tools\setup-aikb.ps1 -SkipIndex
```

`-EnvironmentTarget Process` 只用于自动测试和临时诊断，不是正式安装方式。一键流程任一步失败都会停止并保留明确错误；修复后可以重新执行，也可以转到下面的独立步骤排查。

### 8.2 分步配置

以下九步说明完整保留，适合首次理解项目、逐项验证和故障定位。一键脚本调用的就是这些独立入口。

#### 第 1 步：准备环境

需要：

- Windows；
- Git；
- PowerShell 7，可通过 `pwsh` 调用；
- Python 3.11 或更高版本，并且 `python` 已进入 `PATH`；
- Codex、Claude Code，或其中之一。

确认版本：

```powershell
git --version
pwsh --version
python --version
```

当前 Python 核心只依赖标准库，不需要执行 `pip install`。

#### 第 2 步：分别克隆两个仓库

推荐保持入口路径稳定：

```powershell
git clone <控制仓地址> <你选择的稳定目录>\AIKB
git clone <知识仓地址> <你选择的稳定目录>\AIKB\content
Set-Location <你选择的稳定目录>\AIKB
```

上例把知识仓装配到默认位置。知识仓也可克隆到其他目录或磁盘，之后用 `-KnowledgePath` 登记。两个仓库分别 clone、pull、commit 和 push；控制仓不使用 submodule，也不记录知识仓 commit 指针。`workspace/` 不跨机器同步。

#### 第 3 步：登记控制仓和知识仓

在仓库根目录执行：

```powershell
& .\system\tools\set-aikb-home.ps1
```

脚本会：

- 从自身位置解析控制仓根目录；
- 验证控制仓 `ENTRY_RULES.md`、`system/` 以及知识仓 `.aikb-knowledge.json` 契约；
- 幂等写入当前 Windows 用户的 `AIKB_HOME` 与 `AIKB_KNOWLEDGE_HOME`，并广播环境设置变化；
- 同时更新脚本进程中的变量并回读验证；
- 输出是否发生变化以及需要重启 Agent/终端的提示。

也可以显式指定已经安顿好的目录：

```powershell
& .\system\tools\set-aikb-home.ps1 -Path <控制仓目录> -KnowledgePath <知识仓目录>
```

确认用户级值：

```powershell
[Environment]::GetEnvironmentVariable('AIKB_HOME', 'User')
[Environment]::GetEnvironmentVariable('AIKB_KNOWLEDGE_HOME', 'User')
```

使用上述 `&` 方式时，当前 PowerShell 会立即获得两个变量，可以继续执行安装。其他已经打开的终端和 Agent 不会自动刷新父进程环境；完成设置后请重新启动它们。如果改用 `pwsh -File` 启动脚本，也需要新开终端后再运行安装器。

#### 第 4 步：先验证仓库

```powershell
pwsh -NoProfile -File system/tests/validate-structure.ps1 -KnowledgePath $env:AIKB_KNOWLEDGE_HOME
python -m unittest discover -s system/tools/aikb-mcp/tests -v
pwsh -NoProfile -File system/tests/validate-adapters.ps1
```

`validate-adapters.ps1` 使用临时配置目录，不会安装到真实 Codex 或 Claude Code 用户配置。

#### 第 5 步：配置 Agent 的稳定根指令

可以运行独立安装脚本，把 `system/templates/agent-root-instruction.md` 中的单句入口写入需要使用 AIKB 的 Agent 用户级指令文件：

```powershell
pwsh -NoProfile -File system/adapters/install-root-instructions.ps1
```

只配置一个 Agent 时使用 `-Agents codex` 或 `-Agents claude-code`。脚本写入带标记的 AIKB 受管区块，保留文件中的其他内容并在首次修改前创建 `.aikb-backup`。也可以按下面的位置手工复制模板内容：

- Codex：`~/.codex/AGENTS.md`；
- Claude Code：`~/.claude/CLAUDE.md`。

默认内容为：

```md
每个新会话开始时，请从 Windows 用户环境变量 `AIKB_HOME` 获取 AIKB 根目录，并读取和持续遵循其中的 `ENTRY_RULES.md`。
```

该步骤只负责让 Agent 知道稳定入口，不注册 MCP 或 hooks。Codex 的 AGENTS.md 发现机制可参考 [OpenAI Codex 官方文档](https://developers.openai.com/codex/guides/agents-md)；Claude Code 的用户级记忆文件可参考 [Claude Code 官方文档](https://code.claude.com/docs/en/memory)。

手工配置时，如果目标文件已有其他用户指令，只追加这一句，不要覆盖原内容。

#### 第 6 步：安装 MCP 和 hooks

安装 Codex 与 Claude Code：

```powershell
pwsh -NoProfile -File system/adapters/install-all.ps1
```

只安装其中一个：

```powershell
pwsh -NoProfile -File system/adapters/install-all.ps1 -Agents codex
pwsh -NoProfile -File system/adapters/install-all.ps1 -Agents claude-code
```

这个步骤会修改当前用户的 Agent 配置，所以必须显式执行。安装器会先确认当前进程的 `AIKB_HOME` 与控制仓一致，并验证 `AIKB_KNOWLEDGE_HOME` 的知识仓契约；生成的 MCP 和 hook 命令在运行时读取这两个变量，不保存仓库绝对路径。安装器不修改 `AGENTS.md` 或 `CLAUDE.md`，也不会把 `workspace/` 数据写入 Git。

Codex 和 Claude Code 的配置机制可分别参考 [Codex 配置](https://developers.openai.com/codex/config-reference)、[Codex MCP](https://developers.openai.com/codex/mcp)、[Claude Code hooks](https://code.claude.com/docs/en/hooks)和 [Claude Code MCP](https://code.claude.com/docs/en/mcp)。

#### 第 7 步：运行诊断

```powershell
pwsh -NoProfile -File system/adapters/doctor.ps1
```

诊断应确认：

- 用户级和当前进程的 `AIKB_HOME` 有效并指向当前控制仓；
- 用户级和当前进程的 `AIKB_KNOWLEDGE_HOME` 有效并带有知识仓契约标记；
- 可以找到 Python；
- 可以找到 PowerShell 7 的 `pwsh`；
- `codex` 和 `claude-code` 清单可发现；
- 已选择 Agent 的 MCP 配置存在；
- 已选择 Agent 的 hooks 配置存在。

只安装单个 Agent 时，诊断也要传入对应参数：

```powershell
pwsh -NoProfile -File system/adapters/doctor.ps1 -Agents codex
```

#### 第 8 步：验证元数据并建立初始索引

```powershell
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 validate
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 rebuild
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 search "检索缓存"
```

`validate` 报告不合法元数据；`rebuild` 重建知识和工作状态数据库；`search` 用于确认中文检索可用。正常运行时服务会按内容指纹自动检查索引，不要求每次手工重建。

#### 第 9 步：重启 Agent 并做首次行为验证

完全退出并重新启动 Codex/Claude Code，然后开启新 Session，检查：

1. Agent 只读取 `ENTRY_RULES.md` 和必要的个人规则，不自动读取根 README。
2. 非工程任务不应自动完整接入 AIKB。
3. 工程任务需要未知知识时能够看到并调用 `aikb` MCP。
4. 搜索只返回少量候选，读取时只加载目标条目或章节。
5. 形成实际工作状态后可以创建检查点；普通问答不会写 `workspace/`。
6. 关闭任务后，工作项从 `workspace/active/` 移入 `workspace/archive/`；跨 Agent 续写前必须由 owner 显式授权 participant，legacy 工作项先 claim。

更完整的场景见 `system/tests/agent-behavior-checklist.md`。

## 9. 日常使用流程

### 9.1 Agent 查找知识

推荐路由：

1. 当前上下文已有且仍可信：直接使用。
2. 已知稳定 ID 或准确文件：直接读取。
3. 不知道位置：调用 `search_knowledge`。
4. 从候选中选择目标：调用 `read_knowledge`，尽量限制章节和字符数。
5. MCP 不可用：从控制仓 `INDEX.md` 转到知识仓 `INDEX.md`，再沿知识仓局部 README 降级。
6. 只有全库治理或正式写入前查重才读取知识仓 `CATALOG.md`。

根 README 不参与以上流程。

### 9.2 Agent 续接未完成任务

SessionStart 只会为当前 Agent/精确 Hook `session_id` 已授权的唯一活动任务提供紧凑恢复胶囊；发现外来会话任务时不注入正文、不阻断当前会话。每次 SessionStart 同时报告 candidate 总数及逾期、无 owner、声明可能重复、已结案仍留 Inbox 的计数。Agent 继续工作前仍需要核对当前分支、revision 和工作区状态。

在以下节点写检查点最有价值：

- 作出关键决定；
- 完成了实质修改和验证；
- 出现阻塞；
- Codex 与 Claude Code 交接；
- 上下文压缩前；
- 未完成任务的 Session 结束前；
- 任务关闭时。

没有状态变化时不应机械重复写入。

### 9.3 人类或 Agent 贡献知识

1. 阅读 `system/rules/CONTRIBUTING.md`。
2. 搜索现有条目并查重。
3. 用代码、测试、运行结果、权威文档或用户确认验证结论。
4. 选择 `system/templates/` 中最接近的模板。
5. 为条目分配稳定 ID、类型、标签、适用范围和关系。
6. 更新知识仓中最近一级局部 README 和 `CATALOG.md`。
7. 运行结构测试和核心测试。
8. 只在知识仓中审查并提交本次知识变更；控制面修改另在控制仓提交。

证据不足的内容进入知识仓 `experience/inbox/`（MCP 逻辑路径为 `content/experience/inbox/`），不要包装成 `verified`。

## 10. 常用维护命令

### 查看可用适配器

```powershell
pwsh -NoProfile -File system/adapters/discover-adapters.ps1
```

### 验证知识元数据

```powershell
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 validate
```

### 重建全部本机索引

```powershell
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 rebuild
```

### 命令行搜索和读取

```powershell
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 search "关键词"
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 read "aikb:稳定知识-id"
```

### 查看本机操作审计

```powershell
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 audit list --since 24h
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 audit summary --since 7d
# 默认写入 workspace/audit/reports/2026-08-27.xlsx
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 audit report --date 2026-08-27
# 省略 --date 时使用当天日期并按同一规则命名
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 audit report
# --output 必须是 .xlsx 文件路径；用于自定义目录或文件名
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 audit report --date 2026-08-27 --output E:\Reports\aikb-audit-2026-08-27.xlsx
# 暂时弃用：保留 Markdown 兼容报告，默认写入 workspace/audit/reports/2026-08-27.md
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 audit report-md --date 2026-08-27
# 查看某次调用在 diagnostic/full-local 级别保存的本机输入输出
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 audit diagnostic <调用ID>
```

`audit report` 默认写入 `workspace/audit/reports/<YYYY-MM-DD>.xlsx`，并输出 JSON 格式的文件路径与记录数；工作簿包含“概览”“调用明细”“损坏记录”三个工作表，其中调用明细默认显示可读会话名称、中文动作说明和中文结果说明，原始会话 ID 与技术摘要置于靠后列。Markdown 方案暂时保留为 `audit report-md`，该命令会给出弃用提示。

审计捕获级别由当前进程环境变量 `AIKB_AUDIT_CAPTURE_LEVEL` 控制：默认 `safe` 只写安全摘要；`diagnostic` 额外写入经脱敏、限长的 MCP/hook 输入输出；`full-local` 提高诊断记录预算，仍会脱敏常见密钥、忽略隐藏推理和二进制附件。诊断数据位于 `workspace/audit/diagnostic/`，通过调用 ID 与主审计记录关联，可用 `audit diagnostic <调用ID>` 查询。示例：`$env:AIKB_AUDIT_CAPTURE_LEVEL = 'diagnostic'`。所有审计数据均不进 Git，且没有自动保留期或清理行为。

### 预览或清理过期本机运行数据

```powershell
# 仅列出将被清理的精确路径（默认审计 90 天、检查点 180 天、runtime 30 天）
pwsh -NoProfile -File system/tools/clear-workspace.ps1
# 审查上述 JSON 后，显式执行；仍会显示 PowerShell 删除确认
pwsh -NoProfile -File system/tools/clear-workspace.ps1 -Apply
```

该脚本只处理审计的按日记录、诊断附件、fallback、报告文件，过期的归档工作项，活动任务中非当前的历史检查点，以及超过 runtime 保留期的直接子项。它始终保留活动任务 `work.md`、当前检查点、`runtime/audit.lock` 和 `audit/sessions.json`；后者没有可靠时间戳，必须人工决定是否移除。

### 运行完整自动测试

```powershell
pwsh -NoProfile -File system/tests/validate-structure.ps1
pwsh -NoProfile -File system/tests/validate-adapters.ps1
pwsh -NoProfile -File system/tests/validate-performance.ps1
python -m unittest discover -s system/tools/aikb-mcp/tests -v
```

### 卸载 Agent 集成

全部卸载：

```powershell
pwsh -NoProfile -File system/adapters/uninstall-all.ps1
```

只卸载一个 Agent：

```powershell
pwsh -NoProfile -File system/adapters/uninstall-all.ps1 -Agents codex
pwsh -NoProfile -File system/adapters/uninstall-all.ps1 -Agents claude-code
```

卸载器只清理 AIKB 管理的 MCP 区块和 hook handler，不删除 `.aikb-backup`、根指令、知识内容或 `workspace/` 工作状态。如果不再希望 Agent 读取 AIKB，还需要手工删除 `AGENTS.md`/`CLAUDE.md` 中由 `AIKB managed root instruction` 标记包围的受管区块；早期手工配置则删除指向 `ENTRY_RULES.md` 的那一句。

## 11. 常见问题与排障

### `doctor.ps1` 提示找不到 Python

确认安装 Python 3.11 或更高版本，并在新的 PowerShell 中执行 `python --version`。当前启动器按命令名 `python` 查找，不自动搜索 `py.exe`。

### Agent 看不到 `aikb` MCP

依次检查：

1. 是否运行了真实安装器，而不只是 `validate-adapters.ps1`；
2. `doctor.ps1` 是否通过；
3. 是否完全重启了 Agent；
4. 用户配置中是否已经存在非 AIKB 管理的同名 MCP；
5. 控制仓或知识仓是否被移动，而用户级 `AIKB_HOME` 或 `AIKB_KNOWLEDGE_HOME` 仍指向旧路径。

任一仓库移动后，在新目录重新运行 `set-aikb-home.ps1 -Path <控制仓> -KnowledgePath <知识仓>` 并重启 Agent。适配器配置通过环境变量解析路径，通常不需要重新安装；仍建议运行一次 `doctor.ps1` 复核。

### 搜索结果为空或过期

先执行：

```powershell
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 validate
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 rebuild
```

随后确认目标 Markdown 位于 `AIKB_KNOWLEDGE_HOME` 的四个知识分类目录之一、带有合法 Front Matter，并且状态和过滤条件匹配。MCP 返回的 `content/...` 是稳定逻辑路径，不要求物理知识仓位于控制仓内。主题导航 README 不作为正式知识条目进入 FTS。

### 安装器拒绝覆盖同名 MCP

这是预期保护。先检查现有 `aikb` MCP 是否由其他程序或手工配置创建，再决定重命名、手工合并或删除。不要绕过保护直接覆盖未知配置。

### hooks 故障是否会阻断 Agent

公共 hook 包装器采用 fail-open：Python 不存在或处理异常时返回成功，不阻断普通会话，并在仍能定位控制仓时写入独立 fallback JSON。Stop 的检查点提醒只适用于当前 owner/participant 的精确会话，外来会话不阻断；同一工作状态只阻止一次。SessionStart 的知识提醒只做数量和人工复核提示，不自动删除、晋升或关闭。`session_id` 由 Hook 提供且只是技术关联标签，不是密码学凭据；`AIKB_HOME` 完全无效时无法定位审计目录，这一到达前故障只能由 Agent 自身日志或 doctor 诊断发现。

### 可以删除 `workspace/db` 吗

可以。两个数据库都是派生索引。删除后运行 `rebuild`，或者等待服务在首次调用时重建。删除 `workspace/active` 或 `archive` 则会删除真正的本机任务记录，应谨慎处理。

## 12. 当前限制与后续扩展点

- 当前只考虑 Windows。
- 当前正式适配 Codex 和 Claude Code。
- MCP 服务依赖本机 Python 3.11+。
- 当前没有外部向量检索；SQLite FTS 是默认轻量检索层。
- 不做跨机器 Working State 同步。
- 不保存完整 Session transcript，也不使用 transcript 自动生成权威知识。
- MCP 不提供正式知识写入；正式知识仍通过文件修改、审查和 Git 提交完成。
- 根入口依赖用户环境变量 `AIKB_HOME`，知识定位依赖 `AIKB_KNOWLEDGE_HOME`；任一仓库移动后需要重新设置变量并重启相关进程。

未来可以在不改变 Markdown 权威地位的前提下扩展：

- 新 Agent 适配器；
- 可选向量召回或混合排序；
- 更丰富但仍受预算控制的关系遍历；
- 打包后的独立可执行文件，减少 Python 环境依赖；
- 更完整的 hook 行为自动化测试。

## 13. Git 管理边界

控制仓提交：

- `ENTRY_RULES.md`、转发用的 `INDEX.md` / `CATALOG.md` 和本 README；
- `system/` 中的规则、Schema、工具、适配器、模板和测试；
- `workspace/README.md` 和 `workspace/.gitignore`。

知识仓提交：

- `.aikb-knowledge.json` 契约标记；
- 知识仓自己的 `README.md`、`INDEX.md` 和 `CATALOG.md`；
- `knowledge/`、`experience/`、`workflows/` 和 `projects/` 中经过治理的知识与局部导航。

控制仓通过 `.gitignore` 排除默认装配目录 `/content/`，不记录知识仓 gitlink 或 commit 指针。两个仓库分别审查、提交和推送；一次工作同时修改两边时，检查点会记录两个 Git 快照，但提交仍保持独立。

两边都不应该提交：

- `workspace/active/`、`workspace/archive/`、`workspace/audit/`、`workspace/db/` 或 `workspace/runtime/`；
- Agent 用户配置和 `.aikb-backup`；
- Python 缓存、虚拟环境、密钥或原始 Session 数据。

结构调整或正式知识修改后，至少运行：

```powershell
pwsh -NoProfile -File system/tests/validate-structure.ps1
git diff --check
```

涉及 MCP、工作状态或适配器时，再运行对应的 Python 和适配器测试。改变知识根解析、索引范围或 SessionStart 路径时，还应运行 `validate-performance.ps1`，确认双仓定位没有引入明显的热路径回退。
