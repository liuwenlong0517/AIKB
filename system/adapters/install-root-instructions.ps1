param(
    [ValidateSet('codex', 'claude-code')]
    [string[]]$Agents = @('codex', 'claude-code'),
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }),
    [string]$ClaudeHome = $(Join-Path $HOME '.claude')
)

$ErrorActionPreference = 'Stop'
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
