# AIKB 控制层命令手册

本手册是 AIKB 控制仓的全量命令参考，面向需要安装、运行、诊断、验证或维护 AIKB 的人类维护者。内容按当前控制仓工作树中的实际入口脚本、Python CLI 参数解析器和 MCP 工具定义整理；如果实现与本手册不一致，以当前实现和测试结果为准，并应同步修订本手册。

根目录 `README.md` 中的“常用维护命令”仍是快速入口，本手册不替换、不删减其中的常用命令。本文档只补充完整参数、输入输出、异常边界和不常用入口。

## 1. 使用约定

### 1.1 工作目录与启动方式

以下示例默认在控制仓根目录 `AIKB_HOME` 执行，并使用 PowerShell 7：

    Set-Location $env:AIKB_HOME
    pwsh --version
    python --version

PowerShell 脚本推荐使用以下形式启动，以避免当前 PowerShell 配置文件或执行策略影响脚本：

    pwsh -NoProfile -ExecutionPolicy Bypass -File <script.ps1> <parameters>

参数名区分脚本参数和 CLI 参数：PowerShell 脚本使用 `-Parameter`，Python CLI 使用 `--option`。路径包含空格时始终使用引号；路径包含中文时不需要额外转义。

### 1.2 双仓路径

| 名称 | 默认来源 | 作用 |
|---|---|---|
| `AIKB_HOME` | Windows 用户环境变量；部分直接脚本也可从自身位置推断 | 控制仓根目录，必须包含 `ENTRY_RULES.md` 和 `system/` |
| `AIKB_KNOWLEDGE_HOME` | 用户环境变量；未设置时为 `<AIKB_HOME>\content` | 独立知识仓根目录，必须包含 `.aikb-knowledge.json`，且 `kind=aikb-knowledge`、`contract_version=1` |
| `workspace` | `<AIKB_HOME>\workspace` | 本机活动任务、归档、审计和派生数据库；自定义 workspace 仍必须位于该目录内 |

`set-aikb-home.ps1` 负责登记两个环境变量。`aikb.ps1` 启动器会先验证两个根目录；Python CLI 还支持显式的 `--repo-root`、`--knowledge-root` 和 `--workspace-root`。控制仓与知识仓必须是两个独立 Git 根，知识仓不能放在控制仓的 `system/` 或 `workspace/` 内。

### 1.3 退出码与输出

- 成功通常返回 `0`。
- PowerShell 脚本遇到路径、配置或子步骤错误会抛出异常；调用方应检查 `$LASTEXITCODE`，不能只看是否有部分输出。
- Python CLI 的参数解析错误通常返回 `2`；设置、知识验证、索引或业务异常通常返回非零并将错误写入 stderr。部分未捕获异常会带 Python traceback，这是当前实现行为。
- JSON CLI 输出使用 UTF-8、非 ASCII 字符不转义，便于中文查阅和下游处理。
- `serve` 是 stdio MCP 服务，不是一次性查询命令；它从 stdin 读取 JSON-RPC，向 stdout 输出 JSON-RPC，日志或异常不能混入协议输出。

### 1.4 哪些文件不是命令

`system/adapters/shared/AdapterConfig.psm1` 是被安装器调用的 PowerShell 模块，`system/tools/aikb-mcp/aikb/*.py` 是 Python 内部模块，`system/templates/` 下文件是模板，均没有面向维护者的独立命令入口。下文只列出可以直接运行、被 Agent 直接调用，或通过 `python -m`/console script 暴露的入口。

## 2. 首次配置与环境命令

### 2.1 一键配置：`setup-aikb.ps1`

场景：新机器首次装配 AIKB，或需要按同一顺序重新执行环境登记、仓库测试、根指令安装、MCP/hooks 安装、索引建立和诊断。脚本会调用各独立入口，任何失败阶段都会中止后续步骤。

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/setup-aikb.ps1

参数：

| 参数 | 类型/默认值 | 可用选项与作用 |
|---|---|---|
| `-Agents` | `string[]`；`codex`、`claude-code` | 可传一个或两个 Agent；默认两个都处理。 |
| `-CodexHome` | 字符串；`$env:CODEX_HOME`，否则 `$HOME\.codex` | Codex 用户配置目录；用于根指令、`config.toml` 和 `hooks.json`。 |
| `-ClaudeHome` | 字符串；`$HOME\.claude` | Claude Code 用户目录；用于 `CLAUDE.md` 和 `settings.json`。 |
| `-ClaudeUserConfig` | 字符串；`$HOME\.claude.json` | Claude Code MCP 用户配置文件。 |
| `-KnowledgePath` | 可选字符串；`<控制仓>\content` | 外置知识仓路径；必须通过知识仓契约校验。 |
| `-EnvironmentTarget` | `User` 或 `Process`；默认 `User` | `User` 写入当前 Windows 用户环境并广播变化；`Process` 只用于当前进程/自动测试，不持久化。 |
| `-SkipTests` | switch | 跳过结构、Python 核心和适配器自动测试；安装和后续诊断仍会执行。 |
| `-SkipIndex` | switch | 跳过 `validate` 和 `rebuild`；适合只重复安装配置。 |

