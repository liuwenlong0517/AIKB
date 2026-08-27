# 在隔离临时目录中验证双 Agent 适配器的配置合法性、UTF-8、幂等安装和精确卸载。
# finally 块负责恢复进程环境并清理已验证位于临时目录下的测试目标。
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$tempBase = [IO.Path]::GetTempPath()
$testRoot = Join-Path $tempBase ('aikb-adapter-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$resolvedTestRoot = (Resolve-Path -LiteralPath $testRoot).Path
if (-not $resolvedTestRoot.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
    throw "适配器测试目录越过系统临时目录：$resolvedTestRoot"
}

$codexHome = Join-Path $resolvedTestRoot 'codex'
$claudeHome = Join-Path $resolvedTestRoot 'claude'
$claudeConfig = Join-Path $resolvedTestRoot 'claude.json'
$previousAikbHome = $env:AIKB_HOME
$previousUserAikbHome = [Environment]::GetEnvironmentVariable('AIKB_HOME', 'User')
$previousKnowledgeHome = $env:AIKB_KNOWLEDGE_HOME
$previousUserKnowledgeHome = [Environment]::GetEnvironmentVariable('AIKB_KNOWLEDGE_HOME', 'User')

function ConvertFrom-McpResponse {
    # 将独立进程可能分段返回的 stdout 合并后再解析 JSON。
    param([object[]]$OutputSegments)

    $json = [string]::Concat([string[]]@($OutputSegments)).Trim()
    if ([string]::IsNullOrWhiteSpace($json)) {
        throw 'MCP initialize 未返回 JSON'
    }
    try {
        return $json | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "MCP initialize 返回无效 JSON（片段数：$(@($OutputSegments).Count)，字符数：$($json.Length)）：$($_.Exception.Message)"
    }
}

function ConvertFrom-HookResponse {
    # 按 Agent 名称附加错误上下文，便于区分两个 hook 入口的失败。
    param([object[]]$OutputSegments, [string]$Agent)

    $json = [string]::Concat([string[]]@($OutputSegments)).Trim()
    if ([string]::IsNullOrWhiteSpace($json)) {
        throw "$Agent hook 未返回 JSON"
    }
    try {
        return $json | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "$Agent hook 返回无效 JSON（字符数：$($json.Length)）：$($_.Exception.Message)"
    }
}

function Invoke-McpInitialize {
    # 用显式 UTF-8 编码和无 shell 进程启动 MCP，模拟真实 stdio 客户端。
    param([object]$Server, [string]$InitializeRequest, [int]$TimeoutMilliseconds = 10000)

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = [string]$Server.command
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardInputEncoding = [Text.UTF8Encoding]::new($false)
    $startInfo.StandardOutputEncoding = [Text.UTF8Encoding]::new($false)
    $startInfo.StandardErrorEncoding = [Text.UTF8Encoding]::new($false)
    foreach ($argument in @($Server.args)) {
        [void]$startInfo.ArgumentList.Add([string]$argument)
    }
    $environmentProperty = $Server.PSObject.Properties['env']
    if ($null -ne $environmentProperty) {
        foreach ($property in $environmentProperty.Value.PSObject.Properties) {
            $startInfo.Environment[[string]$property.Name] = [string]$property.Value
        }
    }

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw 'MCP 进程未能启动'
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.StandardInput.WriteLine($InitializeRequest)
        $process.StandardInput.Close()
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            $process.Kill($true)
            throw "MCP initialize 超时（$TimeoutMilliseconds ms）"
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            throw "MCP 命令退出码为 $($process.ExitCode)：$($stderr.Trim())"
        }
        if (-not [string]::IsNullOrWhiteSpace($stderr)) {
            throw "MCP 命令向 stderr 写入内容：$($stderr.Trim())"
        }
        return ConvertFrom-McpResponse -OutputSegments @($stdout)
    }
    finally {
        $process.Dispose()
    }
}

