param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AikbArguments
)

$ErrorActionPreference = 'Stop'
$toolRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $toolRoot '..\..\..')).Path

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw '未找到 Python 3.11 或更高版本。请安装 Python，或使用后续打包的 aikb-mcp.exe。'
}

$env:AIKB_HOME = $repoRoot
Push-Location -LiteralPath $toolRoot
try {
    & $python.Source -m aikb @AikbArguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