示例：

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/setup-aikb.ps1 -Agents codex -KnowledgePath 'E:\Data\AIKB-Knowledge'
    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/setup-aikb.ps1 -Agents codex -EnvironmentTarget Process -CodexHome 'C:\Temp\aikb-codex' -ClaudeHome 'C:\Temp\aikb-claude' -ClaudeUserConfig 'C:\Temp\aikb-claude.json' -SkipIndex
    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/setup-aikb.ps1 -SkipTests -SkipIndex

执行结果：按 `[1/6]` 至 `[6/6]` 输出阶段进度。默认依次完成：

1. 调用 `set-aikb-home.ps1` 登记双仓路径；
2. 运行 `validate-structure.ps1`、Python unittest 和 `validate-adapters.ps1`；
3. 配置所选 Agent 的根指令；
4. 安装所选 Agent 的 MCP 与 hooks；
5. 验证知识元数据并重建知识/工作状态索引；
6. 运行 `doctor.ps1`。

成功后会提示重启已选择的 Agent。安装器会保留无关用户配置；首次修改已有文件时创建 `.aikb-backup`，不会自动 pull、checkout、覆盖知识仓，也不会把 `workspace/` 写入 Git。

异常情况：

- 缺少 `git`、`pwsh` 或 `python`：启动前失败；
- 控制仓、知识仓或 `.aikb-knowledge.json` 无效：路径登记阶段失败；
- 未通过结构、核心、适配器测试：停止安装；
- 目标 Agent 配置中存在未由 AIKB 管理的同名 `aikb` MCP：安装拒绝覆盖；
- 诊断失败：最后阶段报错；应先查看诊断表，再单独运行对应脚本。

### 2.2 登记双仓路径：`set-aikb-home.ps1`

场景：首次安装、控制仓/知识仓移动后，或需要显式验证路径契约。脚本同时设置 `AIKB_HOME` 和 `AIKB_KNOWLEDGE_HOME`，并在当前 PowerShell 进程立即更新 `$env:` 值。

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/set-aikb-home.ps1

参数：

| 参数 | 类型/默认值 | 可用选项与作用 |
|---|---|---|
| `-Path` | 字符串；脚本推断出的控制仓根目录 | 控制仓路径，必须包含 `ENTRY_RULES.md` 和 `system/`。 |
| `-KnowledgePath` | 可选字符串；`<Path>\content` | 知识仓路径，必须存在契约文件且兼容版本为 1。 |
| `-Target` | `User` 或 `Process`；默认 `User` | 写入 Windows 用户级或当前进程级环境变量。 |
| `-PassThru` | switch | 输出对象而不是人类提示文本；对象含 `Target`、旧值、新值和 `Changed`。 |

示例：

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/set-aikb-home.ps1
    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/set-aikb-home.ps1 -Path 'E:\CodeSpace\AIKB' -KnowledgePath 'E:\Data\AIKB-Knowledge'
    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/set-aikb-home.ps1 -Target Process -PassThru

执行结果：默认输出两个环境变量的实际值以及重启终端/Agent 的提示；`-PassThru` 输出 `PSCustomObject`。`User` 目标会发送 Windows 环境刷新广播，但已启动的 Agent 和终端不会改变已有环境，仍需重启。

异常情况：非 Windows 环境、控制仓路径不存在或缺少入口、知识仓缺少契约文件、契约 `kind`/`contract_version` 不匹配、写入后回读不一致，均会抛出异常。脚本不会猜测缺失路径。

## 3. Agent 适配器命令

### 3.1 查看适配器：`discover-adapters.ps1`

场景：确认当前控制仓自动发现了哪些 Agent 适配器。脚本只扫描 `system/adapters/` 的直接子目录中带 `adapter.json` 的目录，`shared/` 不会被误报。

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/adapters/discover-adapters.ps1

无脚本参数。每个适配器输出 `Id`、`Name`、`Platforms`、`McpStdio`、`Hooks` 和物理 `Path`；当前实现通常输出 `codex` 与 `claude-code`。没有可发现适配器时可以无输出但不一定报错。

异常情况：`adapter.json` 不存在的目录被跳过；存在但 JSON 损坏或字段无法读取时会抛出 PowerShell/JSON 异常。

### 3.2 安装根指令：`install-root-instructions.ps1`

场景：只配置 Agent 用户级根指令，让 Agent 知道从 `AIKB_HOME` 读取 `ENTRY_RULES.md`；不安装 MCP，也不安装 hooks。

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/adapters/install-root-instructions.ps1

参数：

| 参数 | 类型/默认值 | 可用选项与作用 |
|---|---|---|
| `-Agents` | `string[]`；`codex`、`claude-code` | 默认两个都配置；可只指定一个。 |
| `-CodexHome` | 字符串；`$env:CODEX_HOME` 或 `$HOME\.codex` | Codex 的 `AGENTS.md` 所在目录。 |
| `-ClaudeHome` | 字符串；`$HOME\.claude` | Claude Code 的 `CLAUDE.md` 所在目录。 |

示例：

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/adapters/install-root-instructions.ps1 -Agents codex
    pwsh -NoProfile -ExecutionPolicy Bypass -File system/adapters/install-root-instructions.ps1 -Agents claude-code -ClaudeHome 'C:\Temp\claude'

