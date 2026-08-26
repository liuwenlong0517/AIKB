# Agent hook 的 fail-open 桥接入口：只有双仓和 Python 均可用时才调用 AIKB。
# 任何环境或服务故障都返回成功，不能阻断宿主 Agent 的正常生命周期。
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
$knowledgeRoot = if ($env:AIKB_KNOWLEDGE_HOME) {
    Resolve-Path -LiteralPath $env:AIKB_KNOWLEDGE_HOME -ErrorAction SilentlyContinue
}
else {
    Resolve-Path -LiteralPath (Join-Path $repoRoot 'content') -ErrorAction SilentlyContinue
}
if (-not $knowledgeRoot -or -not (Test-Path -LiteralPath (Join-Path $knowledgeRoot.Path '.aikb-knowledge.json') -PathType Leaf)) {
    # 知识仓缺失时保持 fail-open，不能阻断普通 Agent 会话。
    exit 0
}
$env:AIKB_KNOWLEDGE_HOME = $knowledgeRoot.Path
$toolRoot = Join-Path $repoRoot 'system\tools\aikb-mcp'
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    # 未安装 Python 时跳过 AIKB，保持 hook 对宿主会话透明。
    exit 0
}

# Hook JSON 是显式 UTF-8 协议，不能依赖 Windows 活动代码页或父 Agent 的 PowerShell 默认值。
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$payload = [Console]::In.ReadToEnd()
Push-Location -LiteralPath $toolRoot
try {
    # 由 Python 统一处理事件语义；PowerShell 只负责 UTF-8 管道和退出策略。
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
