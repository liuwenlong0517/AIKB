$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$errors = [System.Collections.Generic.List[string]]::new()

function Add-ValidationError {
    param([string]$Message)
    $errors.Add($Message)
}

$requiredRootFiles = @(
    '.gitattributes',
    '.gitignore',
    'CATALOG.md',
    'ENTRY_RULES.md',
    'INDEX.md',
    'README.md'
)

$requiredRootDirectories = @('content', 'system')
$allowedSystemEntries = @('README.md', 'rules', 'templates', 'tests')
$allowedContentEntries = @('README.md', 'experience', 'knowledge', 'projects', 'workflows')

foreach ($name in $requiredRootFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $name) -PathType Leaf)) {
        Add-ValidationError "缺少根目录文件：$name"
    }
}

foreach ($name in $requiredRootDirectories) {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $name) -PathType Container)) {
        Add-ValidationError "缺少根目录：$name"
    }
}

Get-ChildItem -LiteralPath $repoRoot -File -Force |
    Where-Object { $_.Name -notin $requiredRootFiles } |
    ForEach-Object { Add-ValidationError "根目录存在白名单外文件：$($_.Name)" }

Get-ChildItem -LiteralPath $repoRoot -Directory -Force |
    Where-Object { $_.Name -notin @('.git', 'content', 'system') } |
    ForEach-Object { Add-ValidationError "根目录存在白名单外目录：$($_.Name)" }

Get-ChildItem -LiteralPath (Join-Path $repoRoot 'system') -Force |
    Where-Object { $_.Name -notin $allowedSystemEntries } |
    ForEach-Object { Add-ValidationError "system/ 存在未定义入口：$($_.Name)" }

Get-ChildItem -LiteralPath (Join-Path $repoRoot 'content') -Force |
    Where-Object { $_.Name -notin $allowedContentEntries } |
    ForEach-Object { Add-ValidationError "content/ 存在未定义入口：$($_.Name)" }

$markdownFiles = Get-ChildItem -LiteralPath $repoRoot -Recurse -File -Filter '*.md' |
    Where-Object { $_.FullName -notmatch '[\\/]\.git[\\/]' }

foreach ($file in $markdownFiles) {
    $text = Get-Content -Raw -LiteralPath $file.FullName
    $links = [regex]::Matches($text, '\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)')
    foreach ($link in $links) {
        $target = $link.Groups[1].Value
        if ($target -match '^(https?://|mailto:|#)') {
            continue
        }

        $decodedTarget = [uri]::UnescapeDataString($target)
        $resolvedTarget = Join-Path $file.DirectoryName $decodedTarget
        if (-not (Test-Path -LiteralPath $resolvedTarget)) {
            $relativeFile = [IO.Path]::GetRelativePath($repoRoot, $file.FullName)
            Add-ValidationError "Markdown 本地链接无效：$relativeFile -> $target"
        }
    }
}

$catalogPath = Join-Path $repoRoot 'CATALOG.md'
$catalogText = Get-Content -Raw -LiteralPath $catalogPath
$contentFiles = Get-ChildItem -LiteralPath (Join-Path $repoRoot 'content') -Recurse -File -Filter '*.md'

foreach ($file in $contentFiles) {
    $relativePath = [IO.Path]::GetRelativePath($repoRoot, $file.FullName).Replace('\', '/')
    if ($catalogText -notmatch [regex]::Escape($relativePath)) {
        Add-ValidationError "CATALOG.md 未登记内容文件：$relativePath"
    }
}

if ($catalogText -match '\[[^\]]+\]\(system/') {
    Add-ValidationError 'CATALOG.md 不应登记 system/ 下的规则、模板或测试文件'
}

$entryText = Get-Content -Raw -LiteralPath (Join-Path $repoRoot 'ENTRY_RULES.md')
foreach ($requiredPath in @(
    'E:\CodeSpace\AIKB\system\rules\AI_RULES.md',
    'E:\CodeSpace\AIKB\system\rules\USER_RULES.md'
)) {
    if (-not $entryText.Contains($requiredPath)) {
        Add-ValidationError "ENTRY_RULES.md 未引用当前规则路径：$requiredPath"
    }
}

$templatePath = Join-Path $repoRoot 'system\templates\agent-root-instruction.md'
$templateText = Get-Content -Raw -LiteralPath $templatePath
$instructionMatch = [regex]::Match($templateText, '(?ms)^```md\r?\n(.+?)\r?\n```')
$expectedInstruction = '每个新会话开始时，请读取并持续遵循 `E:\CodeSpace\AIKB\ENTRY_RULES.md`。'
if (-not $instructionMatch.Success -or $instructionMatch.Groups[1].Value -ne $expectedInstruction) {
    Add-ValidationError 'Agent 根指令模板必须严格保持为指向 ENTRY_RULES.md 的一句话'
}

if ($errors.Count -gt 0) {
    foreach ($validationError in $errors) {
        Write-Host "[失败] $validationError" -ForegroundColor Red
    }
    exit 1
}

Write-Host "结构校验通过：$($markdownFiles.Count) 个 Markdown 文件，$($contentFiles.Count) 个知识内容文件。" -ForegroundColor Green
