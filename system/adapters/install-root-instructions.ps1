# 将统一模板中的最小入口指令写入各 Agent 用户级根指令文件。
# 受管区块可重复生成，既保留用户内容又避免旧版本指令累积。
param(
    [ValidateSet('codex', 'claude-code')]
    [string[]]$Agents = @('codex', 'claude-code'),
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }),
    [string]$ClaudeHome = $(Join-Path $HOME '.claude')
)

$ErrorActionPreference = 'Stop'
# 显式固定输出编码为 UTF-8，避免重定向/非控制台环境下 GBK 字节被按 UTF-8 解码成乱码。
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$templatePath = Join-Path $repoRoot 'system\templates\agent-root-instruction.md'
$template = Get-Content -Raw -LiteralPath $templatePath
$instructionMatch = [regex]::Match($template, '(?ms)^```md\r?\n(.+?)\r?\n```')
if (-not $instructionMatch.Success) {
    throw "无法从模板读取 Agent 根指令：$templatePath"
}
$instruction = $instructionMatch.Groups[1].Value.Trim()
$markerStart = '<!-- >>> AIKB managed root instruction >>> -->'
$markerEnd = '<!-- <<< AIKB managed root instruction <<< -->'

function Write-RootInstruction {
    # 仅替换 AIKB 管理区块；首次覆盖已有文件时先创建一次备份。
    param([string]$Path)

    $directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    $existing = if (Test-Path -LiteralPath $Path -PathType Leaf) { Get-Content -Raw -LiteralPath $Path } else { '' }
    if ($existing -and -not (Test-Path -LiteralPath "$Path.aikb-backup")) {
        Copy-Item -LiteralPath $Path -Destination "$Path.aikb-backup"
    }

    $managedPattern = '(?ms)^' + [regex]::Escape($markerStart) + '.*?^' + [regex]::Escape($markerEnd) + '\r?\n?'
    $clean = [regex]::Replace($existing, $managedPattern, '')
    $instructionLinePattern = '(?m)^\s*' + [regex]::Escape($instruction) + '\s*\r?$'
    $clean = [regex]::Replace($clean, $instructionLinePattern, '').Trim()
    $legacyInstructionPattern = '(?m)^\s*每个新会话开始时，请读取并持续遵循\s+`[^`]*ENTRY_RULES\.md`。\s*\r?$'
    $clean = [regex]::Replace($clean, $legacyInstructionPattern, '').Trim()
    $managedBlock = "$markerStart`n$instruction`n$markerEnd"
    $output = if ($clean) { "$clean`n`n$managedBlock`n" } else { "$managedBlock`n" }

    # 临时文件与目标位于同一目录，Move-Item 才能提供接近原子替换的行为。
    $tempPath = Join-Path $directory ((Split-Path -Leaf $Path) + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($tempPath, $output, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $tempPath -Destination $Path -Force
    }
    finally {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
    Write-Host "已配置 Agent 根指令：$Path"
}

if ($Agents -contains 'codex') {
    Write-RootInstruction -Path (Join-Path $CodexHome 'AGENTS.md')
}
if ($Agents -contains 'claude-code') {
    Write-RootInstruction -Path (Join-Path $ClaudeHome 'CLAUDE.md')
}
