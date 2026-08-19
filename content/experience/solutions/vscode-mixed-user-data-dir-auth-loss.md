---
status: verified
tags: [vscode, windows, authentication, safe-storage, dpapi]
applicable_versions: "VS Code 1.131.0 / Electron 42.7.0 / Windows"
last_verified: 2026-08-05
review_when: "VS Code 调整 sharedStorage、safeStorage、Windows 安装器或便携模式实现时"
supersedes: [content/experience/inbox/vscode-mixed-user-data-dir-auth-loss.md]
---

# 统一 VS Code user-data-dir 修复重启后登录会话丢失

## 背景

Windows Installer 版 VS Code 可能被自定义快捷方式追加 `--user-data-dir` 和 `--extensions-dir`，同时终端中的 `code` 命令仍使用默认 `%APPDATA%/Code`。两种入口交替使用时，本机存在两套用户数据目录和不同的 `Local State` 加密主密钥。

VS Code 官方便携模式只支持 Windows ZIP 发行版，并要求在安装目录内使用 `data` 目录；不应在 Windows Installer 版上用快捷方式参数模拟长期便携模式。

## 问题

登录当次运行正常，但关闭后通过另一入口启动时，VS Code 日志出现：

```text
Error while decrypting the ciphertext provided to safeStorage.decryptString.
```

GitHub Authentication 日志呈现“本次 `Stored 1 sessions`，下次启动 `Got 0 sessions`”的变化，表现为每次重新打开都要求登录。

## 解决方案

1. 关闭全部 VS Code 进程，完整备份默认用户数据目录、自定义用户数据目录、两套扩展目录、`.vscode-shared` 和相关快捷方式。
2. 对比两边的 `settings.json`、`keybindings.json` 和扩展目录，确认统一目录不会丢失当前配置。不要复制可能包含冲突认证密文的 `state.vscdb` 或 `Local State`。
3. Windows Installer 版统一使用默认 `%APPDATA%/Code` 与 `%USERPROFILE%/.vscode/extensions`，移除快捷方式中的 `--user-data-dir`、`--extensions-dir`。
4. 如果公共开始菜单快捷方式受管理员权限保护且当前任务不应绕过 UAC，可在当前用户的开始菜单目录创建同路径、同名称、无自定义参数的用户级快捷方式，利用 Windows 的用户级开始菜单覆盖公共入口。
5. 保留旧自定义目录作为回滚来源，重新启动标准实例并由用户完成一次认证；不要自动操作认证窗口或凭据。
6. 完整关闭并冷启动至少两次，每次检查进程参数、认证日志与解密错误。

## 验证

一次 VS Code 1.131.0、Electron 42.7.0、Windows 环境的实测结果：

- 修复前，两套 `Local State` 的 DPAPI 主密钥都能由当前 Windows 用户解开，抽样 SecretStorage 密文也能用对应主密钥解密，排除了 DPAPI 整体损坏。
- 标准实例根进程命令行不再包含 `--user-data-dir` 或 `--extensions-dir`，子进程明确使用默认 `%APPDATA%/Code`。
- 重新登录后日志出现 `Stored 1 sessions`、`Login success` 和 `Got 1 verified sessions`。
- 连续两次完整关闭和冷启动均出现 `Got stored sessions`、`Got 1 verified sessions`。
- 两次冷启动的 `main.log`、`renderer.log` 均未出现 `safeStorage` 或解密错误。
- 旧自定义用户数据目录的日志时间不再更新，确认启动入口没有回到旧目录。

## 适用范围

适用于 Windows 桌面版 VS Code，尤其是 Windows Installer 版同时存在默认目录和快捷方式自定义 `--user-data-dir` 的环境。本次验证版本为 VS Code 1.131.0、Electron 42.7.0；其他版本应重新检查共享存储和便携模式行为。

如果统一入口后仍失败，应使用官方建议的 `code --verbose --vmodule="*/components/os_crypt/*=1"` 采集日志，并继续检查单目录权限、清理软件、用户配置迁移和 Windows DPAPI。不要把 Linux 的 `password-store` 方案直接套用到 Windows。

## 关联信息

- VS Code Portable mode：<https://code.visualstudio.com/docs/setup/portable>
- VS Code Settings Sync：<https://code.visualstudio.com/docs/configure/settings-sync>
- Electron `safeStorage`：<https://www.electronjs.org/docs/latest/api/safe-storage>
