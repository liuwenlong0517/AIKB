# AIKB CLI 的 PowerShell 启动器：解析双仓环境、固定工作目录并转交 Python 模块。
# 启动器不复制业务逻辑，保证直接调用和 Agent hook 使用同一 Python 入口。
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
$knowledgeRoot = if ($env:AIKB_KNOWLEDGE_HOME) {
    (Resolve-Path -LiteralPath $env:AIKB_KNOWLEDGE_HOME).Path
}
else {
    (Resolve-Path -LiteralPath (Join-Path $repoRoot 'content')).Path
}
# 先验证契约文件，再允许 Python 读取知识仓，避免把普通目录当作知识源。
if (-not (Test-Path -LiteralPath (Join-Path $knowledgeRoot '.aikb-knowledge.json') -PathType Leaf)) {
    throw "AIKB_KNOWLEDGE_HOME 不是有效的知识仓：$knowledgeRoot"
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw '未找到 Python 3.11 或更高版本。请安装 Python，或使用后续打包的 aikb-mcp.exe。'
}

$env:AIKB_HOME = $repoRoot
$env:AIKB_KNOWLEDGE_HOME = $knowledgeRoot
# Push-Location 让 Python 包可由当前工作树直接导入，finally 保证调用方目录恢复。
Push-Location -LiteralPath $toolRoot
try {
    & $python.Source -m aikb @AikbArguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
