# 校验控制仓、独立知识仓和 workspace/ 的入口白名单、链接、规则预算及索引契约。
# 该脚本只报告结构问题，不修改被校验仓库。
param(
    [string]$KnowledgePath,
    # Web 受控执行器传入服务端已解析的绝对 Python；手工/CI 不传时保持 PATH 查找。
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
# 显式固定输出编码为 UTF-8，避免重定向/非控制台环境下 GBK 字节被按 UTF-8 解码成乱码。
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)

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
$allowedSystemEntries = @('README.md', 'COMMANDS.md', 'adapters', 'rules', 'schemas', 'templates', 'tests', 'tools')
$allowedContentEntries = @('.aikb-knowledge.json', '.gitattributes', '.git', '.gitignore', 'CATALOG.md', 'INDEX.md', 'README.md', 'experience', 'knowledge', 'projects', 'workflows')
$allowedWorkspaceEntries = @('.gitignore', 'README.md', 'active', 'archive', 'audit', 'db', 'runtime')

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

$rootIgnore = Get-Content -Raw -LiteralPath (Join-Path $repoRoot '.gitignore')
$workspaceIgnore = Get-Content -Raw -LiteralPath (Join-Path $repoRoot 'workspace\.gitignore')
if ($rootIgnore -notmatch '(?m)^workspace/audit/$') {
    Add-ValidationError '根 .gitignore 未排除 workspace/audit/'
}
if ($workspaceIgnore -notmatch '(?m)^audit/$') {
    Add-ValidationError 'workspace/.gitignore 未排除 audit/'
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

$generatedDirectoryPattern = '[\\/](?:\.git|node_modules|dist|\.vite|\.venv|__pycache__)[\\/]'
$markdownFiles = @(
    # 控制仓允许包含可重建的前端依赖和构建产物；它们不是 AIKB 文档事实源，
    # 不能把第三方包 README 的内部链接纳入本仓结构校验。
    Get-ChildItem -LiteralPath $repoRoot -Recurse -File -Filter '*.md' |
        Where-Object { $_.FullName -notmatch $generatedDirectoryPattern }
    # 知识仓只跳过自身 Git 元数据，正式知识目录仍保持完整检查。
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

foreach ($file in $contentFiles | Where-Object { $_.Name -ne 'README.md' }) {
    $localReadmePath = Join-Path $file.DirectoryName 'README.md'
    if (-not (Test-Path -LiteralPath $localReadmePath -PathType Leaf)) {
        $relativePath = [IO.Path]::GetRelativePath($knowledgeRoot, $file.FullName).Replace('\', '/')
        Add-ValidationError "知识文件所在目录缺少局部 README.md：$relativePath"
        continue
    }
    $localReadmeText = Get-Content -Raw -LiteralPath $localReadmePath
    $fileNamePattern = [regex]::Escape($file.Name)
    # 只接受指向该文件名的 Markdown 链接，避免 README 仅在普通文字中提到文件而不能导航。
    if ($localReadmeText -notmatch "\]\((?:[^)]*/)?$fileNamePattern(?:#[^)]*)?\)") {
        $relativePath = [IO.Path]::GetRelativePath($knowledgeRoot, $file.FullName).Replace('\', '/')
        $relativeReadme = [IO.Path]::GetRelativePath($knowledgeRoot, $localReadmePath).Replace('\', '/')
        Add-ValidationError "局部 README.md 未登记知识文件：$relativeReadme -> $relativePath"
    }
}

if ($catalogText -match '\[[^\]]+\]\(system/') {
    Add-ValidationError 'CATALOG.md 不应登记 system/ 下的规则、模板或测试文件'
}

if ($catalogText -match '\[[^\]]+\]\(workspace/') {
    Add-ValidationError 'CATALOG.md 不应登记 workspace/ 下的工作状态或派生数据库'
}

$knowledgeIndexText = Get-Content -Raw -LiteralPath (Join-Path $knowledgeRoot 'INDEX.md')
foreach ($requiredText in @('knowledge/README.md', 'experience/README.md', 'workflows/README.md', 'projects/README.md', 'CATALOG.md')) {
    if (-not $knowledgeIndexText.Contains($requiredText)) {
        Add-ValidationError "知识仓 INDEX.md 缺少稳定入口：$requiredText"
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

$pythonSource = $null
if ($PythonPath) {
    try {
        $resolvedPythonPath = (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path
        if (-not (Test-Path -LiteralPath $resolvedPythonPath -PathType Leaf)) {
            throw 'PythonPath 不是文件'
        }
        $pythonSource = $resolvedPythonPath
    }
    catch {
        Add-ValidationError '指定的 PythonPath 无法解析为文件'
    }
}
else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        $pythonSource = $python.Source
    }
}
if (-not $pythonSource) {
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
        $metadataOutput = & $pythonSource -m aikb validate 2>&1
        if ($LASTEXITCODE -ne 0) {
            Add-ValidationError "知识元数据验证失败：$($metadataOutput -join ' ')"
        }
        # 职责词、禁止词、绝对路径和规则预算由 Python 共享验证器统一维护，
        # 结构脚本只负责调用和汇总，避免 PowerShell 与 Web 预览出现两套事实源。
        $ruleOutput = & $pythonSource -m aikb --repo-root $repoRoot validate-rules 2>&1
        if ($LASTEXITCODE -ne 0) {
            try {
                $ruleReport = ($ruleOutput -join "`n") | ConvertFrom-Json
                foreach ($rule in $ruleReport.rules) {
                    foreach ($ruleError in $rule.errors) {
                        Add-ValidationError ([string]$ruleError)
                    }
                }
            }
            catch {
                Add-ValidationError "共享规则校验失败：$($ruleOutput -join ' ')"
            }
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
