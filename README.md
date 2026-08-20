# AIKB

AIKB（AI Knowledge Base）是一套面向个人工程工作的长期知识与跨会话工作状态系统。它独立于具体项目和具体 AI Agent，以 Git 管理的 Markdown 作为长期知识事实源，同时在本机提供 SQLite 全文检索、关系索引、MCP 工具和 Session 生命周期适配。

当前实现面向 Windows，正式支持 OpenAI Codex 和 Claude Code。系统的目标不是保存全部聊天内容，而是让不同 Agent 在需要时找到经过验证的工程知识，并让尚未完成的任务能够在后续 Session 中以紧凑、可核对的状态继续。

> 本文件是面向人类维护者的完整项目手册。它不属于 Agent 的默认接入、知识检索或会话恢复上下文。Agent 只有在用户明确要求阅读，或者任务本身是在维护 AIKB 控制面、安装流程或文档时才应读取本文件。Agent 的稳定入口是 `ENTRY_RULES.md`，不是本 README。

## 1. 项目解决什么问题

AIKB 将容易混淆的三类信息分开管理：

1. **长期工程知识**：经过验证、可复用、具有明确适用范围的知识，保存在 `content/`，进入 Git 管理。
2. **系统控制规则**：Agent 如何接入、检索、恢复任务和贡献知识，保存在 `system/`，进入 Git 管理。
3. **当前工作状态**：未完成任务的目标、进度、验证结果、阻塞和下一步，保存在 `workspace/`，仅存在当前机器，不进入 Git。

这种分离避免了两个常见问题：一是把临时对话或未经验证的结论当成长期知识；二是为了跨 Session 续接任务而把整段聊天重新塞入上下文。

AIKB 当前提供：

- Git + Markdown 的可审查长期知识库；
- 稳定 ID、类型、状态、标签和知识关系；
- SQLite FTS5 全文检索、元数据过滤和关系索引；
- 面向 Codex、Claude Code 的 stdio MCP 服务；
- 本机 Working State、检查点和任务归档；
- SessionStart、PreCompact、Stop、SessionEnd 生命周期挂接；
- 可插拔 Agent 适配器，以及幂等安装、诊断和精确卸载；
- 结构、元数据、适配器和 MCP 行为自动测试；
- MCP 失效时沿 `INDEX.md` 和局部知识 README 逐级读取的降级路径。

## 2. 明确的设计边界

### 2.1 Markdown 是长期事实源

正式知识以 `content/` 中的 Markdown 为准。`workspace/db/aikb-knowledge.db` 只是派生索引，可以删除并从 Markdown 重建。搜索结果只负责定位候选内容，Agent 最终使用的是当前 Markdown、元数据和内容哈希。

### 2.2 SQLite 不是第二份知识库

SQLite 保存全文索引、标签、关系、内容哈希和本机工作状态索引，不承担人工编辑和版本审查。数据库损坏、Schema 版本变化或内容指纹不一致时，工具会重建数据库。

### 2.3 Working State 不是聊天记忆

`workspace/` 只记录恢复任务所需的结构化状态，不保存完整聊天、隐藏推理、密钥、原始终端日志或完整 diff。工作状态中的“候选知识”也不能自动成为正式知识，必须经过贡献和验证流程后才能进入 `content/`。

### 2.4 Agent 适配不侵入核心

MCP、知识模型和工作状态模型不依赖 Codex 或 Claude Code。平台差异集中在 `system/adapters/<agent>/`。未来增加 Agent 时，优先新增适配器目录，不修改现有知识内容和核心协议。

### 2.5 根 README 不参与默认 Agent 上下文

本 README 可以随项目增长而保持详实，不承担低 token 接入职责。Agent 正常工作时采用以下最小路径：

```text
Agent 根指令
  -> ENTRY_RULES.md
      -> USER_RULES.md（每个新会话最小加载）
      -> AI_RULES.md（仅任务需要接入 AIKB 时）
          -> INDEX.md（轻量拓扑）
              -> MCP 搜索或最少的 content 局部 README/具体知识
```

只有用户明确要求、安装排障或维护 AIKB 自身时，Agent 才按需读取本 README。`CATALOG.md` 同样不在常规初始化中加载，只用于人类浏览、全库治理或正式写入前查重。

## 3. 总体架构

```text
%AIKB_HOME%
├─ ENTRY_RULES.md          Agent 唯一稳定入口
├─ INDEX.md                Agent 轻量拓扑和 MCP 失效降级入口
├─ CATALOG.md              人类可读的完整知识目录，仅登记 content/
├─ README.md               本文件，人类维护者手册
├─ system/                 控制面：规则、Schema、工具、适配器、模板、测试
├─ content/                内容面：正式知识和候选知识
└─ workspace/              运行面：本机检查点、归档和派生数据库
```

