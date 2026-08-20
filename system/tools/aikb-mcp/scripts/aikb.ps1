param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AikbArguments
)

$ErrorActionPreference = 'Stop'
$toolRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$scriptRepoRoot = (Resolve-Path -LiteralPath (Join-Path $toolRoot '..\..\..')).Path
$repoRoot = if ($env:AIKB_HOME) { (Resolve-Path -LiteralPath $env:AIKB_HOME).Path } else { $scriptRepoRoot }
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'ENTRY_RULES.md') -PathType Leaf)) {
    throw "AIKB_HOME 不是有效的 AIKB 仓库：$repoRoot"
}

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
