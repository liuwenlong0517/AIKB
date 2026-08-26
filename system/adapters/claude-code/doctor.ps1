# Claude Code 适配器诊断入口；实际检查逻辑集中在上级公共脚本。
# 参数只负责把用户指定的 Claude 配置路径转交给公共实现。
param(
    [string]$ClaudeHome = $(Join-Path $HOME '.claude'),
    [string]$ClaudeUserConfig = $(Join-Path $HOME '.claude.json')
)
& (Join-Path $PSScriptRoot '..\doctor.ps1') -Agents claude-code -ClaudeHome $ClaudeHome -ClaudeUserConfig $ClaudeUserConfig
exit 0
