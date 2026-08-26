# Codex 适配器诊断入口；将 CODEX_HOME 或显式路径交给公共诊断脚本。
param([string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }))
& (Join-Path $PSScriptRoot '..\doctor.ps1') -Agents codex -CodexHome $CodexHome
exit 0
