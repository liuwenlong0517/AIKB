# 共享适配器配置模块：负责 JSON/TOML 受管区块合并、备份、原子写入和精确卸载。
# 公共函数只导出安装/卸载编排入口，其余函数保持模块内部实现细节。
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:CodexMarkerStart = '# >>> AIKB managed MCP >>>'
$script:CodexMarkerEnd = '# <<< AIKB managed MCP <<<'
$script:HookScriptName = 'aikb-hook.ps1'

function Add-ObjectProperty {
    # 对 PSCustomObject 执行新增或覆盖，统一处理 ConvertFrom-Json 的动态属性。
    param([object]$Object, [string]$Name, [object]$Value)
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
    else {
        $property.Value = $Value
    }
}

function Read-JsonObject {
    # 缺失或空文件按空对象处理；已有内容必须是合法 JSON 才能继续修改。
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{}
    }
    $text = Get-Content -Raw -LiteralPath $Path
    if ([string]::IsNullOrWhiteSpace($text)) {
        return [pscustomobject]@{}
    }
    return $text | ConvertFrom-Json
}

function Backup-ConfigFile {
    # 只在首次修改前创建一次备份，重复安装不会覆盖用户的原始快照。
    param([string]$Path)
    if ((Test-Path -LiteralPath $Path -PathType Leaf) -and -not (Test-Path -LiteralPath "$Path.aikb-backup")) {
        Copy-Item -LiteralPath $Path -Destination "$Path.aikb-backup"
    }
}

