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

$requiredRootDirectories = @('content', 'system', 'workspace')
$allowedSystemEntries = @('README.md', 'adapters', 'rules', 'schemas', 'templates', 'tests', 'tools')
$allowedContentEntries = @('README.md', 'experience', 'knowledge', 'projects', 'workflows')
$allowedWorkspaceEntries = @('.gitignore', 'README.md', 'active', 'archive', 'db', 'runtime')

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
    Where-Object { $_.Name -notin @('.git', 'content', 'system', 'workspace') } |
    ForEach-Object { Add-ValidationError "根目录存在白名单外目录：$($_.Name)" }

Get-ChildItem -LiteralPath (Join-Path $repoRoot 'system') -Force |
    Where-Object { $_.Name -notin $allowedSystemEntries } |
    ForEach-Object { Add-ValidationError "system/ 存在未定义入口：$($_.Name)" }

Get-ChildItem -LiteralPath (Join-Path $repoRoot 'content') -Force |
    Where-Object { $_.Name -notin $allowedContentEntries } |
    ForEach-Object { Add-ValidationError "content/ 存在未定义入口：$($_.Name)" }

Get-ChildItem -LiteralPath (Join-Path $repoRoot 'workspace') -Force |
    Where-Object { $_.Name -notin $allowedWorkspaceEntries } |
    ForEach-Object { Add-ValidationError "workspace/ 存在未定义入口：$($_.Name)" }

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

if ($catalogText -match '\[[^\]]+\]\(workspace/') {
    Add-ValidationError 'CATALOG.md 不应登记 workspace/ 下的工作状态或派生数据库'
}

$entryText = Get-Content -Raw -LiteralPath (Join-Path $repoRoot 'ENTRY_RULES.md')
foreach ($requiredText in @(
    'AIKB_HOME',
    'system/rules/AI_RULES.md',
    'system/rules/USER_RULES.md',
    '用户明确要求跳过时不接入',
    '根目录 `README.md` 是人类维护手册',
    '不属于 Agent 默认上下文'
)) {
    if (-not $entryText.Contains($requiredText)) {
        Add-ValidationError "ENTRY_RULES.md 缺少入口职责：$requiredText"
    }
}

$roleRequirements = @{
    'system/rules/USER_RULES.md' = @('当前任务的明确要求', '只有用户明确要求时才能修改', 'Java', '使用中文')
    'system/rules/AI_RULES.md' = @(
        'ENTRY_RULES.md', 'INDEX.md', 'read_knowledge', 'search_knowledge', 'content_hash',
        'workspace/', 'work_id', 'system/rules/CONTRIBUTING.md', 'CATALOG.md', 'Markdown 是知识事实源',
        '首次接入不默认读取 `INDEX.md`', 'MCP 不可用', '用户要求跳过 AIKB',
        '不在每次任务后自动写库', '必须先确认'
    )
    'INDEX.md' = @(
        'MCP 不可用时', '根目录 `README.md` 是人类维护手册', 'content/knowledge/README.md',
        'content/experience/README.md', 'content/workflows/README.md', 'content/projects/README.md',
        'system/rules/CONTRIBUTING.md', 'CATALOG.md'
    )
    'system/rules/CONTRIBUTING.md' = @(
        'content/experience/inbox/', 'system/templates/', 'system/schemas/knowledge-entry.schema.json',
        'CATALOG.md', 'INDEX.md', '`id`', '`relations`', 'system/tests/validate-structure.ps1',
        '无需逐次确认', '请求用户决定', '不发布未通过校验的正式知识'
    )
}

foreach ($relativePath in $roleRequirements.Keys) {
    $text = Get-Content -Raw -LiteralPath (Join-Path $repoRoot $relativePath)
    foreach ($requiredText in $roleRequirements[$relativePath]) {
        if (-not $text.Contains($requiredText)) {
            Add-ValidationError "$relativePath 缺少职责闭环：$requiredText"
        }
    }
}

$forbiddenByRole = @{
    'ENTRY_RULES.md' = @('search_knowledge', 'read_knowledge', 'work_id', 'CATALOG.md')
    'system/rules/USER_RULES.md' = @('search_knowledge', 'read_knowledge', 'work_id')
    'INDEX.md' = @('work_id', 'session_id', 'relation_limit')
}
foreach ($relativePath in $forbiddenByRole.Keys) {
    $text = Get-Content -Raw -LiteralPath (Join-Path $repoRoot $relativePath)
    foreach ($forbiddenText in $forbiddenByRole[$relativePath]) {
        if ($text.Contains($forbiddenText)) {
            Add-ValidationError "$relativePath 混入其他层职责：$forbiddenText"
        }
    }
}

