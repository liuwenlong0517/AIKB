param(
    [Parameter(Mandatory = $true)]
    [string]$Agent,
    [Parameter(Mandatory = $true)]
    [string]$Event
)

$ErrorActionPreference = 'Stop'
$repoRoot = if ($env:AIKB_HOME) { (Resolve-Path -LiteralPath $env:AIKB_HOME).Path } else { $null }
if (-not $repoRoot -or -not (Test-Path -LiteralPath (Join-Path $repoRoot 'ENTRY_RULES.md') -PathType Leaf)) {
    # Hook 必须 fail-open；环境变量缺失或失效时交给普通 Agent 会话继续。
    exit 0
}
$toolRoot = Join-Path $repoRoot 'system\tools\aikb-mcp'
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    exit 0
}

# Hook JSON is an explicit UTF-8 protocol. Keep it independent from the Windows
# active code page and from the parent Agent's PowerShell defaults.
$utf8NoBom = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$payload = [Console]::In.ReadToEnd()
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