典型调用链如下：

```text
Codex / Claude Code
  ├─ 根指令 -> ENTRY_RULES.md -> AI_RULES.md
  ├─ MCP stdio -> system/tools/aikb-mcp/
  │                 ├─ content/*.md -> SQLite FTS/元数据/关系索引
  │                 └─ workspace/*.md -> SQLite 工作状态索引
  └─ hooks -> system/adapters/shared/aikb-hook.ps1
                  -> SessionStart 恢复提示 / Stop 检查点提醒
```

## 4. 根目录文件

### `ENTRY_RULES.md`

所有 Agent 共用且应保持路径稳定的唯一入口。它只负责：

- 每个新会话加载个人规则；
- 判断当前任务是否需要接入 AIKB；
- 在需要时延迟加载 `AI_RULES.md`；
- 规定 MCP、索引降级和工作状态的总体入口；
- 避免在同一会话中重复加载规则。

该文件必须保持紧凑，不承载完整架构说明。

### `INDEX.md`

面向 Agent 的轻量拓扑，不列出全部知识条目。MCP 不可用时，Agent 沿 `INDEX.md -> content 分类 README -> 主题 README -> 具体知识文件` 逐级读取最少内容。

### `CATALOG.md`

面向人类的完整知识目录，只登记 `content/` 中的内容，不登记 `system/`、`workspace/` 或根目录控制文件。新增、移动、重命名或删除知识时需要同步维护。

### `.gitignore`

排除 Python 缓存、虚拟环境和 `workspace/` 中的本机运行数据。`workspace/.gitignore` 进一步确保活动任务、归档、数据库和运行时文件不会进入 Git。

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
- 正式知识的发现、验证、查重和写入流程；
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

- `SessionStart`：按当前项目查找活动任务；只有唯一候选时返回紧凑恢复提示，不加载聊天记录。
- `PreCompact`：提供压缩前生命周期接入点；当前保持轻量，不自行解析或总结 transcript。
- `Stop`：如果 Git 工作区相对最近检查点发生变化，提醒 Agent 先保存一次检查点；同一状态只阻止一次，避免循环。
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

`agent` 是开放字符串而不是封闭枚举，因此不同 Agent 可以写入同一个任务，并通过 `agent`、`session_id` 和 `role` 区分来源。任务状态分为：

- 进行中：`planned`、`active`、`blocked`；
- 已关闭：`completed`、`abandoned`、`superseded`。

#### `adapter.schema.json`

定义适配器清单的 ID、显示名称、Windows 平台、MCP/hook 能力以及安装入口。它的作用是保证新适配器可以被自动发现和验证，而不要求核心代码硬编码平台名称。

### 5.4 `system/tools/`：本机初始化与运行工具

`set-aikb-home.ps1` 是项目位置初始化脚本。默认从脚本位置向上定位仓库，验证关键入口后使用 .NET 环境变量 API 写入当前用户的 `AIKB_HOME`；重复运行结果一致。它还支持 `-Path` 显式指定目录和 `-Target Process` 测试模式。正常安装必须使用默认的 `User` 目标，`Process` 目标只服务于自动测试和临时诊断。

`aikb-mcp/` 是轻量 MCP 与索引核心。

该工具使用 Python 3.11 标准库实现，不依赖外部向量数据库、外部 RAG 服务或第三方 Python 包。入口既可以作为命令行工具使用，也可以作为 stdio MCP 服务器运行。

#### Python 模块职责

- `aikb/__main__.py`：命令行入口，提供 `serve`、`validate`、`rebuild`、`search`、`read`、`work-get` 和 `hook` 子命令。
- `aikb/config.py`：解析仓库根目录和本机 `workspace/` 路径，统一数据库位置并创建运行目录。
- `aikb/frontmatter.py`：读取和渲染受控 YAML 风格 Front Matter；不依赖 PyYAML。
- `aikb/indexer.py`：扫描正式知识、验证元数据、构建 FTS/元数据/标签/关系表并原子替换数据库。
- `aikb/knowledge.py`：实现搜索、过滤、短词回退、按稳定 ID 或准确路径读取、章节裁剪和关系返回。
- `aikb/workstate.py`：创建检查点、脱敏、Git 状态签名、恢复胶囊、工作索引重建和任务归档。
- `aikb/server.py`：实现 JSON-RPC stdio MCP 协议、工具声明、参数边界和错误转换。
- `aikb/hooks.py`：把 Session 生命周期事件转换为工作状态恢复或检查点提醒。
- `scripts/aikb.ps1`：Windows 启动器，优先从 `AIKB_HOME` 定位仓库和 Python 模块；在仓库内手工执行且变量缺失时可以从脚本位置回退定位。
- `tests/test_core.py`：核心行为单元测试。

