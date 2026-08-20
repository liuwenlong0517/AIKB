$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$tempBase = [IO.Path]::GetTempPath()
$testRoot = Join-Path $tempBase ('aikb-setup-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$resolvedTestRoot = (Resolve-Path -LiteralPath $testRoot).Path
if (-not $resolvedTestRoot.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
    throw "一键配置测试目录越过系统临时目录：$resolvedTestRoot"
}

$codexHome = Join-Path $resolvedTestRoot 'codex'
$claudeHome = Join-Path $resolvedTestRoot 'claude'
$claudeConfig = Join-Path $resolvedTestRoot 'claude.json'
$previousProcessHome = $env:AIKB_HOME
$previousUserHome = [Environment]::GetEnvironmentVariable('AIKB_HOME', 'User')

try {
    New-Item -ItemType Directory -Path $codexHome -Force | Out-Null
    New-Item -ItemType Directory -Path $claudeHome -Force | Out-Null
    $legacyInstruction = '每个新会话开始时，请读取并持续遵循 `E:\Legacy\AIKB\ENTRY_RULES.md`。'
    Set-Content -LiteralPath (Join-Path $codexHome 'AGENTS.md') -Value "# preserve codex root`n$legacyInstruction" -Encoding utf8NoBOM
    Set-Content -LiteralPath (Join-Path $claudeHome 'CLAUDE.md') -Value "# preserve claude root`n$legacyInstruction" -Encoding utf8NoBOM

    $setupArguments = @{
        EnvironmentTarget = 'Process'
        SkipTests = $false
        SkipIndex = $true
        CodexHome = $codexHome
        ClaudeHome = $claudeHome
        ClaudeUserConfig = $claudeConfig
    }
    & (Join-Path $repoRoot 'system\tools\setup-aikb.ps1') @setupArguments | Out-Null

    $managedFiles = @(
        (Join-Path $codexHome 'AGENTS.md'),
        (Join-Path $codexHome 'config.toml'),
        (Join-Path $codexHome 'hooks.json'),
        (Join-Path $claudeHome 'CLAUDE.md'),
        (Join-Path $claudeHome 'settings.json'),
        $claudeConfig
    )
    $firstHashes = @{}
    foreach ($path in $managedFiles) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "一键配置未生成：$path" }
        $firstHashes[$path] = (Get-FileHash -LiteralPath $path).Hash
        $text = Get-Content -Raw -LiteralPath $path
        if ($text.Contains($repoRoot) -or $text.Contains($repoRoot.Replace('\', '/'))) {
            throw "一键配置生成内容仍包含仓库绝对路径：$path"
        }
    }
    if ((Get-Content -Raw -LiteralPath (Join-Path $codexHome 'AGENTS.md')) -notmatch 'preserve codex root') {
        throw '一键配置覆盖了 Codex 原有根指令'
    }
    if ((Get-Content -Raw -LiteralPath (Join-Path $claudeHome 'CLAUDE.md')) -notmatch 'preserve claude root') {
        throw '一键配置覆盖了 Claude 原有根指令'
    }
    foreach ($path in @((Join-Path $codexHome 'AGENTS.md'), (Join-Path $claudeHome 'CLAUDE.md'))) {
        if ((Get-Content -Raw -LiteralPath $path) -match [regex]::Escape($legacyInstruction)) {
            throw "一键配置未移除旧的绝对路径根指令：$path"
        }
        if (-not (Test-Path -LiteralPath "$path.aikb-backup" -PathType Leaf)) {
            throw "根指令安装前未创建一次性备份：$path"
        }
    }

    $setupArguments.SkipTests = $true
    & (Join-Path $repoRoot 'system\tools\setup-aikb.ps1') @setupArguments | Out-Null
    foreach ($path in $managedFiles) {
        if ((Get-FileHash -LiteralPath $path).Hash -ne $firstHashes[$path]) {
            throw "一键配置重复执行不是幂等操作：$path"
        }
    }
    if ([Environment]::GetEnvironmentVariable('AIKB_HOME', 'User') -ne $previousUserHome) {
        throw '一键配置 Process 测试意外修改了真实用户环境变量'
    }

    Write-Host '一键配置校验通过：独立脚本编排、根指令保留、配置生成、诊断和幂等性均正确。' -ForegroundColor Green
}
finally {
    $env:AIKB_HOME = $previousProcessHome
    if ((Test-Path -LiteralPath $resolvedTestRoot) -and $resolvedTestRoot.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
