---
id: aikb:experience:pitfalls:windows-agent-hook-shell-encoding-boundaries
type: pitfall
status: verified
tags: [windows, powershell, hooks, python, utf-8, git-bash, agent-integration]
applicable_versions: "Windows 11；PowerShell 7 与 Windows PowerShell 5.1；CPython 3.11；Codex/Claude Code command hooks（验证于 2026-08-24）"
last_verified: "2026-08-24"
review_when: "Agent 改变 command hook 的默认 Shell 或 shell 配置语义，PowerShell 改变原生命令编码行为，或 Python 改变 Windows 标准流默认编码时"
supersedes: []
relations: []
---

# Windows Agent Hook 必须分别固定 Shell 解释器与 UTF-8 进程边界

## 背景

Windows 上的 Agent command hook 往往不是单一进程，而是一条跨运行时链路：

```text
Agent hook runner
  → Shell 解释命令
  → PowerShell 包装器读取 JSON
  → Python CLI 处理事件
  → PowerShell 接收 JSON
  → Agent 解析并展示反馈
```

这条链路至少有两个相互独立的边界：

1. **命令解释边界**：`$env:AIKB_HOME` 等语法究竟由 PowerShell、Git Bash 还是其他 Shell 解析。
2. **字节编码边界**：PowerShell 与 Python 通过 stdin/stdout 交换的字节究竟是 GBK、CP936 还是 UTF-8。

两类故障都可能表现为“hook 没有正确返回内容”，但根因和修复方式完全不同。只反复设置环境变量、切换终端字体或确认脚本路径，无法同时解决它们。

常见检索表述：`hook 乱码`、`hook 编码冲突`、`PowerShell hook 乱码`、`Git Bash PowerShell`、`AIKB_HOME 未展开`、`GBK UTF-8`。

## 问题

### 快速分类

| 现象 | 优先怀疑 | 关键证据 |
| --- | --- | --- |
| 路径变成 `:AIKB_HOME\...`，或 `$env:AIKB_HOME` 没有展开 | 命令被非 PowerShell Shell 解释 | 用户级和进程级环境变量都存在，但实际 handler 中的 PowerShell 语法在启动前已被改写 |
| 中文变成大量 `�`，英文和 JSON 标点仍正常 | stdout 字节编码与接收端解码不一致 | `�` 是 Unicode replacement character，说明字节已经在某层错误解码 |
| `ConvertFrom-Json` 报未终止字符串，位置接近中文字段 | JSON 字节在进入解析器前损坏 | 原始输出长度异常或包含 `�`，而 Python 直接构造的对象本身合法 |
| 空负载 `{}` 或纯英文测试通过，实际中文反馈失败 | 测试没有跨过非 ASCII 编码边界 | ASCII 在 GBK 与 UTF-8 中字节相同，不能证明中文链路正确 |

### 陷阱一：PowerShell 命令被错误的 Shell 解释

PowerShell 环境变量语法是 `$env:AIKB_HOME`。如果 Agent 在 Windows 上把 command hook 交给 Git Bash，Bash 会尝试展开 `$env`，随后把 `:AIKB_HOME` 当成普通文本。PowerShell 尚未启动，命令就已经损坏，因此即使 Windows 用户环境变量设置正确也不会生效。

典型误判是看到 `AIKB_HOME` 字样后不断重新设置环境变量。应先检查**安装器实际生成的 handler**，确认 command hook 的 `shell` 字段以及最终由哪个解释器执行。

可靠处理方式：

- 在平台适配器中显式声明该 Agent 所需的 Shell；Windows Claude Code handler 使用 `shell: powershell`。
- 由 hook runner 已经提供 PowerShell 时，命令体只保留原生 PowerShell 表达式，避免再套一层不必要的 Shell。
- 不假设所有 Agent 具有相同的 hook command 语义；差异留在适配器，公共业务逻辑留在共享脚本和 Python 核心。
- 测试必须读取安装器生成后的真实配置并执行 handler，不能只执行手工拼出的等价命令。

### 陷阱二：Python 的 GBK 字节被 PowerShell 当作 UTF-8 解码

中文 Windows 环境中，未启用 UTF-8 mode 的 CPython 标准流可能默认使用 `gbk`/`cp936`；PowerShell 7 的原生命令管道则可能按 UTF-8 接收输出。此时 Python 对象和 JSON 都是正确的，但输出阶段发生以下转换：

```text
Python 中文字符串
  → 按 GBK/CP936 编码成字节
  → PowerShell 按 UTF-8 解码
  → 无效字节被替换成 U+FFFD（�）
  → Agent 收到已经不可逆损坏的 Unicode 字符串
```

英文、数字和多数 JSON 标点属于 ASCII，在 GBK 与 UTF-8 中编码相同，所以它们仍然可读。这种“英文正常、中文乱码”的局部正常状态不是字体问题，反而是编码边界不一致的重要证据。

输出乱码是最直观的症状，但 stdin 也必须处理：当 `cwd`、项目名或事件字段包含中文时，PowerShell 写入 UTF-8 而 Python 按 GBK 读取，同样可能得到错误路径或直接触发解码异常。

可靠处理方式是在协议两端都显式固定 UTF-8：

```python
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8", newline="\n", write_through=True)
sys.stderr.reconfigure(encoding="utf-8", newline="\n", write_through=True)
```

```powershell
$utf8NoBom = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
```

PowerShell 包装器中的环境变量只影响当前 hook 进程及其 Python 子进程，不需要修改 Windows 系统区域，也不需要给用户的所有 Python 项目设置全局变量。Python CLI 再显式 `reconfigure`，可以避免调用方遗漏环境变量时重新退回活动代码页。