#### 知识数据库

`workspace/db/aikb-knowledge.db` 由 `content/` 中带合法知识元数据的 Markdown 派生，主要包含：

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

单个检查点最大 64 KiB；列表字段和文本字段还有更小的内部截断限制。写入前执行常见密钥和 Token 模式脱敏，并拒绝越过 `workspace/active` 或 `workspace/archive` 的路径。

#### MCP 协议边界

当前服务实现 stdio JSON-RPC，支持初始化、ping、工具列表和工具调用，并声明兼容 MCP `2024-11-05`、`2025-03-26` 和 `2025-06-18` 协议版本。它不暴露资源或 Prompt，也不提供正式知识写入工具。

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

- `validate-structure.ps1`：检查根目录白名单、三平面边界、Markdown 本地链接、知识元数据、稳定 ID、关系目标、适配器清单和规则文件预算。
- `validate-adapters.ps1`：在临时用户目录中安装两次并卸载，验证幂等性、配置合法性、无关配置保留和精确清理，不触碰真实用户配置。
- `agent-behavior-checklist.md`：供 Codex、Claude Code 或未来 Agent 执行的人工行为验收场景。
- `system/tools/aikb-mcp/tests/test_core.py`：验证 Front Matter、数据库损坏重建、中文检索、关系读取、MCP 协议、工作状态、脱敏、归档和上下文预算。

## 6. `content/`：长期知识内容面

`content/` 是唯一允许保存长期知识的区域：

- `content/knowledge/`：跨项目通用的工程知识；
- `content/experience/inbox/`：未完成验证或归类的候选知识；
- `content/experience/solutions/`：经过验证的问题解决方案；
- `content/experience/pitfalls/`：容易重复触发的陷阱；
- `content/experience/decisions/`：需要保留背景和取舍的重要决策；
- `content/workflows/`：可重复执行的开发、调试、评审和发布流程；
- `content/projects/<project>/`：只对特定项目成立的长期事实。

主题目录中的 README 是局部导航，不集中堆放知识正文。正式知识遵循“一条知识一个文件”，必须包含证据、验证结果、适用范围和复核条件。

## 7. `workspace/`：本机运行面

`workspace/` 除 README 和 `.gitignore` 外全部被 Git 忽略：

- `active/`：尚未完成的任务和历史检查点；
- `archive/`：已完成、放弃或被替代的任务；
- `db/aikb-knowledge.db`：知识派生索引；
- `db/aikb-work.db`：工作状态派生索引；
- `runtime/`：锁、临时文件和运行标记。

工作状态默认不跨机器共享。克隆仓库到新电脑可以重建知识数据库，但不会带回旧电脑上的未完成任务。

## 8. 初次使用流程

以下步骤都在 Windows PowerShell 7 中执行。先选择最终存放位置，再通过初始化脚本把位置登记为用户环境变量；后续说明不依赖固定盘符。

### 第 1 步：准备环境

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

### 第 2 步：克隆到稳定位置

推荐保持入口路径稳定：

```powershell
git clone <你的-AIKB-仓库地址> <你选择的稳定目录>\AIKB
Set-Location <你选择的稳定目录>\AIKB
```

目录可以位于任意本机磁盘，但应在运行下一步之前安顿好位置。`workspace/` 不跨机器同步，因此换电脑时只需要重新克隆知识库并重新初始化环境变量。

### 第 3 步：登记 `AIKB_HOME`

在仓库根目录执行：

```powershell
& .\system\tools\set-aikb-home.ps1
```

脚本会：

- 从自身位置解析当前仓库根目录；
- 验证 `ENTRY_RULES.md`、`system/` 和 `content/` 是否存在；
- 幂等写入当前 Windows 用户的 `AIKB_HOME` 环境变量，并广播环境设置变化；
- 同时更新脚本进程中的变量并回读验证；
- 输出是否发生变化以及需要重启 Agent/终端的提示。

也可以显式指定已经安顿好的目录：

```powershell
& .\system\tools\set-aikb-home.ps1 -Path <AIKB目录>
```

确认用户级值：