执行结果：在 Codex 写入 `<CodexHome>\AGENTS.md`，在 Claude Code 写入 `<ClaudeHome>\CLAUDE.md`。保留原文，只替换 AIKB 受管标记区块；已有文件首次修改前创建同路径的 `.aikb-backup`；重复执行幂等。也会清理旧的单句绝对路径入口，避免旧路径残留。

异常情况：模板缺失或不能提取入口指令、目标目录无法创建、文件无写权限时失败。该脚本不会修改 MCP、hooks、知识内容或工作状态。

### 3.3 安装 MCP 与 hooks：`install-all.ps1`

场景：把 AIKB MCP stdio 服务和生命周期 hooks 注册到一个或两个 Agent。该脚本不负责根指令，首次完整配置应由 `setup-aikb.ps1` 或先运行 `install-root-instructions.ps1`。

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/adapters/install-all.ps1

参数：

| 参数 | 类型/默认值 | 可用选项与作用 |
|---|---|---|
| `-Agents` | `string[]`；`codex`、`claude-code` | 默认两个 Agent；可只安装一个。 |
| `-CodexHome` | 字符串；`$env:CODEX_HOME` 或 `$HOME\.codex` | Codex 的 `config.toml`/`hooks.json` 目录。 |
| `-ClaudeHome` | 字符串；`$HOME\.claude` | Claude Code 的 `settings.json` 目录。 |
| `-ClaudeUserConfig` | 字符串；`$HOME\.claude.json` | Claude Code MCP 配置文件。 |

示例：

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/adapters/install-all.ps1 -Agents codex
    pwsh -NoProfile -ExecutionPolicy Bypass -File system/adapters/install-all.ps1 -Agents claude-code -ClaudeHome 'C:\Temp\claude' -ClaudeUserConfig 'C:\Temp\claude.json'

执行结果：Codex 使用受管 TOML 区块；Claude Code 使用带 `AIKB_MANAGED=1` 的 JSON MCP 对象；两个 Agent 的 hooks 都指向共享 `aikb-hook.ps1`。配置只保存环境变量引用，不硬编码当前仓库绝对路径。安装成功后需重启 Agent。

异常情况：未设置 `AIKB_HOME` 或 `AIKB_KNOWLEDGE_HOME`、环境变量与当前安装仓不一致、知识仓缺少契约文件、JSON/TOML 配置无法读取、同名 MCP 非 AIKB 管理，均会拒绝安装。安装器只在受管范围内修改文件，不覆盖未知配置。

### 3.4 卸载 MCP 与 hooks：`uninstall-all.ps1`

场景：移除一个或两个 Agent 的 AIKB 管理配置。

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/adapters/uninstall-all.ps1

参数与安装器相同：`-Agents`（`codex`/`claude-code`）、`-CodexHome`、`-ClaudeHome`、`-ClaudeUserConfig`。

示例：

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/adapters/uninstall-all.ps1 -Agents codex
    pwsh -NoProfile -ExecutionPolicy Bypass -File system/adapters/uninstall-all.ps1 -Agents claude-code -ClaudeHome 'C:\Temp\claude' -ClaudeUserConfig 'C:\Temp\claude.json'

执行结果：只移除 AIKB 管理的 MCP 区块、MCP 对象和 hook handler；保留其他 Agent 配置、用户配置、`.aikb-backup`、根指令文件、知识内容和 `workspace/`。卸载后若不希望 Agent 再读取 AIKB，还需手工移除根指令文件中的受管区块。

异常情况：配置不存在时相应项通常跳过；配置损坏、目标路径不可读写或受管结构无法解析时失败。卸载不会因为没有安装过而删除整个用户配置文件。

### 3.5 诊断：`doctor.ps1`

场景：检查双仓变量、Python、PowerShell 7、适配器清单、目标 Agent 配置和真实 MCP/hook 链路；诊断不会自动修复用户配置，但会写入明确标记的审计 probe。

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/adapters/doctor.ps1

参数：

| 参数 | 类型/默认值 | 可用选项与作用 |
|---|---|---|
| `-Agents` | `string[]`；`codex`、`claude-code` | 只检查所选 Agent；默认两个。 |
| `-CodexHome` | 字符串；`$env:CODEX_HOME` 或 `$HOME\.codex` | Codex 配置目录。 |
| `-ClaudeHome` | 字符串；`$HOME\.claude` | Claude Code 配置目录。 |
| `-ClaudeUserConfig` | 字符串；`$HOME\.claude.json` | Claude Code MCP 配置文件。 |
| `-EnvironmentTarget` | `User` 或 `Process`；默认 `User` | `User` 检查用户级 `AIKB_HOME`；两种模式都会检查当前进程值。自动化隔离测试可用 `Process`。 |

示例：

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/adapters/doctor.ps1 -Agents codex
    pwsh -NoProfile -ExecutionPolicy Bypass -File system/adapters/doctor.ps1 -Agents codex -EnvironmentTarget Process -CodexHome 'C:\Temp\aikb-codex'

执行结果：输出表格形式的 `Check`、`Passed`、`Detail`；还会实际初始化 MCP、调用 `get_work_state`、处理 `pre-compact`，以验证运行链路和 Agent 身份审计。所有检查通过返回 `0`，存在失败检查返回 `1`。

异常情况：缺少当前进程双仓变量、知识仓契约、Python、pwsh、配置文件或受管标记，会在表格中标为失败；严重路径/解析错误可能直接抛出异常。诊断不是安装器，不会自动补写配置。

