# AIKB Agent 适配器

本目录实现稳定核心之外的 Agent 配置、MCP 注册和生命周期转换。适配器采用目录自动发现：任何子目录只要包含合法 `adapter.json`，就可以被安装器识别；核心代码不枚举 Agent 名称。

每个适配器至少包含：

- `adapter.json`：平台、能力和安装入口；
- `install.ps1`：显式、幂等安装；
- `uninstall.ps1`：只移除 AIKB 管理的配置；
- `doctor.ps1`：只读诊断。

运行 `discover-adapters.ps1` 查看可用适配器。运行 `install-all.ps1` 才会修改当前用户的 Agent 配置；仓库测试只对临时目录运行，不会自动执行真实安装。

在新 Windows 机器克隆 AIKB 并准备好 Python 3.11 或更高版本后，先登记仓库位置，再显式安装：

```powershell
& .\system\tools\set-aikb-home.ps1
pwsh -NoProfile -File system/adapters/install-all.ps1
pwsh -NoProfile -File system/adapters/doctor.ps1
```

初始化脚本把仓库根目录写入 Windows 用户环境变量 `AIKB_HOME`；安装器生成的 MCP 与 hook 命令只在运行时读取该变量，不保存仓库绝对路径。安装器把集成注册到当前用户配置，因此换机器需要重新运行；仓库在本机移动后只需重新设置变量、重启 Agent 并运行诊断。配置模板和实现随 Git 同步，`workspace/` 工作状态不迁移。安装前会为已有配置创建一次 `.aikb-backup`，重复安装保持幂等；若已经存在非安装器管理的同名 `aikb` MCP，安装会停止而不是覆盖。

新增 Agent 时只新增一个自描述目录。Agent 不支持 hooks 时仍可通过 MCP 使用知识与工作状态；不支持 MCP 时继续通过 `INDEX.md` 和局部 README 降级。
