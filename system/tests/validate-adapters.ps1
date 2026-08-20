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

try {
    New-Item -ItemType Directory -Path $codexHome -Force | Out-Null
    New-Item -ItemType Directory -Path $claudeHome -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $codexHome 'config.toml') -Value 'model = "preserve-me"' -Encoding utf8NoBOM
    Set-Content -LiteralPath (Join-Path $codexHome 'hooks.json') -Value '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"preserve-codex-hook"}]}]}}' -Encoding utf8NoBOM
    Set-Content -LiteralPath (Join-Path $claudeHome 'settings.json') -Value '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"preserve-claude-hook"}]}]}}' -Encoding utf8NoBOM
    Set-Content -LiteralPath $claudeConfig -Value '{"mcpServers":{"other":{"type":"stdio","command":"other.exe"}}}' -Encoding utf8NoBOM
    & (Join-Path $repoRoot 'system\adapters\install-all.ps1') -CodexHome $codexHome -ClaudeHome $claudeHome -ClaudeUserConfig $claudeConfig | Out-Null
    $codexToml = Get-Content -Raw -LiteralPath (Join-Path $codexHome 'config.toml')
    if ($codexToml -notmatch '\[mcp_servers\.aikb\]' -or $codexToml -match 'E:\\CodeSpace') {
        throw 'Codex MCP 配置缺失或 TOML 路径仍包含未转义反斜杠'
    }
    foreach ($path in @((Join-Path $codexHome 'hooks.json'), (Join-Path $claudeHome 'settings.json'), $claudeConfig)) {
        Get-Content -Raw -LiteralPath $path | ConvertFrom-Json | Out-Null
    }
    if ($codexToml -notmatch 'preserve-me') { throw 'Codex 原有配置未保留' }
    if ((Get-Content -Raw -LiteralPath (Join-Path $codexHome 'hooks.json')) -notmatch 'preserve-codex-hook') { throw 'Codex 原有 hook 未保留' }
    if ((Get-Content -Raw -LiteralPath (Join-Path $claudeHome 'settings.json')) -notmatch 'preserve-claude-hook') { throw 'Claude 原有 hook 未保留' }
    if ((Get-Content -Raw -LiteralPath $claudeConfig) -notmatch '"other"') { throw 'Claude 原有 MCP 未保留' }

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
    if ((Test-Path -LiteralPath $resolvedTestRoot) -and $resolvedTestRoot.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
