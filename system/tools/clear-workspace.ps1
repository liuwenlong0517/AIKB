# 清理 workspace 中已经超过保留期的本机审计文件与运行检查点；默认仅输出候选项，不删除任何内容。
# 活动任务的 work.md 和其当前检查点始终保留，避免清理动作破坏可恢复的工作状态。
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    # 审计事实源、诊断附件、fallback 与可重建报告按文件最后写入时间计算保留期。
    [ValidateRange(1, 36500)]
    [int]$AuditRetentionDays = 90,

    # 已归档任务及活动任务中非当前的历史检查点按文件最后写入时间计算保留期。
    [ValidateRange(1, 36500)]
    [int]$CheckpointRetentionDays = 180,

    # 默认定位本脚本所属控制仓的 workspace；测试可显式传入独立临时目录。
    [string]$WorkspacePath = $(Join-Path $PSScriptRoot '..\\..\\workspace'),

    # 未提供时脚本只预览；提供后仍由 ShouldProcess 支持 -WhatIf 和 PowerShell 确认提示。
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'

function Get-NonReparseFiles {
    <#
    .SYNOPSIS
    递归枚举目录中的普通文件，但绝不进入符号链接或 junction。

    .DESCRIPTION
    workspace 是本机数据边界。跳过 reparse point 可避免管理员手工建立的链接将递归扫描或后续删除扩展到该边界外。
    #>
    param([Parameter(Mandatory)][System.IO.DirectoryInfo]$Directory)

    foreach ($entry in Get-ChildItem -LiteralPath $Directory.FullName -Force) {
        if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            Write-Warning "跳过 reparse point：$($entry.FullName)"
            continue
        }
        if ($entry.PSIsContainer) {
            Get-NonReparseFiles -Directory $entry
        }
        else {
            $entry
        }
    }
}

