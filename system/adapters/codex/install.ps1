# Codex 适配器安装入口；公共安装器负责 MCP 与 hooks 的幂等配置合并。
param([string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }))
& (Join-Path $PSScriptRoot '..\install-all.ps1') -Agents codex -CodexHome $CodexHome
exit 0
