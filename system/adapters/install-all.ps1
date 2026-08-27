# 安装指定 Agent 的根指令、MCP 和生命周期 hooks。
# 所有用户配置修改都委托给共享模块，以统一备份、原子写入和冲突检测。
param(
    [ValidateSet('codex', 'claude-code')]
    [string[]]$Agents = @('codex', 'claude-code'),
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }),
    [string]$ClaudeHome = $(Join-Path $HOME '.claude'),
    [string]$ClaudeUserConfig = $(Join-Path $HOME '.claude.json')
)

$ErrorActionPreference = 'Stop'
# 显式固定输出编码为 UTF-8，避免重定向/非控制台环境下 GBK 字节被按 UTF-8 解码成乱码。
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
Import-Module (Join-Path $PSScriptRoot 'shared\AdapterConfig.psm1') -Force

if (-not $env:AIKB_HOME) {
    throw '未设置 AIKB_HOME。请先运行 system/tools/set-aikb-home.ps1，并在新的终端中执行安装。'
}
# 双仓路径必须显式存在，避免安装后 Agent 继承到错误的知识仓。
if (-not $env:AIKB_KNOWLEDGE_HOME) {
    throw '未设置 AIKB_KNOWLEDGE_HOME。请先运行 system/tools/set-aikb-home.ps1，并在新的终端中执行安装。'
}

foreach ($agent in $Agents) {
    Install-AikbAdapter -Agent $agent -RepoRoot $repoRoot -CodexHome $CodexHome -ClaudeHome $ClaudeHome -ClaudeUserConfig $ClaudeUserConfig
    Write-Host "已安装 AIKB $agent 适配器。"
}

Write-Host '配置已写入当前用户；重启对应 Agent 后生效。工作状态仍只保存在本机 workspace/。'