### 3.6 按 Agent 的便捷入口

这些脚本只是把公共脚本固定为单个 Agent，参数更少，行为与对应公共入口相同。

| 入口 | 可用参数 | 等价操作 |
|---|---|---|
| `system/adapters/codex/install.ps1` | `-CodexHome`；默认 `$env:CODEX_HOME` 或 `$HOME\.codex` | `install-all.ps1 -Agents codex -CodexHome ...` |
| `system/adapters/codex/uninstall.ps1` | `-CodexHome`；同上 | `uninstall-all.ps1 -Agents codex -CodexHome ...` |
| `system/adapters/codex/doctor.ps1` | `-CodexHome`；同上 | `doctor.ps1 -Agents codex -CodexHome ...` |
| `system/adapters/claude-code/install.ps1` | `-ClaudeHome`、`-ClaudeUserConfig`；默认 `$HOME\.claude`、`$HOME\.claude.json` | `install-all.ps1 -Agents claude-code ...` |
| `system/adapters/claude-code/uninstall.ps1` | `-ClaudeHome`、`-ClaudeUserConfig`；同上 | `uninstall-all.ps1 -Agents claude-code ...` |
| `system/adapters/claude-code/doctor.ps1` | `-ClaudeHome`、`-ClaudeUserConfig`；同上 | `doctor.ps1 -Agents claude-code ...` |

示例：

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/adapters/codex/install.ps1
    pwsh -NoProfile -ExecutionPolicy Bypass -File system/adapters/claude-code/doctor.ps1 -ClaudeHome 'C:\Temp\claude' -ClaudeUserConfig 'C:\Temp\claude.json'

### 3.7 生命周期 hook 包装器：`aikb-hook.ps1`

场景：由 Codex/Claude Code 配置直接调用；也可人工模拟 hook 输入，验证真实 stdin/stdout、UTF-8 和 fail-open 行为。它接收 stdin 中的 JSON，不应把普通日志写到 stdout。

    '{"cwd":"E:\\CodeSpace\\AIKB","prompt":"检查任务"}' | pwsh -NoProfile -ExecutionPolicy Bypass -File system/adapters/shared/aikb-hook.ps1 -Agent codex -Event session-start

参数：

| 参数 | 必填 | 可用值/作用 |
|---|---|---|
| `-Agent` | 是 | 任意非空字符串，安装器当前传 `codex` 或 `claude-code`；用于审计身份。 |
| `-Event` | 是 | `session-start`、`sessionstart`、`pre-compact`、`precompact`、`stop`、`session-end`、`sessionend` 或其他字符串。下划线会归一化为短横线；未知事件返回空对象。 |

stdin payload 常用字段：`cwd` 或 `project_path`（项目路径）、`session_id`/`sessionId`/`conversation_id`（会话标识）、`stop_hook_active`（Stop 递归保护）。空 stdin 会按 `{}` 处理。

结果：

- 唯一活动任务 + `session-start`：返回 `hookSpecificOutput.additionalContext` 恢复胶囊；
- 无活动任务：返回 `{}`；
- 多个活动任务：返回 `{}`，并记录 `multiple_active_work`；
- `stop` 且检查点后 Git 状态变化：返回 `decision=block`，要求先写检查点；
- `stop_hook_active=true` 或 Git 未变化：返回 `{}`；
- `pre-compact`/`session-end`：正常记录事件并返回 `{}`；
- wrapper 正常调用 Python 时输出紧凑 JSON；Python 不存在、双仓无效、handler 非零或启动失败时 wrapper 仍返回退出码 `0`，必要时写入 `workspace/audit/fallback/`，不阻断宿主 Agent。

异常情况：直接运行 Python CLI 时坏 JSON 或处理异常会非零退出；通过 wrapper 调用时这些故障按 fail-open 处理。`AIKB_HOME`/知识仓完全无效时无法写 fallback，这是 wrapper 能力边界。

## 4. Python CLI

Python CLI 的实现目录为 `system/tools/aikb-mcp`。在该目录直接运行：

    Set-Location (Join-Path $env:AIKB_HOME 'system/tools/aikb-mcp')
    python -m aikb <command> <options>

从控制仓根目录运行，推荐使用 PowerShell 启动器：

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/aikb-mcp/scripts/aikb.ps1 <command> <options>

项目还在 `pyproject.toml` 声明了 console script；在该项目已安装到当前 Python 环境时，可以使用等价形式：

    aikb-mcp <command> <options>

PowerShell 启动器自身只有一个参数：`AikbArguments`，类型为字符串数组，并接收所有剩余参数；它不新增业务选项，只负责双仓校验、切换到 Python 工具目录并转发到 `python -m aikb`。`aikb-mcp` 也是同一 Python 入口的安装后别名。Python 根命令、每个顶层子命令以及每个 `audit` 子命令都自动提供 `-h`/`--help`；下文的参数表省略这个重复的帮助选项。

### 4.1 全局选项

全局选项应放在子命令之前；`aikb.ps1` 会把剩余参数原样转交 Python。

