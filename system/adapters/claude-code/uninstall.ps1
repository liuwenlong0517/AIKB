# Claude Code 适配器卸载入口；只请求公共模块移除 AIKB 管理区块。
# 用户未托管的配置由共享卸载逻辑保留。
param(
    [string]$ClaudeHome = $(Join-Path $HOME '.claude'),
    [string]$ClaudeUserConfig = $(Join-Path $HOME '.claude.json')
)
& (Join-Path $PSScriptRoot '..\uninstall-all.ps1') -Agents claude-code -ClaudeHome $ClaudeHome -ClaudeUserConfig $ClaudeUserConfig
exit 0
