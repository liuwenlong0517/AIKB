param(
    [ValidateSet('codex', 'claude-code')]
    [string[]]$Agents = @('codex', 'claude-code'),
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }),
    [string]$ClaudeHome = $(Join-Path $HOME '.claude'),
    [string]$ClaudeUserConfig = $(Join-Path $HOME '.claude.json')
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
Import-Module (Join-Path $PSScriptRoot 'shared\AdapterConfig.psm1') -Force

if (-not $env:AIKB_HOME) {
    throw '未设置 AIKB_HOME。请先运行 system/tools/set-aikb-home.ps1，并在新的终端中执行安装。'
}

foreach ($agent in $Agents) {
    Install-AikbAdapter -Agent $agent -RepoRoot $repoRoot -CodexHome $CodexHome -ClaudeHome $ClaudeHome -ClaudeUserConfig $ClaudeUserConfig
    Write-Host "已安装 AIKB $agent 适配器。"
}

Write-Host '配置已写入当前用户；重启对应 Agent 后生效。工作状态仍只保存在本机 workspace/。'
