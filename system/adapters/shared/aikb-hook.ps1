# Agent hook 的 fail-open 桥接入口：只有双仓和 Python 均可用时才调用 AIKB。
# 任何环境或服务故障都返回成功，不能阻断宿主 Agent 的正常生命周期。
param(
    [Parameter(Mandatory = $true)]
    [string]$Agent,
    [Parameter(Mandatory = $true)]
    [string]$Event
)

$ErrorActionPreference = 'Stop'
# 显式抑制失效变量的路径解析错误；后续统一按空根目录走 fail-open 分支。
$repoRoot = $null
if ($env:AIKB_HOME) {
    $resolvedRepoRoot = Resolve-Path -LiteralPath $env:AIKB_HOME -ErrorAction SilentlyContinue
    if ($resolvedRepoRoot) {
        $repoRoot = $resolvedRepoRoot.Path
    }
}
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

function Write-AikbFallbackAudit {
    param([string]$OutcomeCode, [string]$ErrorType)
    try {
        $now = [DateTimeOffset]::Now
        $eventId = [guid]::NewGuid().ToString('N')
        $directory = Join-Path $repoRoot ("workspace\audit\fallback\{0}\{1}\{2}" -f $now.ToString('yyyy'), $now.ToString('MM'), $now.ToString('yyyy-MM-dd'))
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        $target = Join-Path $directory ("{0}-{1}.json" -f $now.ToString('HHmmss'), $eventId)
        $temporary = "$target.tmp-$([guid]::NewGuid().ToString('N'))"
        $record = [ordered]@{
            schema_version = 1; record_type = 'wrapper_failure'; event_id = $eventId; invocation_id = $eventId
            timestamp = $now.ToString('yyyy-MM-ddTHH:mm:ss.fffzzz'); source = 'hook'; agent = $Agent
            client = $null; connection_id = $null; session_id = $null; project_id = $null; operation = $Event
            action = @{ event = $Event; layer = 'powershell-wrapper' }; status = 'failed'
            outcome_code = $OutcomeCode; result_summary = $null; duration_ms = $null; error_type = $ErrorType
        }
        [IO.File]::WriteAllText($temporary, (($record | ConvertTo-Json -Compress -Depth 5) + "`n"), [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $target -ErrorAction Stop
    }
    catch {
        # 审计兜底本身也必须 fail-open。
    }
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    # 未安装 Python 时跳过 AIKB，保持 hook 对宿主会话透明。
    Write-AikbFallbackAudit -OutcomeCode 'python_not_found' -ErrorType 'CommandNotFoundException'
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
    $handlerExitCode = $LASTEXITCODE
    if ($handlerExitCode -eq 0 -and $null -ne $result) {
        $result
    }
    elseif ($handlerExitCode -ne 0) {
        Write-AikbFallbackAudit -OutcomeCode 'handler_nonzero_exit' -ErrorType "ExitCode$handlerExitCode"
    }
    exit 0
}
catch {
    # Hook 不得因为本机 AIKB 故障阻断普通 Agent 会话。
    Write-AikbFallbackAudit -OutcomeCode 'python_start_failed' -ErrorType $_.Exception.GetType().Name
    exit 0
}
finally {
    Pop-Location
}