function Write-TextAtomic {
    # 在目标同目录写入 UTF-8 无 BOM 临时文件，再替换目标以避免半写配置。
    param([string]$Path, [string]$Content)
    $directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    Backup-ConfigFile -Path $Path
    $tempPath = Join-Path $directory ((Split-Path -Leaf $Path) + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($tempPath, $Content, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $tempPath -Destination $Path -Force
    }
    finally {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
}

function Write-JsonAtomic {
    # 使用固定深度序列化 JSON，并复用文本原子写入和备份策略。
    param([string]$Path, [object]$Value)
    $json = $Value | ConvertTo-Json -Depth 30
    Write-TextAtomic -Path $Path -Content ($json + [Environment]::NewLine)
}

function Remove-AikbHookHandlers {
    # 删除命令中引用 aikb-hook.ps1 的组，同时保留同组内的非 AIKB handlers。
    param([object]$Hooks, [string]$Event)
    $property = $Hooks.PSObject.Properties[$Event]
    if ($null -eq $property) {
        return
    }
    $keptGroups = [System.Collections.Generic.List[object]]::new()
    foreach ($group in @($property.Value)) {
        if ($null -eq $group) { continue }
        $handlersProperty = $group.PSObject.Properties['hooks']
        if ($null -eq $handlersProperty) {
            $keptGroups.Add($group)
            continue
        }
        $keptHandlers = @($handlersProperty.Value | Where-Object {
            $commandProperty = $_.PSObject.Properties['command']
            $null -eq $commandProperty -or [string]$commandProperty.Value -notmatch [regex]::Escape($script:HookScriptName)
        })
        if ($keptHandlers.Count -gt 0) {
            $handlersProperty.Value = [object[]]$keptHandlers
            $keptGroups.Add($group)
        }
    }
    $property.Value = [object[]]$keptGroups.ToArray()
}

function Add-AikbHookHandler {
    # 先清理同事件旧的 AIKB handler，再追加唯一受管组，保证安装幂等。
    param(
        [object]$Hooks,
        [string]$Event,
        [string]$Command,
        [string]$Matcher = '',
        [int]$Timeout = 10,
        [string]$Shell = ''
    )
    Remove-AikbHookHandlers -Hooks $Hooks -Event $Event
    $handler = [pscustomobject]@{ type = 'command'; command = $Command; timeout = $Timeout }
    if ($Shell) {
        Add-ObjectProperty -Object $handler -Name 'shell' -Value $Shell
    }
    $group = if ($Matcher) {
        [pscustomobject]@{ matcher = $Matcher; hooks = [object[]]@($handler) }
    }
    else {
        [pscustomobject]@{ hooks = [object[]]@($handler) }
    }
    $property = $Hooks.PSObject.Properties[$Event]
    $groups = [System.Collections.Generic.List[object]]::new()
    if ($null -ne $property) {
        foreach ($existingGroup in @($property.Value)) { $groups.Add($existingGroup) }
    }
    $groups.Add($group)
    Add-ObjectProperty -Object $Hooks -Name $Event -Value ([object[]]$groups.ToArray())
}

function Update-HooksJson {
    # 根据 Agent 生成对应 shell 语法的生命周期 hooks，并保留用户其他 hooks。
    param([string]$Path, [string]$Agent)
    $config = Read-JsonObject -Path $Path
    $hooksProperty = $config.PSObject.Properties['hooks']
    if ($null -eq $hooksProperty) {
        $hooks = [pscustomobject]@{}
        Add-ObjectProperty -Object $config -Name 'hooks' -Value $hooks
    }
    else {
        $hooks = $hooksProperty.Value
    }
    $hookCommand = "& (Join-Path `$env:AIKB_HOME 'system/adapters/shared/aikb-hook.ps1') -Agent $Agent -Event"
    if ($Agent -eq 'claude-code') {
        Add-AikbHookHandler -Hooks $hooks -Event 'SessionStart' -Matcher 'startup|resume|clear|compact' -Command "$hookCommand session-start" -Timeout 10 -Shell 'powershell'
        Add-AikbHookHandler -Hooks $hooks -Event 'PreCompact' -Matcher 'manual|auto' -Command "$hookCommand pre-compact" -Timeout 10 -Shell 'powershell'
        Add-AikbHookHandler -Hooks $hooks -Event 'Stop' -Command "$hookCommand stop" -Timeout 10 -Shell 'powershell'
        Add-AikbHookHandler -Hooks $hooks -Event 'SessionEnd' -Command "$hookCommand session-end" -Timeout 3 -Shell 'powershell'
    }
    else {
        $base = "pwsh -NoProfile -ExecutionPolicy Bypass -Command `"$hookCommand"
        Add-AikbHookHandler -Hooks $hooks -Event 'SessionStart' -Matcher 'startup|resume|compact' -Command "$base session-start`"" -Timeout 10
        Add-AikbHookHandler -Hooks $hooks -Event 'PreCompact' -Matcher 'manual|auto' -Command "$base pre-compact`"" -Timeout 10
        Add-AikbHookHandler -Hooks $hooks -Event 'Stop' -Command "$base stop`"" -Timeout 10
        Add-AikbHookHandler -Hooks $hooks -Event 'SessionEnd' -Command "$base session-end`"" -Timeout 3
    }
    Write-JsonAtomic -Path $Path -Value $config
}

function Remove-HooksJson {
    # 遍历所有生命周期事件，只移除 AIKB hook 命令并保留配置骨架。
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    $config = Read-JsonObject -Path $Path
    $hooksProperty = $config.PSObject.Properties['hooks']
    if ($null -eq $hooksProperty) { return }
    foreach ($event in @('SessionStart', 'PreCompact', 'Stop', 'SessionEnd')) {
        Remove-AikbHookHandlers -Hooks $hooksProperty.Value -Event $event
    }
    Write-JsonAtomic -Path $Path -Value $config
}

function Update-CodexMcp {
    # 替换 Codex 受管 TOML 区块；发现同名非受管服务时拒绝覆盖。
    param([string]$Path)
    $existing = if (Test-Path -LiteralPath $Path) { Get-Content -Raw -LiteralPath $Path } else { '' }
    if ($existing -match '(?m)^\s*\[mcp_servers\.aikb\]\s*$' -and $existing -notmatch [regex]::Escape($script:CodexMarkerStart)) {
        throw "Codex 已存在非 AIKB 安装器管理的 mcp_servers.aikb：$Path"
    }
    $pattern = '(?ms)^' + [regex]::Escape($script:CodexMarkerStart) + '.*?^' + [regex]::Escape($script:CodexMarkerEnd) + '\r?\n?'
    # 配置仅保存环境变量引用，避免把当前机器的控制仓绝对路径写入用户文件。
    $clean = [regex]::Replace($existing, $pattern, '').TrimEnd()
    $launcherCommand = "& (Join-Path `$env:AIKB_HOME 'system/tools/aikb-mcp/scripts/aikb.ps1') serve --agent codex"
    $tomlCommand = $launcherCommand.Replace('\', '\\').Replace('"', '\"')
    $block = @"
$script:CodexMarkerStart
[mcp_servers.aikb]
command = "pwsh"
args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "$tomlCommand"]
env_vars = ["AIKB_HOME", "AIKB_KNOWLEDGE_HOME"]
startup_timeout_sec = 10
tool_timeout_sec = 60
enabled = true
$script:CodexMarkerEnd
"@
    $output = if ($clean) { $clean + [Environment]::NewLine + [Environment]::NewLine + $block.Trim() + [Environment]::NewLine } else { $block.Trim() + [Environment]::NewLine }
    Write-TextAtomic -Path $Path -Content $output
}

function Remove-CodexMcp {
    # 只删除带 AIKB 起止标记的 Codex MCP 区块。
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    $existing = Get-Content -Raw -LiteralPath $Path
    $pattern = '(?ms)^' + [regex]::Escape($script:CodexMarkerStart) + '.*?^' + [regex]::Escape($script:CodexMarkerEnd) + '\r?\n?'
    $clean = [regex]::Replace($existing, $pattern, '').TrimEnd()
    Write-TextAtomic -Path $Path -Content ($(if ($clean) { $clean + [Environment]::NewLine } else { '' }))
}

function Update-ClaudeMcp {
    # 写入带 AIKB_MANAGED 标记的 Claude MCP 对象，冲突对象不强行覆盖。
    param([string]$Path)
    $config = Read-JsonObject -Path $Path
    $serversProperty = $config.PSObject.Properties['mcpServers']
    if ($null -eq $serversProperty) {
        $servers = [pscustomobject]@{}
        Add-ObjectProperty -Object $config -Name 'mcpServers' -Value $servers
    }
    else {
        $servers = $serversProperty.Value
    }
    $existingAikb = $servers.PSObject.Properties['aikb']
    if ($null -ne $existingAikb) {
        $existingEnv = $existingAikb.Value.PSObject.Properties['env']
        $managedMarker = if ($null -ne $existingEnv) { $existingEnv.Value.PSObject.Properties['AIKB_MANAGED'] } else { $null }
        if ($null -eq $managedMarker -or [string]$managedMarker.Value -ne '1') {
            throw "Claude Code 已存在非 AIKB 安装器管理的 mcpServers.aikb：$Path"
        }
    }
    $launcherCommand = "& (Join-Path `$env:AIKB_HOME 'system/tools/aikb-mcp/scripts/aikb.ps1') serve --agent claude-code"
    $server = [pscustomobject]@{
        type = 'stdio'
        command = 'pwsh'
        args = [object[]]@('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $launcherCommand)
        env = [pscustomobject]@{ AIKB_MANAGED = '1' }
    }
    Add-ObjectProperty -Object $servers -Name 'aikb' -Value $server
    Write-JsonAtomic -Path $Path -Value $config
}

function Remove-ClaudeMcp {
    # 仅当 Claude MCP 对象带 AIKB_MANAGED=1 时才删除 aikb 服务。
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    $config = Read-JsonObject -Path $Path
    $serversProperty = $config.PSObject.Properties['mcpServers']
    if ($null -ne $serversProperty) {
        $aikbProperty = $serversProperty.Value.PSObject.Properties['aikb']
        if ($null -ne $aikbProperty) {
            $envProperty = $aikbProperty.Value.PSObject.Properties['env']
            $managedMarker = if ($null -ne $envProperty) { $envProperty.Value.PSObject.Properties['AIKB_MANAGED'] } else { $null }
            if ($null -ne $managedMarker -and [string]$managedMarker.Value -eq '1') {
                $serversProperty.Value.PSObject.Properties.Remove('aikb')
                Write-JsonAtomic -Path $Path -Value $config
            }
        }
    }
}

function Install-AikbAdapter {
    # 校验双仓环境与目标仓一致后，安装指定 Agent 的 MCP 和 hooks 配置。
    param(
        [ValidateSet('codex', 'claude-code')][string]$Agent,
        [string]$RepoRoot,
        [string]$CodexHome,
        [string]$ClaudeHome,
        [string]$ClaudeUserConfig
    )
    $configuredRoot = $env:AIKB_HOME
    if (-not $configuredRoot) {
        throw '未设置 AIKB_HOME。请先运行 system/tools/set-aikb-home.ps1，再安装 Agent 适配器。'
    }
    $resolvedConfiguredRoot = (Resolve-Path -LiteralPath $configuredRoot).Path
    $resolvedRepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
    if (-not $resolvedConfiguredRoot.Equals($resolvedRepoRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "AIKB_HOME 指向 $resolvedConfiguredRoot，但当前安装仓库是 $resolvedRepoRoot。请先重新运行 set-aikb-home.ps1。"
    }
    $configuredKnowledgeRoot = $env:AIKB_KNOWLEDGE_HOME
    if (-not $configuredKnowledgeRoot -or -not (Test-Path -LiteralPath (Join-Path $configuredKnowledgeRoot '.aikb-knowledge.json') -PathType Leaf)) {
        throw 'AIKB_KNOWLEDGE_HOME 未指向有效知识仓。请先重新运行 set-aikb-home.ps1。'
    }
    if ($Agent -eq 'codex') {
        # Codex 使用 TOML 受管区块，Claude Code 使用 JSON 标记对象。
        Update-CodexMcp -Path (Join-Path $CodexHome 'config.toml')
        Update-HooksJson -Path (Join-Path $CodexHome 'hooks.json') -Agent 'codex'
    }
    else {
        Update-ClaudeMcp -Path $ClaudeUserConfig
        Update-HooksJson -Path (Join-Path $ClaudeHome 'settings.json') -Agent 'claude-code'
    }
}

function Uninstall-AikbAdapter {
    # 根据 Agent 选择对应配置入口，并只清除 AIKB 明确管理的内容。
    param(
        [ValidateSet('codex', 'claude-code')][string]$Agent,
        [string]$CodexHome,
        [string]$ClaudeHome,
        [string]$ClaudeUserConfig
    )
    if ($Agent -eq 'codex') {
        Remove-CodexMcp -Path (Join-Path $CodexHome 'config.toml')
        Remove-HooksJson -Path (Join-Path $CodexHome 'hooks.json')
    }
    else {
        Remove-ClaudeMcp -Path $ClaudeUserConfig
        Remove-HooksJson -Path (Join-Path $ClaudeHome 'settings.json')
    }
}

Export-ModuleMember -Function Install-AikbAdapter, Uninstall-AikbAdapter
