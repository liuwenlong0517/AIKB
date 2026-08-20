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

try {
    $environmentScript = Join-Path $repoRoot 'system\tools\set-aikb-home.ps1'
    & $environmentScript -Path $repoRoot -Target Process -PassThru | Out-Null
    $secondEnvironmentWrite = & $environmentScript -Path $repoRoot -Target Process -PassThru
    if ($env:AIKB_HOME -ne $repoRoot) { throw 'Process 级 AIKB_HOME 初始化失败' }
    if ($secondEnvironmentWrite.Changed) { throw 'AIKB_HOME 初始化脚本重复执行不是幂等操作' }
    if ([Environment]::GetEnvironmentVariable('AIKB_HOME', 'User') -ne $previousUserAikbHome) {
        throw 'Process 级环境变量测试意外修改了真实用户环境变量'
    }
    New-Item -ItemType Directory -Path $codexHome -Force | Out-Null
    New-Item -ItemType Directory -Path $claudeHome -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $codexHome 'config.toml') -Value 'model = "preserve-me"' -Encoding utf8NoBOM
    Set-Content -LiteralPath (Join-Path $codexHome 'hooks.json') -Value '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"preserve-codex-hook"}]}]}}' -Encoding utf8NoBOM
    Set-Content -LiteralPath (Join-Path $claudeHome 'settings.json') -Value '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"preserve-claude-hook"}]}]}}' -Encoding utf8NoBOM
    Set-Content -LiteralPath $claudeConfig -Value '{"mcpServers":{"other":{"type":"stdio","command":"other.exe"}}}' -Encoding utf8NoBOM
    & (Join-Path $repoRoot 'system\adapters\install-all.ps1') -CodexHome $codexHome -ClaudeHome $claudeHome -ClaudeUserConfig $claudeConfig | Out-Null
    $codexToml = Get-Content -Raw -LiteralPath (Join-Path $codexHome 'config.toml')
    if ($codexToml -notmatch '\[mcp_servers\.aikb\]' -or $codexToml -notmatch 'AIKB_HOME') {
        throw 'Codex MCP 配置缺失或未通过 AIKB_HOME 解析路径'
    }
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
    $server = $claudeObject.mcpServers.aikb
    $initialize = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"adapter-test","version":"1"}}}'
    $mcpResponse = $initialize | & $server.command @($server.args) | Select-Object -First 1 | ConvertFrom-Json
    if ($mcpResponse.result.serverInfo.name -ne 'aikb') {
        throw '通过 AIKB_HOME 生成的 MCP 命令无法实际启动服务'
    }
    $hookInvoke = "& (Join-Path `$env:AIKB_HOME 'system/adapters/shared/aikb-hook.ps1') -Agent codex -Event session-start"
    $hookResponse = '{}' | & pwsh -NoProfile -ExecutionPolicy Bypass -Command $hookInvoke | ConvertFrom-Json
    if ($null -eq $hookResponse) {
        throw '通过 AIKB_HOME 生成的 hook 命令无法实际启动'
    }

    $before = (Get-FileHash -LiteralPath (Join-Path $codexHome 'hooks.json')).Hash
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
    if ((Test-Path -LiteralPath $resolvedTestRoot) -and $resolvedTestRoot.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