| 选项 | 类型/默认值 | 作用与约束 |
|---|---|---|
| `-h`/`--help` | switch | 显示帮助并退出。 |
| `--repo-root PATH` | 可选路径 | 显式控制仓；必须是有效 AIKB 控制仓。 |
| `--knowledge-root PATH` | 可选路径 | 显式知识仓；必须是有效契约仓。传入后优先于环境变量。 |
| `--workspace-root PATH` | 可选路径 | 显式 workspace；必须位于控制仓 `workspace/` 内。 |

示例：

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/aikb-mcp/scripts/aikb.ps1 --repo-root 'E:\CodeSpace\AIKB' --knowledge-root 'E:\Data\AIKB-Knowledge' search '索引'

路径解析失败、知识仓与控制仓相同、知识仓在 `system/` 或 `workspace/` 内，或 workspace 越界时，CLI 在业务执行前失败。

### 4.2 `serve`：启动 stdio MCP 服务

场景：Agent 的 MCP 配置调用；通常不由人类在交互终端中手工运行。

    python -m aikb serve --agent codex

参数：`--agent AGENT`，默认 `unknown`，可传任意字符串；安装器传 `codex` 或 `claude-code`。不显式写子命令时，CLI 也默认进入 `serve`，但 Agent 身份为 `unknown`：

    python -m aikb

结果：通过 stdin/stdout 运行 MCP JSON-RPC，支持 `initialize`、`ping`、`tools/list`、`tools/call`、空的 `resources/list`、`prompts/list` 和 `logging/setLevel`。服务会将 MCP 调用写入本机审计。

异常情况：双仓配置无效、Python/SQLite 运行时不可用或协议输入损坏时服务可能结束或返回 JSON-RPC 错误；不要在 stdout 注入调试文本。

### 4.3 `validate`：验证知识元数据

    python -m aikb validate

无命令专属参数。结果为 JSON：

    {
      "valid": true,
      "documents": 10,
      "errors": [],
      "ids": ["aikb:example:entry"]
    }

校验 Front Matter 必填字段、稳定 ID、类型/状态、tags、relations、正式条目的适用版本/验证日期、重复 ID 和关系目标。只读知识文件，不写索引。合法返回 `0`；发现错误仍输出完整报告并返回 `1`。

异常情况：知识仓路径或文件无法读取、Front Matter 解析发生不可恢复错误时可能直接抛出异常；不要把 `valid=false` 当作命令成功。

### 4.4 `rebuild`：重建全部派生索引

    python -m aikb rebuild

无命令专属参数。先从知识 Markdown 重建 `workspace/db/aikb-knowledge.db`，再重建活动/归档工作状态的 `aikb-work.db`。返回 JSON，包含两个索引的条目数、tokenizer 或数据库路径。

异常情况：知识验证失败、SQLite 建库/完整性检查失败、workspace 无法创建或临时文件替换失败时非零退出；Markdown 是事实源，数据库可在关闭查看器后重新生成。

### 4.5 `search`：搜索知识

    python -m aikb search '检索缓存'

参数：

| 参数 | 类型/默认值 | 可用选项与作用 |
|---|---|---|
| `query` | 必填位置参数，字符串 | 中英文关键词；去除首尾空白后不得为空。 |
| `--limit` | 整数，默认 `5` | 结果数量预算；实现会限制到 `1..20`，超出范围会被裁剪而不是报错。 |

示例：

    python -m aikb search 'Windows 编码' --limit 10
    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/aikb-mcp/scripts/aikb.ps1 search '双仓' --limit 3

结果 JSON 含 `query`、`count`、`results`、`index`。每个候选包含稳定 ID、标题、类型、状态、逻辑路径、章节、tags、分数、匹配来源、摘要、截断标记和内容哈希。索引缺失、过期或损坏时会在搜索前自动重建。

异常情况：空 query 抛出 `query 不能为空`；知识验证/索引失败时非零退出。没有匹配项是正常成功结果（`count=0`），不是异常。

### 4.6 `read`：读取单篇知识

    python -m aikb read 'aikb:example:entry'

参数：

| 参数 | 类型/默认值 | 可用选项与作用 |
|---|---|---|
| `identifier` | 必填位置参数，字符串 | 稳定 ID，或 `content/...` 逻辑路径；反斜杠会归一化。 |
| `--section` | 可选字符串 | 按标题包含匹配读取章节及其子章节；不传则读取正文。 |
| `--max-chars` | 整数，默认 `4000` | 内容预算；实现限制到 `300..12000`，超出范围会裁剪。 |

示例：

    python -m aikb read 'aikb:knowledge:python:runtime' --section '虚拟环境' --max-chars 6000
    python -m aikb read 'content/experience/solutions/example.md' --max-chars 1000

结果 JSON 含 ID、标题、类型、状态、逻辑路径、tags、适用版本、最近验证时间、内容哈希、正文、截断标记和 relations。命令行 CLI 固定包含关系；MCP `read_knowledge` 可另行关闭关系。

异常情况：空 identifier、找不到 ID/路径、section 无匹配、索引路径越界、知识文件缺少 Front Matter 均失败。`max_chars` 太小不会失败，会按下限 `300` 处理。

### 4.7 `work-get`：查询活动工作状态

    python -m aikb work-get

参数：

| 参数 | 类型/默认值 | 作用 |
|---|---|---|
| `--project-path PATH` | 可选 | 只查询该项目；路径会规范化并转为本机项目 ID。 |
| `--work-id ID` | 可选 | 只查询指定工作项；可与 `--project-path` 同时使用。 |