```powershell
[Environment]::GetEnvironmentVariable('AIKB_HOME', 'User')
```

使用上述 `&` 方式时，当前 PowerShell 会立即获得 `$env:AIKB_HOME`，可以继续执行安装。其他已经打开的终端和 Agent 不会自动刷新父进程环境；完成设置后请重新启动它们。如果改用 `pwsh -File` 启动脚本，也需要新开终端后再运行安装器。

### 第 4 步：先验证仓库

```powershell
pwsh -NoProfile -File system/tests/validate-structure.ps1
python -m unittest discover -s system/tools/aikb-mcp/tests -v
pwsh -NoProfile -File system/tests/validate-adapters.ps1
```

`validate-adapters.ps1` 使用临时配置目录，不会安装到真实 Codex 或 Claude Code 用户配置。

### 第 5 步：配置 Agent 的稳定根指令

将 `system/templates/agent-root-instruction.md` 中的单句入口复制到需要使用 AIKB 的 Agent 用户级指令文件：

- Codex：`~/.codex/AGENTS.md`；
- Claude Code：`~/.claude/CLAUDE.md`。

默认内容为：

```md
每个新会话开始时，请从 Windows 用户环境变量 `AIKB_HOME` 获取 AIKB 根目录，并读取和持续遵循其中的 `ENTRY_RULES.md`。
```

