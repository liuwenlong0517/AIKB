param(
    [string]$ClaudeHome = $(Join-Path $HOME '.claude'),
    [string]$ClaudeUserConfig = $(Join-Path $HOME '.claude.json')
)
& (Join-Path $PSScriptRoot '..\install-all.ps1') -Agents claude-code -ClaudeHome $ClaudeHome -ClaudeUserConfig $ClaudeUserConfig
exit 0
