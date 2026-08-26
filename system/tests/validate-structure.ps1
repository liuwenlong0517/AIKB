# 校验控制仓、独立知识仓和 workspace/ 的入口白名单、链接、规则预算及索引契约。
# 该脚本只报告结构问题，不修改被校验仓库。
param(
    [string]$KnowledgePath
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$knowledgeRoot = if ($KnowledgePath) {
    (Resolve-Path -LiteralPath $KnowledgePath).Path
}
elseif ($env:AIKB_KNOWLEDGE_HOME) {
    (Resolve-Path -LiteralPath $env:AIKB_KNOWLEDGE_HOME).Path
}
else {
    (Resolve-Path -LiteralPath (Join-Path $repoRoot 'content')).Path
}
$errors = [System.Collections.Generic.List[string]]::new()

function Add-ValidationError {
    # 统一收集错误，脚本末尾一次性输出，便于 CI 或人工修复全部问题。
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

$requiredRootDirectories = @('system', 'workspace')
# 这些白名单体现控制面与知识面、运行面之间的职责边界。
$allowedSystemEntries = @('README.md', 'adapters', 'rules', 'schemas', 'templates', 'tests', 'tools')
$allowedContentEntries = @('.aikb-knowledge.json', '.gitattributes', '.git', '.gitignore', 'CATALOG.md', 'INDEX.md', 'README.md', 'experience', 'knowledge', 'projects', 'workflows')
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

Get-ChildItem -LiteralPath $knowledgeRoot -Force |
    Where-Object { $_.Name -notin $allowedContentEntries } |
    ForEach-Object { Add-ValidationError "知识仓存在未定义入口：$($_.Name)" }

$knowledgeManifestPath = Join-Path $knowledgeRoot '.aikb-knowledge.json'
if (-not (Test-Path -LiteralPath $knowledgeManifestPath -PathType Leaf)) {
    Add-ValidationError '知识仓缺少 .aikb-knowledge.json'
}
else {
    try {
        $knowledgeManifest = Get-Content -Raw -LiteralPath $knowledgeManifestPath | ConvertFrom-Json
        if ($knowledgeManifest.kind -ne 'aikb-knowledge' -or [int]$knowledgeManifest.contract_version -ne 1) {
            Add-ValidationError '知识仓契约标记无效：kind 必须为 aikb-knowledge，contract_version 必须为 1'
        }
    }
    catch {
        Add-ValidationError "知识仓契约标记不是合法 JSON：$($_.Exception.Message)"
    }
}

Get-ChildItem -LiteralPath (Join-Path $repoRoot 'workspace') -Force |
    Where-Object { $_.Name -notin $allowedWorkspaceEntries } |
    ForEach-Object { Add-ValidationError "workspace/ 存在未定义入口：$($_.Name)" }

$markdownFiles = @(
    # 同时检查控制仓和知识仓，但跳过各自 Git 元数据目录。
    Get-ChildItem -LiteralPath $repoRoot -Recurse -File -Filter '*.md' |
        Where-Object { $_.FullName -notmatch '[\\/]\.git[\\/]' }
    Get-ChildItem -LiteralPath $knowledgeRoot -Recurse -File -Filter '*.md' |
        Where-Object { $_.FullName -notmatch '[\\/]\.git[\\/]' }
) | Sort-Object -Property FullName -Unique

foreach ($file in $markdownFiles) {
    $text = Get-Content -Raw -LiteralPath $file.FullName
    $links = [regex]::Matches($text, '\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)')
    foreach ($link in $links) {
        # 外部 URL 和页内锚点不属于本地路径校验范围。
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

$catalogPath = Join-Path $knowledgeRoot 'CATALOG.md'
$catalogText = Get-Content -Raw -LiteralPath $catalogPath
$contentRoot = $knowledgeRoot
$contentFiles = Get-ChildItem -LiteralPath $contentRoot -Recurse -File -Filter '*.md' |
    Where-Object {
        $_.FullName -notmatch '[\\/]\.git[\\/]' -and
        $_.FullName -notin @((Join-Path $contentRoot 'CATALOG.md'), (Join-Path $contentRoot 'INDEX.md'))
    }

foreach ($file in $contentFiles) {
    $relativePath = [IO.Path]::GetRelativePath($knowledgeRoot, $file.FullName).Replace('\', '/')
    if ($catalogText -notmatch [regex]::Escape($relativePath)) {
        Add-ValidationError "知识仓 CATALOG.md 未登记内容文件：$relativePath"
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
    'system/rules/USER_RULES.md' = @(
        '当前任务的明确要求', '只有用户明确要求时才能修改', 'Java', '使用中文',
        '用户指令或前提可能有误', '未知或无法确认', '删除任何文件或目录前',
        'Agent 专属资料', '`AGENTS.md`', '类注释', '方法注释', '关键代码注释'
    )
    'system/rules/AI_RULES.md' = @(
        'ENTRY_RULES.md', 'INDEX.md', 'read_knowledge', 'search_knowledge', 'content_hash',
        'workspace/', 'work_id', 'system/rules/CONTRIBUTING.md', 'CATALOG.md', 'Markdown 是知识事实源',
        '首次接入不默认读取控制仓或知识仓的 `INDEX.md`', 'MCP 不可用', '用户要求跳过 AIKB',
        '不在每次任务后自动写库', '必须先确认'
    )
    'INDEX.md' = @(
        '稳定降级入口', '根目录 `README.md` 是人类维护手册', 'AIKB_KNOWLEDGE_HOME',
        '%AIKB_HOME%\content', 'system/rules/CONTRIBUTING.md', '知识仓 `CATALOG.md`'
    )
    'system/rules/CONTRIBUTING.md' = @(
        'content/experience/inbox/', 'system/templates/', 'system/schemas/knowledge-entry.schema.json',
        'CATALOG.md', 'INDEX.md', '`id`', '`relations`', 'system/tests/validate-structure.ps1',
        '无需逐次确认', '请求用户决定', '不发布未通过校验的正式知识'
    )
}

$knowledgeIndexText = Get-Content -Raw -LiteralPath (Join-Path $knowledgeRoot 'INDEX.md')
foreach ($requiredText in @('knowledge/README.md', 'experience/README.md', 'workflows/README.md', 'projects/README.md', 'CATALOG.md')) {
    if (-not $knowledgeIndexText.Contains($requiredText)) {
        Add-ValidationError "知识仓 INDEX.md 缺少稳定入口：$requiredText"
    }
}

foreach ($relativePath in $roleRequirements.Keys) {
    # 规则文本的职责词和禁止词共同防止层级边界逐渐漂移。
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
    'system/rules/USER_RULES.md' = 800
    'system/rules/AI_RULES.md' = 2100
    'system/rules/CONTRIBUTING.md' = 3200
}
# 核心入口需要保持短小，避免每个新会话加载过多控制面上下文。
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
        $previousAikbHome = $env:AIKB_HOME
        $previousKnowledgeHome = $env:AIKB_KNOWLEDGE_HOME
        $env:AIKB_HOME = $repoRoot
        $env:AIKB_KNOWLEDGE_HOME = $knowledgeRoot
        $metadataOutput = & $python.Source -m aikb validate 2>&1
        if ($LASTEXITCODE -ne 0) {
            Add-ValidationError "知识元数据验证失败：$($metadataOutput -join ' ')"
        }
    }
    finally {
        $env:PYTHONUTF8 = $previousUtf8
        $env:AIKB_HOME = $previousAikbHome
        $env:AIKB_KNOWLEDGE_HOME = $previousKnowledgeHome
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