该步骤只负责让 Agent 知道稳定入口，不注册 MCP 或 hooks。Codex 的 AGENTS.md 发现机制可参考 [OpenAI Codex 官方文档](https://developers.openai.com/codex/guides/agents-md)；Claude Code 的用户级记忆文件可参考 [Claude Code 官方文档](https://code.claude.com/docs/en/memory)。

如果目标文件已有其他用户指令，只追加这一句，不要覆盖原内容。

### 第 6 步：安装 MCP 和 hooks

安装 Codex 与 Claude Code：

```powershell
pwsh -NoProfile -File system/adapters/install-all.ps1
```

只安装其中一个：

```powershell
pwsh -NoProfile -File system/adapters/install-all.ps1 -Agents codex
pwsh -NoProfile -File system/adapters/install-all.ps1 -Agents claude-code
```

这个步骤会修改当前用户的 Agent 配置，所以必须显式执行。安装器会先确认当前进程的 `AIKB_HOME` 与正在安装的仓库一致；生成的 MCP 和 hook 命令在运行时读取 `AIKB_HOME`，不保存仓库绝对路径。安装器不修改 `AGENTS.md` 或 `CLAUDE.md`，也不会把 `workspace/` 数据写入 Git。

Codex 和 Claude Code 的配置机制可分别参考 [Codex 配置](https://developers.openai.com/codex/config-reference)、[Codex MCP](https://developers.openai.com/codex/mcp)、[Claude Code hooks](https://code.claude.com/docs/en/hooks)和 [Claude Code MCP](https://code.claude.com/docs/en/mcp)。

### 第 7 步：运行诊断

```powershell
pwsh -NoProfile -File system/adapters/doctor.ps1
```

诊断应确认：

- 用户级和当前进程的 `AIKB_HOME` 有效并指向当前仓库；
- 可以找到 Python；
- 可以找到 PowerShell 7 的 `pwsh`；
- `codex` 和 `claude-code` 清单可发现；
- 已选择 Agent 的 MCP 配置存在；
- 已选择 Agent 的 hooks 配置存在。

只安装单个 Agent 时，诊断也要传入对应参数：

```powershell
pwsh -NoProfile -File system/adapters/doctor.ps1 -Agents codex
```

### 第 8 步：验证元数据并建立初始索引

```powershell
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 validate
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 rebuild
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 search "检索缓存"
```

`validate` 报告不合法元数据；`rebuild` 重建知识和工作状态数据库；`search` 用于确认中文检索可用。正常运行时服务会按内容指纹自动检查索引，不要求每次手工重建。

### 第 9 步：重启 Agent 并做首次行为验证

完全退出并重新启动 Codex/Claude Code，然后开启新 Session，检查：

1. Agent 只读取 `ENTRY_RULES.md` 和必要的个人规则，不自动读取根 README。
2. 非工程任务不应自动完整接入 AIKB。
3. 工程任务需要未知知识时能够看到并调用 `aikb` MCP。
4. 搜索只返回少量候选，读取时只加载目标条目或章节。
5. 形成实际工作状态后可以创建检查点；普通问答不会写 `workspace/`。
6. 关闭任务后，工作项从 `workspace/active/` 移入 `workspace/archive/`。

更完整的场景见 `system/tests/agent-behavior-checklist.md`。

## 9. 日常使用流程

### 9.1 Agent 查找知识

推荐路由：

1. 当前上下文已有且仍可信：直接使用。
2. 已知稳定 ID 或准确文件：直接读取。
3. 不知道位置：调用 `search_knowledge`。
4. 从候选中选择目标：调用 `read_knowledge`，尽量限制章节和字符数。
5. MCP 不可用：沿 `INDEX.md` 和 `content/` 局部 README 降级。
6. 只有全库治理或正式写入前查重才读取 `CATALOG.md`。

根 README 不参与以上流程。

### 9.2 Agent 续接未完成任务

SessionStart 可以发现当前项目唯一活动任务并提供紧凑恢复胶囊。Agent 继续工作前仍需要核对当前分支、revision 和工作区状态。

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
6. 更新最近一级局部 README 和 `CATALOG.md`。
7. 运行结构测试和核心测试。

证据不足的内容进入 `content/experience/inbox/`，不要包装成 `verified`。

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

### 运行完整自动测试

```powershell
pwsh -NoProfile -File system/tests/validate-structure.ps1
pwsh -NoProfile -File system/tests/validate-adapters.ps1
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

卸载器只清理 AIKB 管理的 MCP 区块和 hook handler，不删除 `.aikb-backup`、根指令、知识内容或 `workspace/` 工作状态。如果不再希望 Agent 读取 AIKB，还需要手工删除 `AGENTS.md`/`CLAUDE.md` 中指向 `ENTRY_RULES.md` 的那一句。

## 11. 常见问题与排障

### `doctor.ps1` 提示找不到 Python

确认安装 Python 3.11 或更高版本，并在新的 PowerShell 中执行 `python --version`。当前启动器按命令名 `python` 查找，不自动搜索 `py.exe`。

### Agent 看不到 `aikb` MCP

依次检查：

1. 是否运行了真实安装器，而不只是 `validate-adapters.ps1`；
2. `doctor.ps1` 是否通过；
3. 是否完全重启了 Agent；
4. 用户配置中是否已经存在非 AIKB 管理的同名 MCP；
5. 仓库是否被移动，而用户级 `AIKB_HOME` 仍指向旧路径。

仓库移动后，在新目录重新运行 `set-aikb-home.ps1` 并重启 Agent。适配器配置通过环境变量解析路径，通常不需要重新安装；仍建议运行一次 `doctor.ps1` 复核。

### 搜索结果为空或过期

先执行：

```powershell
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 validate
pwsh -NoProfile -File system/tools/aikb-mcp/scripts/aikb.ps1 rebuild
```

随后确认目标 Markdown 位于 `content/`、带有合法 Front Matter，并且状态和过滤条件匹配。主题导航 README 不作为正式知识条目进入 FTS。

### 安装器拒绝覆盖同名 MCP

这是预期保护。先检查现有 `aikb` MCP 是否由其他程序或手工配置创建，再决定重命名、手工合并或删除。不要绕过保护直接覆盖未知配置。

### hooks 故障是否会阻断 Agent

公共 hook 包装器采用 fail-open：Python 不存在或处理异常时返回成功，不阻断普通会话。Stop 的检查点提醒是唯一可能暂缓结束的行为，并且同一工作状态只阻止一次。

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
- 根入口和运行时配置依赖用户环境变量 `AIKB_HOME`；仓库移动后需要重新设置变量并重启相关进程。

未来可以在不改变 Markdown 权威地位的前提下扩展：

- 新 Agent 适配器；
- 可选向量召回或混合排序；
- 更丰富但仍受预算控制的关系遍历；
- 打包后的独立可执行文件，减少 Python 环境依赖；
- 更完整的 hook 行为自动化测试。

## 13. Git 管理边界

应该提交：

- `ENTRY_RULES.md`、`INDEX.md`、`CATALOG.md` 和本 README；
- `system/` 中的规则、Schema、工具、适配器、模板和测试；
- `content/` 中经过治理的知识；
- `workspace/README.md` 和 `workspace/.gitignore`。

不应该提交：

- `workspace/active/`；
- `workspace/archive/`；
- `workspace/db/`；
- `workspace/runtime/`；
- Agent 用户配置和 `.aikb-backup`；
- Python 缓存、虚拟环境、密钥或原始 Session 数据。

结构调整或正式知识修改后，至少运行：

```powershell
pwsh -NoProfile -File system/tests/validate-structure.ps1
git diff --check
```

涉及 MCP、工作状态或适配器时，再运行对应的 Python 和适配器测试。
