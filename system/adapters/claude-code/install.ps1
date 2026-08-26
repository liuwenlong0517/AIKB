# Claude Code 适配器安装入口；公共安装器负责合并配置并保持无关内容。
# 这里不直接修改用户文件，便于各 Agent 入口保持一致行为。
param(
    [string]$ClaudeHome = $(Join-Path $HOME '.claude'),
    [string]$ClaudeUserConfig = $(Join-Path $HOME '.claude.json')
)
& (Join-Path $PSScriptRoot '..\install-all.ps1') -Agents claude-code -ClaudeHome $ClaudeHome -ClaudeUserConfig $ClaudeUserConfig
exit 0