$portableRuleFiles = @(
    'ENTRY_RULES.md',
    'INDEX.md',
    'system/rules/USER_RULES.md',
    'system/rules/AI_RULES.md',
    'system/rules/CONTRIBUTING.md'
)
foreach ($relativePath in $portableRuleFiles) {
    $text = Get-Content -Raw -LiteralPath (Join-Path $repoRoot $relativePath)
    if ($text -match '[A-Za-z]:\\') {
        Add-ValidationError "$relativePath 不得包含机器绝对路径"
    }
}

$ruleBudgets = @{
    'ENTRY_RULES.md' = 800
    'INDEX.md' = 800
    'system/rules/USER_RULES.md' = 600
    'system/rules/AI_RULES.md' = 2100
    'system/rules/CONTRIBUTING.md' = 3200
}
foreach ($relativePath in $ruleBudgets.Keys) {
    $text = Get-Content -Raw -LiteralPath (Join-Path $repoRoot $relativePath)
    if ($text.Length -gt $ruleBudgets[$relativePath]) {
        Add-ValidationError "核心规则超过字符预算：$relativePath = $($text.Length) > $($ruleBudgets[$relativePath])"
    }
}

$templatePath = Join-Path $repoRoot 'system\templates\agent-root-instruction.md'
$templateText = Get-Content -Raw -LiteralPath $templatePath
$instructionMatch = [regex]::Match($templateText, '(?ms)^```md\r?\n(.+?)\r?\n```')
$expectedInstruction = '每个新会话开始时，请从 Windows 用户环境变量 `AIKB_HOME` 获取 AIKB 根目录，并读取和持续遵循其中的 `ENTRY_RULES.md`。'
if (-not $instructionMatch.Success -or $instructionMatch.Groups[1].Value -ne $expectedInstruction) {
    Add-ValidationError 'Agent 根指令模板必须严格保持为指向 ENTRY_RULES.md 的一句话'
}

$setupPath = Join-Path $repoRoot 'system\tools\setup-aikb.ps1'
if (-not (Test-Path -LiteralPath $setupPath -PathType Leaf)) {
    Add-ValidationError '缺少首次使用一键编排脚本 system/tools/setup-aikb.ps1'
}
else {
    $setupText = Get-Content -Raw -LiteralPath $setupPath
    foreach ($requiredScript in @(
        'set-aikb-home.ps1',
        'validate-structure.ps1',
        'validate-adapters.ps1',
        'install-root-instructions.ps1',
        'install-all.ps1',
        'aikb.ps1',
        'doctor.ps1'
    )) {
        if (-not $setupText.Contains($requiredScript)) {
            Add-ValidationError "一键配置未编排独立脚本：$requiredScript"
        }
    }
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Add-ValidationError '未找到 Python，无法验证知识元数据和适配器实现'
}
else {
    $toolRoot = Join-Path $repoRoot 'system\tools\aikb-mcp'
    Push-Location -LiteralPath $toolRoot
    try {
        $previousUtf8 = $env:PYTHONUTF8
        $env:PYTHONUTF8 = '1'
        $metadataOutput = & $python.Source -m aikb validate 2>&1
        if ($LASTEXITCODE -ne 0) {
            Add-ValidationError "知识元数据验证失败：$($metadataOutput -join ' ')"
        }
    }
    finally {
        $env:PYTHONUTF8 = $previousUtf8
        Pop-Location
    }
}

$adapterIds = @{}
Get-ChildItem -LiteralPath (Join-Path $repoRoot 'system\adapters') -Directory | ForEach-Object {
    $manifestPath = Join-Path $_.FullName 'adapter.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        if ($_.Name -ne 'shared') { Add-ValidationError "适配器目录缺少 adapter.json：$($_.Name)" }
        return
    }
    try {
        $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
        if (-not $manifest.id -or $adapterIds.ContainsKey([string]$manifest.id)) {
            Add-ValidationError "适配器 ID 缺失或重复：$($_.Name)"
        }
        else {
            $adapterIds[[string]$manifest.id] = $true
        }
        foreach ($scriptName in @($manifest.install.script, $manifest.install.uninstallScript, $manifest.install.doctorScript)) {
            if (-not (Test-Path -LiteralPath (Join-Path $_.FullName $scriptName) -PathType Leaf)) {
                Add-ValidationError "适配器入口缺失：$($_.Name)/$scriptName"
            }
        }
    }
    catch {
        Add-ValidationError "适配器清单无效：$manifestPath -> $($_.Exception.Message)"
    }
}

if ($errors.Count -gt 0) {
    foreach ($validationError in $errors) {
        Write-Host "[失败] $validationError" -ForegroundColor Red
    }
    exit 1
}

Write-Host "结构校验通过：$($markdownFiles.Count) 个 Markdown 文件，$($contentFiles.Count) 个知识内容文件，$($adapterIds.Count) 个 Agent 适配器。" -ForegroundColor Green
