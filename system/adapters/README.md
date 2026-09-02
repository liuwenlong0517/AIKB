# AIKB Agent 适配器

本目录实现稳定核心之外的 Agent 配置、MCP 注册和生命周期转换。适配器采用目录自动发现：任何子目录只要包含合法 `adapter.json`，就可以被安装器识别；核心代码不枚举 Agent 名称。

每个适配器至少包含：

- `adapter.json`：平台、能力和安装入口；
- `install.ps1`：显式、幂等安装；
- `uninstall.ps1`：只移除 AIKB 管理的配置；
- `doctor.ps1`：诊断配置并写入明确标记的 MCP/hook 审计 probe，不自动修复用户配置。

运行 `discover-adapters.ps1` 查看可用适配器。`install-root-instructions.ps1` 独立配置用户级 `AGENTS.md`/`CLAUDE.md`，保留原内容并写入受管区块；`install-all.ps1` 安装 MCP 和 hooks。仓库测试只对临时目录运行，不会自动执行真实安装。

在新 Windows 机器克隆 AIKB 并准备好 Python 3.11 或更高版本后，先登记仓库位置，再显式安装：

```powershell
& .\system\tools\set-aikb-home.ps1
pwsh -NoProfile -File system/adapters/install-root-instructions.ps1
pwsh -NoProfile -File system/adapters/install-all.ps1
pwsh -NoProfile -File system/adapters/doctor.ps1
```

也可以从仓库根目录运行 `& .\system\tools\setup-aikb.ps1` 一键编排上述步骤、自动测试和索引建立；一键脚本只调用独立入口，不取代分步脚本。

初始化脚本把仓库根目录写入 Windows 用户环境变量 `AIKB_HOME`；安装器生成的 MCP 与 hook 命令只在运行时读取该变量，不保存仓库绝对路径，并通过 `serve --agent codex|claude-code` 为审计提供适配器身份。安装器把集成注册到当前用户配置，因此换机器需要重新运行；仓库在本机移动后只需重新设置变量、重启 Agent 并运行诊断。配置模板和实现随 Git 同步，`workspace/` 工作状态和审计不迁移。安装前会为已有配置创建一次 `.aikb-backup`，重复安装保持幂等；若已经存在非安装器管理的同名 `aikb` MCP，安装会停止而不是覆盖。

Claude Code 在 Windows 上可能默认用 Git Bash 解释 command hook，因此其 AIKB handlers 显式设置 `shell: powershell`，让 `$env:AIKB_HOME` 只由 PowerShell 展开；Codex 继续使用自身支持的完整 `pwsh` 命令格式。共享包装器和 Python CLI 将 hook 的 stdin/stdout 固定为 UTF-8，不依赖 Windows 活动代码页或用户级 Python 编码配置。新增或修改 hooks 时必须通过 `validate-adapters.ps1` 执行生成后的 handler，并以中文路径和中文反馈验证真实往返，而不是只测试手工构造的等价命令。

新增 Agent 时只新增一个自描述目录。Agent 不支持 hooks 时仍可通过 MCP 使用知识与工作状态；不支持 MCP 时继续通过根 `INDEX.md` 和各级 `INDEX.md` 降级。
