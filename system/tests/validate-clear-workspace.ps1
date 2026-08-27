# 在隔离临时 workspace 中验证 clear-workspace.ps1 会清理陈旧 runtime 项，但始终保留 audit.lock。
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$tempBase = [IO.Path]::GetTempPath()
$testRoot = Join-Path $tempBase ('aikb-clear-workspace-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$resolvedTestRoot = (Resolve-Path -LiteralPath $testRoot).Path
if (-not $resolvedTestRoot.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
    throw "清理脚本测试目录越过系统临时目录：$resolvedTestRoot"
}

$workspace = Join-Path $resolvedTestRoot 'workspace'
$runtime = Join-Path $workspace 'runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null

try {
    $lockPath = Join-Path $runtime 'audit.lock'
    $staleDirectory = Join-Path $runtime 'adapter-test-stale'
    $staleFile = Join-Path $runtime 'stale.tmp'
    $recentDirectory = Join-Path $runtime 'recent-marker'
    Set-Content -LiteralPath $lockPath -Value '1' -Encoding utf8NoBOM
    New-Item -ItemType Directory -Path $staleDirectory | Out-Null
    Set-Content -LiteralPath (Join-Path $staleDirectory 'marker.json') -Value '{}' -Encoding utf8NoBOM
    Set-Content -LiteralPath $staleFile -Value 'old' -Encoding utf8NoBOM
    New-Item -ItemType Directory -Path $recentDirectory | Out-Null
    Set-Content -LiteralPath (Join-Path $recentDirectory 'marker.json') -Value '{}' -Encoding utf8NoBOM

    $oldUtc = [DateTime]::UtcNow.AddDays(-10)
    [IO.File]::SetLastWriteTimeUtc($lockPath, $oldUtc)
    [IO.Directory]::SetLastWriteTimeUtc($staleDirectory, $oldUtc)
    [IO.File]::SetLastWriteTimeUtc($staleFile, $oldUtc)

    $scriptPath = Join-Path $repoRoot 'system\tools\clear-workspace.ps1'
    $preview = (& pwsh -NoProfile -ExecutionPolicy Bypass -File $scriptPath `
        -WorkspacePath $workspace -RuntimeRetentionDays 5 | Out-String).Trim() | ConvertFrom-Json
    if ($preview.CandidateCount -ne 2) {
        throw "runtime 预览候选数错误：$($preview.CandidateCount)"
    }
    if (-not (@($preview.Preserved) | Where-Object Path -eq $lockPath)) {
        throw 'runtime 预览未报告受保护的 audit.lock'
    }
    if (-not (@($preview.Candidates) | Where-Object Path -eq $staleDirectory)) {
        throw 'runtime 预览未列出陈旧子目录'
    }
    if (-not (@($preview.Candidates) | Where-Object Path -eq $staleFile)) {
        throw 'runtime 预览未列出陈旧文件'
    }
    if (@($preview.Candidates) | Where-Object Path -eq $recentDirectory) {
        throw 'runtime 预览错误地列出了未过期子目录'
    }

    & pwsh -NoProfile -ExecutionPolicy Bypass -File $scriptPath `
        -WorkspacePath $workspace -RuntimeRetentionDays 5 -Apply -Confirm:$false | Out-Null
    if (Test-Path -LiteralPath $staleDirectory) {
        throw 'Apply 未删除陈旧 runtime 子目录'
    }
    if (Test-Path -LiteralPath $staleFile) {
        throw 'Apply 未删除陈旧 runtime 文件'
    }
    if (-not (Test-Path -LiteralPath $recentDirectory -PathType Container)) {
        throw 'Apply 错误删除了未过期 runtime 子目录'
    }
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
        throw 'Apply 错误删除了 audit.lock'
    }
    Write-Output 'clear-workspace runtime 校验通过：陈旧 runtime 项可清理，audit.lock 始终保留。'
}
finally {
    if ((Test-Path -LiteralPath $resolvedTestRoot) -and
        $resolvedTestRoot.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