try {
    # 先验证环境脚本的 Process 级幂等行为，再把临时目录作为两个 Agent 的目标。
    $fragmentedResponse = ConvertFrom-McpResponse -OutputSegments @(
        '{"jsonrpc":"2.0","id":1,"result":{"instructions":"分段',
        '响应"}}'
    )
    if ($fragmentedResponse.result.instructions -ne '分段响应') {
        throw 'MCP 分段 JSON 响应拼接测试失败'
    }

    $environmentScript = Join-Path $repoRoot 'system\tools\set-aikb-home.ps1'
    & $environmentScript -Path $repoRoot -Target Process -PassThru | Out-Null
    $secondEnvironmentWrite = & $environmentScript -Path $repoRoot -Target Process -PassThru
    if ($env:AIKB_HOME -ne $repoRoot) { throw 'Process 级 AIKB_HOME 初始化失败' }
    if ($env:AIKB_KNOWLEDGE_HOME -ne (Join-Path $repoRoot 'content')) { throw 'Process 级 AIKB_KNOWLEDGE_HOME 初始化失败' }
    if ($secondEnvironmentWrite.Changed) { throw 'AIKB_HOME 初始化脚本重复执行不是幂等操作' }
    if ([Environment]::GetEnvironmentVariable('AIKB_HOME', 'User') -ne $previousUserAikbHome) {
        throw 'Process 级环境变量测试意外修改了真实用户环境变量'
    }
    if ([Environment]::GetEnvironmentVariable('AIKB_KNOWLEDGE_HOME', 'User') -ne $previousUserKnowledgeHome) {
        throw 'Process 级知识仓环境变量测试意外修改了真实用户环境变量'
    }
    New-Item -ItemType Directory -Path $codexHome -Force | Out-Null
    New-Item -ItemType Directory -Path $claudeHome -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $codexHome 'config.toml') -Value 'model = "preserve-me"' -Encoding utf8NoBOM
    Set-Content -LiteralPath (Join-Path $codexHome 'hooks.json') -Value '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"preserve-codex-hook"}]}]}}' -Encoding utf8NoBOM
    Set-Content -LiteralPath (Join-Path $claudeHome 'settings.json') -Value '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"preserve-claude-hook"}]}]}}' -Encoding utf8NoBOM
    Set-Content -LiteralPath $claudeConfig -Value '{"mcpServers":{"other":{"type":"stdio","command":"other.exe"}}}' -Encoding utf8NoBOM
    & (Join-Path $repoRoot 'system\adapters\install-all.ps1') -CodexHome $codexHome -ClaudeHome $claudeHome -ClaudeUserConfig $claudeConfig | Out-Null
    $codexToml = Get-Content -Raw -LiteralPath (Join-Path $codexHome 'config.toml')
    if ($codexToml -notmatch '\[mcp_servers\.aikb\]' -or $codexToml -notmatch 'AIKB_HOME' -or $codexToml -notmatch 'AIKB_KNOWLEDGE_HOME') {
        throw 'Codex MCP 配置缺失或未通过双根环境变量解析路径'
    }
    if ($codexToml -notmatch 'serve --agent codex') { throw 'Codex MCP 未显式传递审计 Agent 身份' }
    & python -c "import sys,tomllib; tomllib.load(open(sys.argv[1], 'rb'))" (Join-Path $codexHome 'config.toml')
    if ($LASTEXITCODE -ne 0) { throw 'Codex MCP TOML 配置无法解析' }
    foreach ($path in @((Join-Path $codexHome 'hooks.json'), (Join-Path $claudeHome 'settings.json'), $claudeConfig)) {
        Get-Content -Raw -LiteralPath $path | ConvertFrom-Json | Out-Null
    }
    if ($codexToml -notmatch 'preserve-me') { throw 'Codex 原有配置未保留' }
    if ((Get-Content -Raw -LiteralPath (Join-Path $codexHome 'hooks.json')) -notmatch 'preserve-codex-hook') { throw 'Codex 原有 hook 未保留' }
    if ((Get-Content -Raw -LiteralPath (Join-Path $claudeHome 'settings.json')) -notmatch 'preserve-claude-hook') { throw 'Claude 原有 hook 未保留' }
    if ((Get-Content -Raw -LiteralPath $claudeConfig) -notmatch '"other"') { throw 'Claude 原有 MCP 未保留' }
    foreach ($path in @((Join-Path $codexHome 'config.toml'), (Join-Path $codexHome 'hooks.json'), (Join-Path $claudeHome 'settings.json'), $claudeConfig)) {
        $configuredText = Get-Content -Raw -LiteralPath $path
        if ($configuredText.Contains($repoRoot) -or $configuredText.Contains($repoRoot.Replace('\', '/'))) {
            throw "Agent 配置仍强绑定仓库绝对路径：$path"
        }
        if ($configuredText -notmatch 'AIKB_HOME') {
            throw "Agent 配置未引用 AIKB_HOME：$path"
        }
    }

    $claudeObject = Get-Content -Raw -LiteralPath $claudeConfig | ConvertFrom-Json
    # 从生成配置实际启动 MCP，避免只验证文本而漏掉命令行或环境传递错误。
    $server = $claudeObject.mcpServers.aikb
    if (($server.args -join ' ') -notmatch 'serve --agent claude-code') {
        throw 'Claude Code MCP 未显式传递审计 Agent 身份'
    }
    $initialize = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"adapter-test","version":"1"}}}'
    $mcpResponse = Invoke-McpInitialize -Server $server -InitializeRequest $initialize
    if ($mcpResponse.result.serverInfo.name -ne 'aikb') {
        throw '通过 AIKB_HOME 生成的 MCP 命令无法实际启动服务'
    }
    $claudeSettings = Get-Content -Raw -LiteralPath (Join-Path $claudeHome 'settings.json') | ConvertFrom-Json
    $claudeSessionHook = @($claudeSettings.hooks.SessionStart)[-1].hooks[0]
    if ($claudeSessionHook.shell -ne 'powershell') {
        throw 'Claude Code hook 未显式使用 PowerShell shell'
    }
    $claudeSessionGroup = @($claudeSettings.hooks.SessionStart)[-1]
    if ($claudeSessionGroup.matcher -notmatch '(^|\|)clear(\||$)') {
        throw 'Claude Code SessionStart hook 未覆盖 clear 生命周期'
    }
    if ($claudeSessionHook.command -match '^pwsh\s' -or $claudeSessionHook.command -notmatch '\$env:AIKB_HOME') {
        throw 'Claude Code hook 未使用原生 PowerShell 环境变量命令'
    }
    $hookResponse = '{}' | & pwsh -NoProfile -ExecutionPolicy Bypass -Command $claudeSessionHook.command | ConvertFrom-Json
    if ($null -eq $hookResponse) {
        throw 'Claude Code 生成的 PowerShell hook 无法实际启动'
    }
    $auditLines = Get-ChildItem -LiteralPath (Join-Path $repoRoot 'workspace\audit\events') -Filter '*.jsonl' -Recurse -ErrorAction SilentlyContinue |
        ForEach-Object { Get-Content -LiteralPath $_.FullName }
    $auditEvents = @($auditLines | ForEach-Object { $_ | ConvertFrom-Json })
    if (-not ($auditEvents | Where-Object { $_.agent -eq 'claude-code' -and $_.operation -eq 'initialize' })) {
        throw 'Claude Code MCP initialize 未记录适配器身份'
    }
    if (-not ($auditEvents | Where-Object { $_.agent -eq 'claude-code' -and $_.operation -eq 'session-start' -and $_.outcome_code -eq 'invalid_project' })) {
        throw 'Claude Code hook 未记录 invalid_project 审计结果'
    }

    # 过期的控制仓变量同样必须 fail-open；该路径在 Python 启动前就会触发 PowerShell 的路径解析。
    $previousInvalidRootAikbHome = $env:AIKB_HOME
    $previousInvalidRootKnowledgeHome = $env:AIKB_KNOWLEDGE_HOME
    $invalidRootExitCode = $null
    try {
        $env:AIKB_HOME = Join-Path $resolvedTestRoot 'missing-control-root'
        $env:AIKB_KNOWLEDGE_HOME = Join-Path $resolvedTestRoot 'missing-knowledge-root'
        '{}' | & pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repoRoot 'system\adapters\shared\aikb-hook.ps1') -Agent adapter-test -Event stop | Out-Null
        $invalidRootExitCode = $LASTEXITCODE
    }
    finally {
        $env:AIKB_HOME = $previousInvalidRootAikbHome
        $env:AIKB_KNOWLEDGE_HOME = $previousInvalidRootKnowledgeHome
    }
    if ($invalidRootExitCode -ne 0) { throw '无效 AIKB_HOME 时 hook wrapper 未保持 fail-open' }

    # 在独立 PowerShell 进程中隐藏 Python，验证 wrapper 仍 fail-open 并写入独立 fallback JSON。
    $fallbackRoot = Join-Path $repoRoot 'workspace\audit\fallback'
    $fallbackBefore = @(Get-ChildItem -LiteralPath $fallbackRoot -Filter '*.json' -Recurse -ErrorAction SilentlyContinue).Count
    $pwshExecutable = (Get-Command pwsh -ErrorAction Stop).Source
    $savedPath = $env:PATH
    try {
        $env:PATH = ''
        '{}' | & $pwshExecutable -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repoRoot 'system\adapters\shared\aikb-hook.ps1') -Agent adapter-test -Event stop | Out-Null
    }
    finally {
        $env:PATH = $savedPath
    }
    if ($LASTEXITCODE -ne 0) { throw 'Python 缺失时 hook wrapper 未保持 fail-open' }
    $fallbackAfter = @(Get-ChildItem -LiteralPath $fallbackRoot -Filter '*.json' -Recurse -ErrorAction SilentlyContinue).Count
    if ($fallbackAfter -le $fallbackBefore) { throw 'Python 缺失时 hook wrapper 未写入 fallback 审计' }

    $codexSettings = Get-Content -Raw -LiteralPath (Join-Path $codexHome 'hooks.json') | ConvertFrom-Json
    $codexSessionHook = @($codexSettings.hooks.SessionStart)[-1].hooks[0]
    $encodingRepo = Join-Path $resolvedTestRoot '中文AIKB'
    $encodingProject = Join-Path $encodingRepo '中文项目'
    $encodingToolRoot = Join-Path $encodingRepo 'system\tools\aikb-mcp'
    $previousEncodingAikbHome = $env:AIKB_HOME
    $previousEncodingKnowledgeHome = $env:AIKB_KNOWLEDGE_HOME
    $previousPythonUtf8 = $env:PYTHONUTF8
    $previousPythonIoEncoding = $env:PYTHONIOENCODING
    try {
        # 复制最小双仓结构到中文路径，专门验证 PowerShell 与 Python 的 UTF-8 往返。
        New-Item -ItemType Directory -Path (Join-Path $encodingRepo 'content') -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $encodingRepo 'system\tools') -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $encodingRepo 'system\adapters\shared') -Force | Out-Null
        New-Item -ItemType Directory -Path $encodingProject -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $repoRoot 'ENTRY_RULES.md') -Destination (Join-Path $encodingRepo 'ENTRY_RULES.md')
        Copy-Item -LiteralPath (Join-Path $repoRoot 'content\.aikb-knowledge.json') -Destination (Join-Path $encodingRepo 'content\.aikb-knowledge.json')
        Copy-Item -LiteralPath (Join-Path $repoRoot 'system\tools\aikb-mcp') -Destination (Join-Path $encodingRepo 'system\tools') -Recurse
        Copy-Item -LiteralPath (Join-Path $repoRoot 'system\adapters\shared\aikb-hook.ps1') -Destination (Join-Path $encodingRepo 'system\adapters\shared\aikb-hook.ps1')

        $env:AIKB_HOME = $encodingRepo
        $env:AIKB_KNOWLEDGE_HOME = Join-Path $encodingRepo 'content'
        Push-Location -LiteralPath $encodingToolRoot
        try {
            $checkpointCode = "import sys; from aikb.config import Settings; from aikb.workstate import WorkStateStore; WorkStateStore(Settings.load()).checkpoint({'project_path': sys.argv[1], 'goal': sys.argv[2], 'agent': 'adapter-test', 'session_id': 'utf8'})"
            & python -c $checkpointCode $encodingProject '编码边界验证'
            if ($LASTEXITCODE -ne 0) { throw '无法建立 UTF-8 hook 测试状态' }
        }
        finally {
            Pop-Location
        }

        # 主动模拟曾把 Python GBK 输出转换为 Unicode 替换字符的冲突 Windows 默认值。
        $env:PYTHONUTF8 = '0'
        $env:PYTHONIOENCODING = 'cp936'
        $unicodePayload = @{ cwd = $encodingProject; prompt = '中文输入' } | ConvertTo-Json -Compress
        foreach ($case in @(
            @{ Agent = 'Codex'; Shell = 'pwsh'; Command = $codexSessionHook.command },
            @{ Agent = 'Claude Code'; Shell = 'powershell.exe'; Command = $claudeSessionHook.command }
        )) {
            $outputSegments = $unicodePayload | & $case.Shell -NoProfile -ExecutionPolicy Bypass -Command $case.Command
            if ($LASTEXITCODE -ne 0) { throw "$($case.Agent) UTF-8 hook 执行失败" }
            $response = ConvertFrom-HookResponse -OutputSegments @($outputSegments) -Agent $case.Agent
            $context = [string]$response.hookSpecificOutput.additionalContext
            if ($context -notmatch 'AIKB 发现一个本机活动任务' -or $context -notmatch '编码边界验证') {
                throw "$($case.Agent) hook 中文反馈未完整往返"
            }
            if ($context.Contains([char]0xFFFD)) {
                throw "$($case.Agent) hook 中文反馈包含 Unicode 替换字符"
            }
        }
    }
    finally {
        $env:AIKB_HOME = $previousEncodingAikbHome
        $env:AIKB_KNOWLEDGE_HOME = $previousEncodingKnowledgeHome
        $env:PYTHONUTF8 = $previousPythonUtf8
        $env:PYTHONIOENCODING = $previousPythonIoEncoding
    }

    $before = (Get-FileHash -LiteralPath (Join-Path $codexHome 'hooks.json')).Hash
    # 再次安装后文件哈希必须不变，卸载则只应删除 AIKB 受管内容。
    & (Join-Path $repoRoot 'system\adapters\install-all.ps1') -CodexHome $codexHome -ClaudeHome $claudeHome -ClaudeUserConfig $claudeConfig | Out-Null
    $after = (Get-FileHash -LiteralPath (Join-Path $codexHome 'hooks.json')).Hash
    if ($before -ne $after) { throw '适配器重复安装不是幂等操作' }

    & (Join-Path $repoRoot 'system\adapters\uninstall-all.ps1') -CodexHome $codexHome -ClaudeHome $claudeHome -ClaudeUserConfig $claudeConfig | Out-Null
    if ((Get-Content -Raw -LiteralPath (Join-Path $codexHome 'config.toml')) -match 'mcp_servers\.aikb') { throw 'Codex MCP 卸载不完整' }
    if ((Get-Content -Raw -LiteralPath (Join-Path $codexHome 'hooks.json')) -match 'aikb-hook\.ps1') { throw 'Codex hooks 卸载不完整' }
    if ((Get-Content -Raw -LiteralPath $claudeConfig) -match '"aikb"') { throw 'Claude MCP 卸载不完整' }
    if ((Get-Content -Raw -LiteralPath (Join-Path $claudeHome 'settings.json')) -match 'aikb-hook\.ps1') { throw 'Claude hooks 卸载不完整' }

    Write-Host '适配器校验通过：Codex/Claude Code 安装幂等、配置合法、卸载范围准确。' -ForegroundColor Green
}
finally {
    $env:AIKB_HOME = $previousAikbHome
    $env:AIKB_KNOWLEDGE_HOME = $previousKnowledgeHome
    if ((Test-Path -LiteralPath $resolvedTestRoot) -and $resolvedTestRoot.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
        for ($attempt = 1; $attempt -le 5; $attempt++) {
            try {
                Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force -ErrorAction Stop
                break
            }
            catch {
                if ($attempt -eq 5) { throw }
                Start-Sleep -Milliseconds 200
            }
        }
    }
}