示例：

    python -m aikb work-get --project-path 'E:\CodeSpace\local-code-rag'
    python -m aikb work-get --work-id 'fix-index-1234abcd'

结果 JSON 含 `count`、`unique` 和最多 5 个活动项的紧凑恢复胶囊；CLI 没有暴露 limit 参数。查询只看 `planned`、`active`、`blocked`，不读取聊天记录。

异常情况：workspace 数据库不存在或过期会自动重建；工作状态文件损坏、路径无法解析时可能失败。没有匹配项是成功的 `count=0`。

### 4.8 `hook`：直接运行 Python hook handler

    '{"cwd":"E:\\CodeSpace\\AIKB"}' | python -m aikb hook --agent codex --event session-start

参数：

| 参数 | 必填 | 作用 |
|---|---|---|
| `--agent AGENT` | 是 | 审计与行为上下文中的 Agent 名称。 |
| `--event EVENT` | 是 | 生命周期事件；可用 `session-start`/`sessionstart`、`pre-compact`/`precompact`、`stop`、`session-end`/`sessionend`，未知值会记录 unsupported 并返回空对象。 |

stdin 必须是一个 JSON 对象；空 stdin 按 `{}` 处理。结果与 `aikb-hook.ps1` 相同，但这是底层 handler：JSON 无效或业务异常会非零退出，不具备 wrapper 的 fail-open 退出策略。

### 4.9 `audit`：查询本机审计

审计命令读取 `workspace/audit/events/**/*.jsonl` 和 fallback JSON，先把同一 invocation 的开始/结束事件合并；缺少结束事件的调用显示为 `incomplete`。所有时间过滤使用本机时区。审计命令只读审计文件，报告是可重建派生物。默认 `safe` 级别只保存可读会话标签、中文动作/结果说明及安全摘要；若要保存本机诊断输入输出，须在启动 Agent 或命令前设置当前进程环境变量：

    $env:AIKB_AUDIT_CAPTURE_LEVEL = 'diagnostic'

可用值为 `safe`（默认）、`diagnostic`（脱敏且限长的输入输出）和 `full-local`（提高诊断记录预算，仍脱敏常见密钥，不保存隐藏推理、二进制或完整 traceback）。诊断附件写入 `workspace/audit/diagnostic/`，不会进入 Git。

#### `audit list`

    python -m aikb audit list --since 24h --source hook --status failed

参数：

| 选项 | 类型/可用值 | 作用 |
|---|---|---|
| `--since` | `<正整数>h` 或 `<正整数>d`，如 `24h`、`7d` | 只保留距现在的小时/天数；不支持 `m`、小数或零。 |
| `--date` | `YYYY-MM-DD` | 只保留本机该日期的记录。 |
| `--agent` | 字符串 | 精确匹配 Agent。 |
| `--source` | `mcp` 或 `hook` | 按记录来源过滤。 |
| `--status` | `succeeded`、`failed`、`noop`、`blocked`、`incomplete` | 按合并后的逻辑调用状态过滤。 |

过滤条件可组合。结果 JSON 含 `count`、`items` 和 `damaged`；`damaged` 列出无法解析的文件/行号。没有结果仍返回成功。

异常情况：`--since` 格式错误或不大于 0、`--date` 不是合法 ISO 日期时失败；审计文件损坏不会使整个命令失败，而是列入 `damaged`。

#### `audit show`

    python -m aikb audit show 'event-or-invocation-id'

参数只有必填位置参数 `event_id`。它可以是开始事件 ID、结束事件 ID 或 invocation ID；命令会在合并后的记录中查找。找到时输出完整的脱敏审计项并返回 `0`；找不到时输出错误 JSON 并返回 `1`。该子命令没有 `--date`、`--since` 等过滤选项。

#### `audit diagnostic`

    python -m aikb audit diagnostic 'invocation-id'

参数只有必填位置参数 `invocation_id`，必须是一次 MCP 或 hook 调用的 invocation ID。命令只读取该调用在 `workspace/audit/diagnostic/` 中的输入、输出或错误诊断附件，输出 JSON `count`、`items` 和 `damaged`；没有附件时返回成功的 `count=0`。通常表示未设置 `AIKB_AUDIT_CAPTURE_LEVEL`，或该次调用发生在设置前。

示例：

    $env:AIKB_AUDIT_CAPTURE_LEVEL = 'diagnostic'
    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/aikb-mcp/scripts/aikb.ps1 audit diagnostic 'a1b2c3d4...'

诊断记录始终经过密钥脱敏、NUL 清理和大小限制；`full-local` 只增加预算，不等同于保存聊天记录或模型隐藏推理。

#### `audit summary`

    python -m aikb audit summary --since 7d --agent codex

选项：`--since`、`--date`、`--agent`、`--source`；含义与 `audit list` 相同，但没有 `--status`。结果统计 `count`、各状态/Agent/来源/操作数量、平均耗时、fallback 数量、损坏数量/路径和最近活动时间。

#### `audit report`

    python -m aikb audit report --date 2026-08-27

参数：

| 选项 | 类型/默认值 | 作用与限制 |
|---|---|---|
| `--date` | `YYYY-MM-DD`；默认当天 | 报告筛选日期和默认文件名日期。 |
| `--output` | 可选路径；默认 `workspace/audit/reports/YYYY-MM-DD.xlsx` | 必须是 `.xlsx` 文件路径，不能是目录；父目录不存在时会创建。 |

