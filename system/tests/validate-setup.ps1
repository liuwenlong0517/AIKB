# 在系统临时目录中验证一键配置编排、原有指令保留、幂等性和环境恢复。
# 测试只使用 Process 级环境变量，不应改变真实用户配置。
$ErrorActionPreference = 'Stop'
# 显式固定输出编码为 UTF-8，避免重定向/非控制台环境下 GBK 字节被按 UTF-8 解码成乱码。
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)

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
$previousProcessKnowledgeHome = $env:AIKB_KNOWLEDGE_HOME
$previousUserKnowledgeHome = [Environment]::GetEnvironmentVariable('AIKB_KNOWLEDGE_HOME', 'User')

try {
    # 用旧版本绝对路径指令模拟升级场景，验证安装器会清理并备份旧内容。
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

    # 首次运行应生成全部受管文件，并且配置中不能硬编码当前仓库绝对路径。
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
    if ((Get-Content -Raw -LiteralPath (Join-Path $codexHome 'config.toml')) -notmatch 'serve --agent codex') {
        throw '一键配置未生成 Codex 审计身份参数'
    }
    if ((Get-Content -Raw -LiteralPath $claudeConfig) -notmatch 'serve --agent claude-code') {
        throw '一键配置未生成 Claude Code 审计身份参数'
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
    # 第二次运行必须保持文件哈希不变，证明安装链路是幂等的。
    & (Join-Path $repoRoot 'system\tools\setup-aikb.ps1') @setupArguments | Out-Null
    foreach ($path in $managedFiles) {
        if ((Get-FileHash -LiteralPath $path).Hash -ne $firstHashes[$path]) {
            throw "一键配置重复执行不是幂等操作：$path"
        }
    }
    if ([Environment]::GetEnvironmentVariable('AIKB_HOME', 'User') -ne $previousUserHome) {
        throw '一键配置 Process 测试意外修改了真实用户环境变量'
    }
    if ([Environment]::GetEnvironmentVariable('AIKB_KNOWLEDGE_HOME', 'User') -ne $previousUserKnowledgeHome) {
        throw '一键配置 Process 测试意外修改了真实用户知识仓环境变量'
    }

    Write-Host '一键配置校验通过：独立脚本编排、根指令保留、配置生成、诊断和幂等性均正确。' -ForegroundColor Green
}
finally {
    $env:AIKB_HOME = $previousProcessHome
    $env:AIKB_KNOWLEDGE_HOME = $previousProcessKnowledgeHome
    if ((Test-Path -LiteralPath $resolvedTestRoot) -and $resolvedTestRoot.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
