param(
    [Parameter(Mandatory = $true)]
    [string]$Agent,
    [Parameter(Mandatory = $true)]
    [string]$Event
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..\..')).Path
$toolRoot = Join-Path $repoRoot 'system\tools\aikb-mcp'
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    exit 0
}

$payload = [Console]::In.ReadToEnd()
$env:AIKB_HOME = $repoRoot
Push-Location -LiteralPath $toolRoot
try {
    $result = $payload | & $python.Source -m aikb hook --agent $Agent --event $Event
    if ($LASTEXITCODE -eq 0 -and $null -ne $result) {
        $result
    }
    exit 0
}
catch {
    # Hook 不得因为本机 AIKB 故障阻断普通 Agent 会话。
    exit 0
}
finally {
    Pop-Location
}
