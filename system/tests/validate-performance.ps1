# 对已预热的知识搜索和 SessionStart hook 做本机热路径中位数验收。
# 脚本会暂时设置进程级双仓变量，结束时恢复调用方环境。
param(
    [string]$KnowledgePath,
    [ValidateRange(3, 21)]
    [int]$SearchSamples = 7,
    [ValidateRange(3, 21)]
    [int]$HookSamples = 5,
    [ValidateRange(1, 10000)]
    [double]$MaxSearchMedianMs = 500,
    [ValidateRange(1, 10000)]
    [double]$MaxHookMedianMs = 450
)

$ErrorActionPreference = 'Stop'
# 显式固定输出编码为 UTF-8，避免重定向/非控制台环境下 GBK 字节被按 UTF-8 解码成乱码。
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$resolvedKnowledgeRoot = if ($KnowledgePath) {
    (Resolve-Path -LiteralPath $KnowledgePath).Path
}
elseif ($env:AIKB_KNOWLEDGE_HOME) {
    (Resolve-Path -LiteralPath $env:AIKB_KNOWLEDGE_HOME).Path
}
else {
    (Resolve-Path -LiteralPath (Join-Path $repoRoot 'content')).Path
}

if (-not (Test-Path -LiteralPath (Join-Path $resolvedKnowledgeRoot '.aikb-knowledge.json') -PathType Leaf)) {
    throw "知识仓缺少 .aikb-knowledge.json：$resolvedKnowledgeRoot"
}

$launcher = Join-Path $repoRoot 'system\tools\aikb-mcp\scripts\aikb.ps1'
$hook = Join-Path $repoRoot 'system\adapters\shared\aikb-hook.ps1'
$previousAikbHome = $env:AIKB_HOME
$previousKnowledgeHome = $env:AIKB_KNOWLEDGE_HOME

function Get-Median {
    # 对样本排序后取中位值；奇偶样本都支持，便于调整验收参数。
    param([double[]]$Values)

    # 样本数由参数限制为奇数；仍采用通用实现，便于以后调整验收规模。
    $ordered = @($Values | Sort-Object)
    $middle = [Math]::Floor($ordered.Count / 2)
    if (($ordered.Count % 2) -eq 1) {
        return [double]$ordered[$middle]
    }
    return ([double]$ordered[$middle - 1] + [double]$ordered[$middle]) / 2
}

function Measure-Invocation {
    # 只测量脚本块本身，不把结果输出混入计时调用方。
    param([scriptblock]$Action)

    $watch = [Diagnostics.Stopwatch]::StartNew()
    & $Action | Out-Null
    $watch.Stop()
    return [Math]::Round($watch.Elapsed.TotalMilliseconds, 2)
}

try {
    $env:AIKB_HOME = $repoRoot
    $env:AIKB_KNOWLEDGE_HOME = $resolvedKnowledgeRoot

    # 预热负责建立或刷新派生索引；计时样本只衡量稳定热路径。
    & $launcher search '检索缓存' --limit 3 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '性能验收预热搜索失败' }

    $searchTimes = @()
    for ($index = 0; $index -lt $SearchSamples; $index++) {
        $searchTimes += Measure-Invocation { & $launcher search '检索缓存' --limit 3 }
        if ($LASTEXITCODE -ne 0) { throw '性能验收搜索失败' }
    }

    $hookPayload = @{ cwd = $repoRoot; prompt = 'AIKB 双仓性能验收' } | ConvertTo-Json -Compress
    $hookTimes = @()
    for ($index = 0; $index -lt $HookSamples; $index++) {
        # Agent 适配器会在独立 PowerShell 进程中把 hook JSON 写入标准输入；验收保持同一路径。
        $hookTimes += Measure-Invocation { $hookPayload | & pwsh -NoProfile -ExecutionPolicy Bypass -File $hook -Agent codex -Event SessionStart }
        if ($LASTEXITCODE -ne 0) { throw '性能验收 SessionStart hook 失败' }
    }

    $searchMedian = [Math]::Round((Get-Median -Values $searchTimes), 2)
    $hookMedian = [Math]::Round((Get-Median -Values $hookTimes), 2)
    # 先输出完整样本，便于定位抖动；随后再按用户阈值判定失败。
    [pscustomobject]@{
        control_root = $repoRoot
        knowledge_root = $resolvedKnowledgeRoot
        search_samples_ms = $searchTimes
        search_median_ms = $searchMedian
        search_limit_ms = $MaxSearchMedianMs
        hook_samples_ms = $hookTimes
        hook_median_ms = $hookMedian
        hook_limit_ms = $MaxHookMedianMs
    } | ConvertTo-Json -Depth 4

    if ($searchMedian -gt $MaxSearchMedianMs) {
        throw "热搜索中位耗时 $searchMedian ms 超过阈值 $MaxSearchMedianMs ms"
    }
    if ($hookMedian -gt $MaxHookMedianMs) {
        throw "SessionStart 中位耗时 $hookMedian ms 超过阈值 $MaxHookMedianMs ms"
    }

    Write-Host 'AIKB 双仓性能验收通过。'
}
finally {
    $env:AIKB_HOME = $previousAikbHome
    $env:AIKB_KNOWLEDGE_HOME = $previousKnowledgeHome
}