示例：

    python -m aikb audit report
    python -m aikb audit report --date 2026-08-27 --output 'E:\Reports\aikb-audit-2026-08-27.xlsx'

结果：输出 JSON `{"output":"绝对路径","count":数量}`。工作簿包含“概览”“调用明细”“损坏记录”三个工作表；调用明细优先展示会话名称、中文动作说明和中文结果说明，原始 session ID 与技术摘要位于靠后列；表支持筛选并冻结表头。重复生成同一输出会原子覆盖旧派生报告，不改变 JSONL 审计事实源。

异常情况：目录作为 `--output`、扩展名不是 `.xlsx`、父路径是文件或输出不可写时失败；目录/扩展名校验返回 `2`，写入系统错误返回 `1`。日期格式错误会在过滤阶段失败。

#### `audit report-md`

    python -m aikb audit report-md --date 2026-08-27

参数与 `audit report` 相同，但 `--output` 必须是 `.md`，默认 `workspace/audit/reports/YYYY-MM-DD.md`。该入口暂时保留兼容已有自动化，stderr 会输出弃用警告；成功 stdout 仍为输出路径和记录数 JSON。建议新流程使用 `audit report` 的 Excel 输出。

## 5. 自动校验与性能命令

### 5.1 结构校验：`validate-structure.ps1`

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tests/validate-structure.ps1

参数：`-KnowledgePath`，可选知识仓路径；未传时依次使用 `AIKB_KNOWLEDGE_HOME`，否则 `<控制仓>\content`。

脚本只读检查，不修改仓库。检查根目录和 `system/`/`workspace/` 白名单、双仓 Markdown 链接、目录覆盖、Front Matter/目录职责、规则字符预算、知识仓契约和路径边界。通过时输出通过消息并返回 `0`；失败时集中输出所有错误并返回 `1`。

示例：

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tests/validate-structure.ps1 -KnowledgePath 'E:\Data\AIKB-Knowledge'

异常情况：知识仓路径不存在、入口缺失或扫描文件无法读取时可能直接抛出异常；它不会替代 `aikb validate` 的知识元数据检查。

### 5.2 适配器回归：`validate-adapters.ps1`

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tests/validate-adapters.ps1

无参数。脚本在系统临时目录和 Process 级双仓变量中验证 Codex/Claude Code 的配置生成、重复安装幂等、MCP 实际启动、hooks 实际执行、中文 UTF-8 往返、fallback 和精确卸载；不会接触真实用户 Agent 配置。通过返回 `0`；断言失败或临时测试异常返回非零。

### 5.3 一键配置回归：`validate-setup.ps1`

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tests/validate-setup.ps1

无参数。使用临时用户目录和 Process 级变量运行一键配置两次，验证根指令内容保留、旧绝对路径清理、一次性备份、配置生成、诊断和幂等性。测试结束会清理自己创建的临时目录；不修改真实用户级环境变量。通过返回 `0`，失败返回非零。

### 5.4 性能验收：`validate-performance.ps1`

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tests/validate-performance.ps1

参数：

| 参数 | 类型/默认值 | 可用范围与作用 |
|---|---|---|
| `-KnowledgePath` | 可选字符串 | 知识仓路径；默认环境变量或 `<控制仓>\content`。 |
| `-SearchSamples` | 整数 `3..21`；默认 `7` | 热搜索样本数。 |
| `-HookSamples` | 整数 `3..21`；默认 `5` | SessionStart hook 样本数。 |
| `-MaxSearchMedianMs` | 数值 `1..10000`；默认 `500` | 搜索中位耗时上限。 |
| `-MaxHookMedianMs` | 数值 `1..10000`；默认 `500` | hook 中位耗时上限。 |

示例：

    pwsh -NoProfile -ExecutionPolicy Bypass -File system/tests/validate-performance.ps1 -SearchSamples 9 -HookSamples 7 -MaxSearchMedianMs 800 -MaxHookMedianMs 800

脚本先预热索引，再通过真实 launcher 和真实 hook 进程测量样本，输出控制仓/知识仓、样本数组、中位数和阈值 JSON；超过阈值抛出失败。会在 finally 中恢复调用方进程环境，不写用户级环境变量。参数类型错误或超出 ValidateRange 范围时，PowerShell 在执行前拒绝参数。

### 5.5 Python 核心单元测试

    python -m unittest discover -s system/tools/aikb-mcp/tests -v

这是 Python 标准库 unittest 的项目测试入口，没有 AIKB 自定义参数。`discover` 从指定目录发现测试，`-v` 打开详细输出；测试覆盖 Front Matter、索引/中文搜索、MCP 协议、工作状态、审计脱敏和审计报告。全部通过返回 `0`，任一失败返回非零。

## 6. MCP 服务接口（不是 shell 命令）

以下操作由 `serve` 暴露给 Agent，不能写成 `python -m aikb search_knowledge` 直接调用。它们通过 MCP `tools/call` 使用；`tools/list` 会返回同样的 schema。所有工具拒绝未声明的额外字段。

### 6.1 `search_knowledge`

场景：不知道知识位置时发现候选，不返回整篇文档。

