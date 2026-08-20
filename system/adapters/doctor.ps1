param(
    [ValidateSet('codex', 'claude-code')]
    [string[]]$Agents = @('codex', 'claude-code'),
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }),
    [string]$ClaudeHome = $(Join-Path $HOME '.claude'),
    [string]$ClaudeUserConfig = $(Join-Path $HOME '.claude.json'),
    [ValidateSet('User', 'Process')]
    [string]$EnvironmentTarget = 'User'
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$results = [System.Collections.Generic.List[object]]::new()

$userAikbHome = [Environment]::GetEnvironmentVariable('AIKB_HOME', 'User')
$processAikbHome = $env:AIKB_HOME
$userHomeValid = $false
if ($userAikbHome -and (Test-Path -LiteralPath $userAikbHome -PathType Container)) {
    $userHomeValid = (Resolve-Path -LiteralPath $userAikbHome).Path.Equals($repoRoot, [StringComparison]::OrdinalIgnoreCase)
}
$processHomeValid = $false
if ($processAikbHome -and (Test-Path -LiteralPath $processAikbHome -PathType Container)) {
    $processHomeValid = (Resolve-Path -LiteralPath $processAikbHome).Path.Equals($repoRoot, [StringComparison]::OrdinalIgnoreCase)
}
if ($EnvironmentTarget -eq 'User') {
    $results.Add([pscustomobject]@{ Check = 'AIKB_HOME:User'; Passed = $userHomeValid; Detail = $(if ($userAikbHome) { $userAikbHome } else { '未设置' }) })
}
$results.Add([pscustomobject]@{ Check = 'AIKB_HOME:Process'; Passed = $processHomeValid; Detail = $(if ($processAikbHome) { $processAikbHome } else { '未继承，请重启终端或 Agent' }) })

$python = Get-Command python -ErrorAction SilentlyContinue
$results.Add([pscustomobject]@{ Check = 'Python'; Passed = $null -ne $python; Detail = $(if ($python) { $python.Source } else { '未找到 Python' }) })
$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
$results.Add([pscustomobject]@{ Check = 'PowerShell 7'; Passed = $null -ne $pwsh; Detail = $(if ($pwsh) { $pwsh.Source } else { '未找到 pwsh' }) })

foreach ($adapter in & (Join-Path $PSScriptRoot 'discover-adapters.ps1')) {
    $results.Add([pscustomobject]@{ Check = "Adapter:$($adapter.Id)"; Passed = $true; Detail = $adapter.Path })
}

$checks = @()
if ($Agents -contains 'codex') {
    $checks += @{ Name = 'Codex Root'; Path = Join-Path $CodexHome 'AGENTS.md'; Pattern = 'AIKB_HOME.*ENTRY_RULES\.md' }
    $checks += @{ Name = 'Codex MCP'; Path = Join-Path $CodexHome 'config.toml'; Pattern = '\[mcp_servers\.aikb\]' }
    $checks += @{ Name = 'Codex Hooks'; Path = Join-Path $CodexHome 'hooks.json'; Pattern = 'aikb-hook\.ps1' }
}
if ($Agents -contains 'claude-code') {
    $checks += @{ Name = 'Claude Root'; Path = Join-Path $ClaudeHome 'CLAUDE.md'; Pattern = 'AIKB_HOME.*ENTRY_RULES\.md' }
    $checks += @{ Name = 'Claude MCP'; Path = $ClaudeUserConfig; Pattern = '"aikb"' }
    $checks += @{ Name = 'Claude Hooks'; Path = Join-Path $ClaudeHome 'settings.json'; Pattern = 'aikb-hook\.ps1' }
}
foreach ($check in $checks) {
    $exists = Test-Path -LiteralPath $check.Path -PathType Leaf
    $matched = $exists -and ((Get-Content -Raw -LiteralPath $check.Path) -match $check.Pattern)
    $results.Add([pscustomobject]@{ Check = $check.Name; Passed = $matched; Detail = $check.Path })
}

$results | Format-Table -AutoSize
if (@($results | Where-Object { -not $_.Passed }).Count -gt 0) { exit 1 }
