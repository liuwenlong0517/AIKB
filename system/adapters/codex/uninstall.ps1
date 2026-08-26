# Codex 适配器卸载入口；只移除 AIKB 标记的配置，不清理用户其他设置。
param([string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }))
& (Join-Path $PSScriptRoot '..\uninstall-all.ps1') -Agents codex -CodexHome $CodexHome
exit 0
