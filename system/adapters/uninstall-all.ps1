# 卸载指定 Agent 的 AIKB 管理配置；共享模块负责精确识别并删除受管内容。
# 不删除用户目录、备份文件或未标记的其他 Agent 配置。
param(
    [ValidateSet('codex', 'claude-code')]
    [string[]]$Agents = @('codex', 'claude-code'),
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }),
    [string]$ClaudeHome = $(Join-Path $HOME '.claude'),
    [string]$ClaudeUserConfig = $(Join-Path $HOME '.claude.json')
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'shared\AdapterConfig.psm1') -Force

foreach ($agent in $Agents) {
    Uninstall-AikbAdapter -Agent $agent -CodexHome $CodexHome -ClaudeHome $ClaudeHome -ClaudeUserConfig $ClaudeUserConfig
    Write-Host "已移除 AIKB $agent 适配器配置；其他配置保持不变。"
}