参数：`query`（必填字符串）、`type`（可选字符串类型过滤）、`status`（可选字符串，默认 `verified`）、`tags`（可选字符串数组）、`limit`（整数 `1..20`，默认 `5`）、`excerpt_chars`（整数 `120..1600`，默认 `700`）。

    {"name":"search_knowledge","arguments":{"query":"Windows 编码","status":"verified","tags":["windows"],"limit":5,"excerpt_chars":700}}

结果与 CLI `search` 类似，包含索引状态和候选摘要。空 query、知识验证或索引失败会以 MCP `isError=true` 文本错误返回；无匹配是正常空结果。

### 6.2 `read_knowledge`

场景：已有稳定 ID 或准确逻辑路径后读取当前 Markdown。

参数：`id_or_path`（必填字符串）、`section`（可选标题匹配）、`max_chars`（整数 `300..12000`，默认 `4000`）、`include_relations`（布尔，默认 `true`）。

    {"name":"read_knowledge","arguments":{"id_or_path":"aikb:example:entry","section":"异常处理","max_chars":4000,"include_relations":true}}

结果包含正文、元数据、截断标记和可选关系。找不到条目、章节不存在或文档缺少 Front Matter 时返回 MCP 错误。

### 6.3 `get_work_state`

场景：恢复本机未完成任务；只返回紧凑胶囊，不读取聊天记录。

参数：`project_path`（可选）、`work_id`（可选）、`limit`（整数 `1..20`，默认 `5`）。

    {"name":"get_work_state","arguments":{"project_path":"E:\\CodeSpace\\AIKB","limit":5}}

结果含 `count`、`unique` 和工作项；无匹配是正常空结果。索引缺失/过期会自动重建。

### 6.4 `checkpoint_work_state`

场景：保存有实际进度、决定、验证、阻塞或交接价值的结构化检查点；只写 `workspace/`，不写正式知识。

必填参数：`project_path`、`agent`、`session_id`。可用参数完整列表：

| 参数 | 类型/可用值 |
|---|---|
| `project_path` | 字符串，必填 |
| `work_id`、`goal`、`current_state` | 字符串；新工作项需要 `goal`，续写已有 `work_id` 可省略 goal |
| `status` | `planned`、`active`、`blocked`；默认 `active` |
| `agent`、`session_id`、`role`、`based_on`、`sensitivity` | 字符串；`agent`/`session_id` 必填，`sensitivity` 默认 `normal` |
| `decisions`、`verified_facts`、`completed`、`changed_files`、`verification`、`assumptions`、`blockers`、`next_steps`、`candidate_knowledge`、`resume_checks` | 字符串数组 |
| `repositories` | 最多 8 项的对象数组；每项必填 `path`，可选 `role` |

示例：

    {"name":"checkpoint_work_state","arguments":{"project_path":"E:\\CodeSpace\\AIKB","goal":"整理控制层命令手册","status":"active","agent":"codex","session_id":"session-20260827","role":"implement","changed_files":["system/COMMANDS.md"],"verification":["validate-structure.ps1"]}}

结果含 `work_id`、`project_id`、`checkpoint_id`、状态、work.md 路径和是否应用脱敏。异常包括缺少项目路径/新任务 goal、非法 status、仓库列表超过 8 项、单检查点超过 64 KiB 或路径越过 workspace 边界。

### 6.5 `close_work_state`

场景：结束活动任务并安全移动到本机归档。

必填参数：`work_id`、`agent`、`session_id`；`status` 必须是 `completed`、`abandoned` 或 `superseded`；`note` 可选字符串。

    {"name":"close_work_state","arguments":{"work_id":"整理命令手册-a1b2c3d4","status":"completed","agent":"codex","session_id":"session-20260827","note":"已完成校验"}}

结果含工作 ID、最终状态、最后检查点和归档路径。work ID 格式无效、匹配不到唯一活动任务、归档目标已存在或归档路径越界时返回错误；该操作不是幂等删除，重复关闭同一工作项不会当作成功。

## 7. 维护者快速选择

| 目标 | 首选命令 |
|---|---|
| 第一次完整安装 | `setup-aikb.ps1` |
| 修改控制仓/知识仓位置 | `set-aikb-home.ps1` |
| 只配置 Agent 根入口 | `install-root-instructions.ps1` |
| 只安装/卸载 Agent 集成 | `install-all.ps1` / `uninstall-all.ps1` |
| 查看问题 | `doctor.ps1` |
| 验证知识元数据 | `aikb.ps1 validate` |
| 索引过期或数据库损坏 | `aikb.ps1 rebuild` |
| 查知识 | `aikb.ps1 search`，再 `aikb.ps1 read` |
| 查活动任务 | `aikb.ps1 work-get` |
| 查审计事件 | `aikb.ps1 audit list/show/summary` |
| 查看已启用的诊断输入输出 | `aikb.ps1 audit diagnostic <调用ID>` |
| 生成人类审计报告 | `aikb.ps1 audit report` |
| 结构/适配器/配置/性能验收 | `validate-structure.ps1`、`validate-adapters.ps1`、`validate-setup.ps1`、`validate-performance.ps1` |

执行修改类命令前，先确认目标路径和 Git 状态；`workspace/` 数据和审计报告是本机派生运行数据，不应作为知识事实或 Git 提交内容。