## 解决方案

### 设计原则

1. 把 hook stdin/stdout 定义为明确的 UTF-8 JSON 协议，而不是依赖运行机器的默认代码页。
2. Shell 选择属于 Agent 适配器职责；事件处理和工作状态逻辑不感知具体 Agent。
3. PowerShell 包装器负责进程启动、编码和 fail-open；Python CLI 负责协议解析与业务处理。
4. 配置生成器、生成后的配置、共享包装器和 Python CLI 必须作为完整链路验证。
5. hook 为避免阻断普通 Agent 会话可以 fail-open，但测试不能只检查退出码；必须断言返回的业务字段和中文内容。

### 排查顺序

1. 读取真实用户配置中的 handler，而不是只看生成器源码。
2. 确认 `AIKB_HOME` 的 User、Process 值和目标路径有效。
3. 查看 handler 的 `shell` 和最终 command；出现 `:AIKB_HOME` 时优先修正 Shell 解释器。
4. 分别查看 Python 的 `sys.stdin.encoding`、`sys.stdout.encoding`、`locale.getpreferredencoding(False)`，以及 PowerShell 的 `[Console]::InputEncoding`、`[Console]::OutputEncoding`、`$OutputEncoding`。
5. 使用真实 wrapper 发送含中文的 JSON，保留原始 stdout；检查能否按 UTF-8 解码、能否解析 JSON、是否包含 U+FFFD。
6. 临时在当前进程设置 `PYTHONUTF8=1` 复测。如果同一 handler 立即恢复中文，可确认是 Python/PowerShell 编码边界，而不是字体或业务数据问题。

### 不应作为项目运行前提的做法

- 不依赖 `chcp 65001`、终端字体或 PowerShell Profile；非交互 hook 可能根本不加载这些设置。
- 不要求用户开启 Windows“Beta: 使用 Unicode UTF-8 提供全球语言支持”；这是整机级行为变更，可能影响旧程序。
- 不把用户级 `PYTHONUTF8` 或 `PYTHONIOENCODING` 作为唯一修复；它们会影响其他 Python 项目，也无法约束主动指定编码的程序。
- 不只把 JSON 输出改成 ASCII escape 后就宣称完成；这只能缓解 stdout，不能修复包含中文路径的 stdin。
- 不用空对象、纯英文、退出码为 0 或“能解析 JSON”作为充分验收条件。

## 验证

本条目基于 AIKB 在 Windows 11 上两次真实 hook 故障及修复验证：

1. Shell 故障修复提交 `87f9d26`：Claude Code 生成配置显式使用 `shell: powershell`，实际 handler 能读取 `AIKB_HOME`；`SessionStart`、`PreCompact`、`Stop`、`SessionEnd` 均完成验证。
2. 编码故障复现时，Python 报告 stdin/stdout 为 `gbk`、首选编码为 `cp936`，PowerShell 7 输出编码为 UTF-8；真实 wrapper 稳定产生 `�`。
3. 仅给同一进程临时设置 `PYTHONUTF8=1` 后，同一 wrapper 的中文输出恢复正常，确认根因位于进程编码边界。
4. 修复后在外层故意设置 `PYTHONUTF8=0`、`PYTHONIOENCODING=cp936`，使用中文项目路径和中文 payload 执行生成后的 handler：
   - Codex 通过 `pwsh` 返回完整中文 JSON；
   - Claude Code 通过 `powershell.exe` 返回完整中文 JSON；
   - 两者输出均不含 U+FFFD。
5. 当前真实用户配置中的 Codex、Claude Code `SessionStart` handler 也在上述 CP936 对抗环境中通过；Python 单元测试、适配器测试、一键部署测试、结构校验和安装诊断全部通过。

回归测试必须保留至少以下断言：

- payload 中包含中文路径或中文字段；
- 返回内容包含预期中文业务语义；
- 原始或解析后的内容不包含 `\uFFFD`；
- Codex 与 Claude Code 分别使用其实际 Shell 路径；
- 执行的是安装器生成的 handler，而不是测试中重新拼接的近似命令。

## 适用范围

- 适用于 Windows 上以 command hook 连接 Agent、PowerShell 包装器和 Python CLI 的集成。
- 结论不局限于 AIKB；任何跨 Shell、跨语言、通过 stdio 交换 JSON 的本地工具都应分别约束命令解释器和字符编码。
- 已验证 PowerShell 7、Windows PowerShell 5.1、CPython 3.11，以及当前 Codex/Claude Code 适配器。
- macOS/Linux、非 PowerShell Shell、二进制 framing 或 HTTP transport 需要按各自运行时重新验证，不能直接套用 Windows 代码页结论。

## 关联信息

- [Agent 配置生成器](../../../system/adapters/shared/AdapterConfig.psm1)：按 Agent 生成 Shell 与 handler 配置。
- [共享 PowerShell hook 包装器](../../../system/adapters/shared/aikb-hook.ps1)：固定 PowerShell 与 Python 子进程的 UTF-8 边界。
- [Python CLI 入口](../../../system/tools/aikb-mcp/aikb/__main__.py)：显式重配标准流编码。
- [适配器回归测试](../../../system/tests/validate-adapters.ps1)：执行生成后的 Codex/Claude Code handler，并验证中文往返。
- [Python 核心测试](../../../system/tools/aikb-mcp/tests/test_core.py)：在 CP936 对抗环境中验证 CLI stdin/stdout。