function Test-DirectoryCanBeRemoved {
    <#
    .SYNOPSIS
    确认待删目录没有 reparse point，且解析后仍在指定根目录内。

    .DESCRIPTION
    Remove-Item -Recurse 不应面对未知链接。发现链接时保留整个工作项，并在结果中报告 skipped，供人工检查后处理。
    #>
    param(
        [Parameter(Mandatory)][System.IO.DirectoryInfo]$Directory,
        [Parameter(Mandatory)][System.IO.DirectoryInfo]$Boundary
    )

    if (-not $Directory.FullName.StartsWith($Boundary.FullName + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    foreach ($entry in Get-ChildItem -LiteralPath $Directory.FullName -Force) {
        if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            return $false
        }
        if ($entry.PSIsContainer -and -not (Test-DirectoryCanBeRemoved -Directory ([System.IO.DirectoryInfo]$entry) -Boundary $Boundary)) {
            return $false
        }
    }
    return $true
}

function Get-CurrentCheckpointId {
    <#
    .SYNOPSIS
    从活动 work.md 的 Front Matter 读取当前检查点 ID。

    .DESCRIPTION
    只接受当前渲染器生成的双引号标量。格式不符时返回空值，调用方会保守地跳过该任务的历史检查点，而不是猜测当前文件。
    #>
    param([Parameter(Mandatory)][System.IO.FileInfo]$WorkFile)

    $insideFrontMatter = $false
    foreach ($line in Get-Content -LiteralPath $WorkFile.FullName -Encoding utf8) {
        if ($line -eq '---') {
            if ($insideFrontMatter) {
                break
            }
            $insideFrontMatter = $true
            continue
        }
        if ($insideFrontMatter -and $line -match '^checkpoint_id:\s*"(?<id>[^"]+)"\s*$') {
            return $Matches.id
        }
    }
    return $null
}

function New-CleanupCandidate {
    <#
    .SYNOPSIS
    将可删除对象统一表示为可序列化的候选记录。
    #>
    param(
        [Parameter(Mandatory)][string]$Category,
        [Parameter(Mandatory)][System.IO.FileSystemInfo]$Item,
        [Parameter(Mandatory)][bool]$IsDirectory
    )

    [pscustomobject]@{
        Category = $Category
        Path = $Item.FullName
        IsDirectory = $IsDirectory
        LastWriteTimeUtc = $Item.LastWriteTimeUtc.ToString('o')
    }
}

$workspace = Get-Item -LiteralPath $WorkspacePath -Force -ErrorAction Stop
if (-not $workspace.PSIsContainer) {
    throw "WorkspacePath 必须是目录：$($workspace.FullName)"
}
$workspaceRoot = [System.IO.DirectoryInfo]$workspace
$auditCutoff = [datetime]::UtcNow.AddDays(-$AuditRetentionDays)
$checkpointCutoff = [datetime]::UtcNow.AddDays(-$CheckpointRetentionDays)
$candidates = [System.Collections.Generic.List[object]]::new()
$skipped = [System.Collections.Generic.List[object]]::new()

# 审计会话标签注册表没有可靠时间戳，因此不按猜测规则清理；其余审计文件都可按最后写入时间安全判断。
foreach ($name in @('events', 'diagnostic', 'fallback', 'reports')) {
    $root = Get-Item -LiteralPath (Join-Path $workspaceRoot.FullName "audit\\$name") -Force -ErrorAction SilentlyContinue
    if (-not $root -or -not $root.PSIsContainer) {
        continue
    }
    foreach ($file in Get-NonReparseFiles -Directory ([System.IO.DirectoryInfo]$root)) {
        if ($file.LastWriteTimeUtc -lt $auditCutoff) {
            $candidates.Add((New-CleanupCandidate -Category "audit/$name" -Item $file -IsDirectory $false))
        }
    }
}

$archiveRoot = Get-Item -LiteralPath (Join-Path $workspaceRoot.FullName 'archive') -Force -ErrorAction SilentlyContinue
if ($archiveRoot -and $archiveRoot.PSIsContainer) {
    foreach ($workFile in Get-NonReparseFiles -Directory ([System.IO.DirectoryInfo]$archiveRoot) | Where-Object Name -eq 'work.md') {
        $workDirectory = $workFile.Directory
        if ($workFile.LastWriteTimeUtc -lt $checkpointCutoff) {
            if (Test-DirectoryCanBeRemoved -Directory $workDirectory -Boundary ([System.IO.DirectoryInfo]$archiveRoot)) {
                $candidates.Add((New-CleanupCandidate -Category 'archive-work-item' -Item $workDirectory -IsDirectory $true))
            }
            else {
                $skipped.Add([pscustomobject]@{ Category = 'archive-work-item'; Path = $workDirectory.FullName; Reason = '目录越过 archive 边界或包含 reparse point' })
            }
        }
    }
}

$activeRoot = Get-Item -LiteralPath (Join-Path $workspaceRoot.FullName 'active') -Force -ErrorAction SilentlyContinue
if ($activeRoot -and $activeRoot.PSIsContainer) {
    foreach ($workFile in Get-NonReparseFiles -Directory ([System.IO.DirectoryInfo]$activeRoot) | Where-Object Name -eq 'work.md') {
        $currentCheckpointId = Get-CurrentCheckpointId -WorkFile $workFile
        if (-not $currentCheckpointId) {
            $skipped.Add([pscustomobject]@{ Category = 'active-history'; Path = $workFile.Directory.FullName; Reason = '无法读取当前 checkpoint_id，已保守跳过' })
            continue
        }
        $checkpointsRoot = Get-Item -LiteralPath (Join-Path $workFile.Directory.FullName 'checkpoints') -Force -ErrorAction SilentlyContinue
        if (-not $checkpointsRoot -or -not $checkpointsRoot.PSIsContainer) {
            continue
        }
        foreach ($checkpoint in Get-NonReparseFiles -Directory ([System.IO.DirectoryInfo]$checkpointsRoot) | Where-Object Extension -eq '.md') {
            if ($checkpoint.BaseName -ne $currentCheckpointId -and $checkpoint.LastWriteTimeUtc -lt $checkpointCutoff) {
                $candidates.Add((New-CleanupCandidate -Category 'active-history-checkpoint' -Item $checkpoint -IsDirectory $false))
            }
        }
    }
}

$applied = [System.Collections.Generic.List[object]]::new()
if ($Apply) {
    foreach ($candidate in $candidates) {
        $action = "删除过期 $($candidate.Category)"
        if ($PSCmdlet.ShouldProcess($candidate.Path, $action)) {
            if ($candidate.IsDirectory) {
                Remove-Item -LiteralPath $candidate.Path -Recurse -Force
            }
            else {
                Remove-Item -LiteralPath $candidate.Path -Force
            }
            $applied.Add($candidate)
        }
    }
}

[pscustomobject]@{
    Mode = if ($Apply) { 'apply' } else { 'preview' }
    Workspace = $workspaceRoot.FullName
    AuditCutoffUtc = $auditCutoff.ToString('o')
    CheckpointCutoffUtc = $checkpointCutoff.ToString('o')
    CandidateCount = $candidates.Count
    AppliedCount = $applied.Count
    Candidates = @($candidates)
    Applied = @($applied)
    Skipped = @($skipped)
} | ConvertTo-Json -Depth 5
