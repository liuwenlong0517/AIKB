param(
    [ValidateSet('codex', 'claude-code')]
    [string[]]$Agents = @('codex', 'claude-code'),
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }),
    [string]$ClaudeHome = $(Join-Path $HOME '.claude'),
    [string]$ClaudeUserConfig = $(Join-Path $HOME '.claude.json')
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$results = [System.Collections.Generic.List[object]]::new()

$python = Get-Command python -ErrorAction SilentlyContinue
$results.Add([pscustomobject]@{ Check = 'Python'; Passed = $null -ne $python; Detail = $(if ($python) { $python.Source } else { '未找到 Python' }) })

foreach ($adapter in & (Join-Path $PSScriptRoot 'discover-adapters.ps1')) {
    $results.Add([pscustomobject]@{ Check = "Adapter:$($adapter.Id)"; Passed = $true; Detail = $adapter.Path })
}

$checks = @()
if ($Agents -contains 'codex') {
    $checks += @{ Name = 'Codex MCP'; Path = Join-Path $CodexHome 'config.toml'; Pattern = '\[mcp_servers\.aikb\]' }
    $checks += @{ Name = 'Codex Hooks'; Path = Join-Path $CodexHome 'hooks.json'; Pattern = 'aikb-hook\.ps1' }
}
if ($Agents -contains 'claude-code') {
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
