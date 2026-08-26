# 首次配置编排器：设置双仓环境、运行校验、安装根指令与适配器并执行诊断。
# 每个阶段调用独立脚本，便于按需跳过测试/索引或单独重复执行。
param(
    [ValidateSet('codex', 'claude-code')]
    [string[]]$Agents = @('codex', 'claude-code'),
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }),
    [string]$ClaudeHome = $(Join-Path $HOME '.claude'),
    [string]$ClaudeUserConfig = $(Join-Path $HOME '.claude.json'),
    [string]$KnowledgePath,
    [ValidateSet('User', 'Process')]
    [string]$EnvironmentTarget = 'User',
    [switch]$SkipTests,
    [switch]$SkipIndex
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path

foreach ($command in @('git', 'pwsh', 'python')) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "缺少首次配置所需命令：$command"
    }
}

# 先建立当前进程可见的双仓环境，后续脚本都从同一解析结果工作。
Write-Host '[1/6] 设置控制仓和知识仓路径' -ForegroundColor Cyan
$resolvedKnowledgePath = if ($KnowledgePath) { $KnowledgePath } else { Join-Path $repoRoot 'content' }
& (Join-Path $PSScriptRoot 'set-aikb-home.ps1') -Path $repoRoot -KnowledgePath $resolvedKnowledgePath -Target $EnvironmentTarget

if (-not $SkipTests) {
    # 结构、Python 核心和适配器测试按依赖顺序执行，任一步失败即停止安装。
    Write-Host '[2/6] 运行仓库与适配器测试' -ForegroundColor Cyan
    & pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repoRoot 'system\tests\validate-structure.ps1')
    if ($LASTEXITCODE -ne 0) { throw '仓库结构测试失败' }
    & python -m unittest discover -s (Join-Path $repoRoot 'system\tools\aikb-mcp\tests') -v
    if ($LASTEXITCODE -ne 0) { throw 'Python 核心测试失败' }
    & pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repoRoot 'system\tests\validate-adapters.ps1')
    if ($LASTEXITCODE -ne 0) { throw '适配器测试失败' }
}
else {
    Write-Host '[2/6] 已按参数跳过自动测试' -ForegroundColor DarkYellow
}

Write-Host '[3/6] 配置 Agent 根指令' -ForegroundColor Cyan
& (Join-Path $repoRoot 'system\adapters\install-root-instructions.ps1') -Agents $Agents -CodexHome $CodexHome -ClaudeHome $ClaudeHome

Write-Host '[4/6] 安装 MCP 与 hooks' -ForegroundColor Cyan
& (Join-Path $repoRoot 'system\adapters\install-all.ps1') -Agents $Agents -CodexHome $CodexHome -ClaudeHome $ClaudeHome -ClaudeUserConfig $ClaudeUserConfig

if (-not $SkipIndex) {
    # 索引是可重建派生物；先验证 Markdown，再建立知识和工作状态数据库。
    Write-Host '[5/6] 验证知识并建立本机索引' -ForegroundColor Cyan
    $launcher = Join-Path $repoRoot 'system\tools\aikb-mcp\scripts\aikb.ps1'
    & $launcher validate
    & $launcher rebuild
}
else {
    Write-Host '[5/6] 已按参数跳过索引建立' -ForegroundColor DarkYellow
}

Write-Host '[6/6] 运行安装诊断' -ForegroundColor Cyan
& (Join-Path $repoRoot 'system\adapters\doctor.ps1') -Agents $Agents -CodexHome $CodexHome -ClaudeHome $ClaudeHome -ClaudeUserConfig $ClaudeUserConfig -EnvironmentTarget $EnvironmentTarget

Write-Host 'AIKB 首次配置完成。请重启已选择的 Agent；分步脚本仍可独立重复运行。' -ForegroundColor Green
