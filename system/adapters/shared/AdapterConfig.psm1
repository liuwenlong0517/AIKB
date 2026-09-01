# 共享适配器配置模块：负责 JSON/TOML 受管区块合并、备份、原子写入和精确卸载。
# 公共函数只导出安装/卸载编排入口，其余函数保持模块内部实现细节。
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:CodexMarkerStart = '# >>> AIKB managed MCP >>>'
$script:CodexMarkerEnd = '# <<< AIKB managed MCP <<<'
$script:HookScriptName = 'aikb-hook.ps1'

function New-JsonObject {
    # 使用有序字典作为新建 JSON 对象，既保持原有字段顺序，也允许后续按原始键名精确更新。
    return [ordered]@{}
}

function Find-JsonPropertyName {
    # 按“精确匹配优先、忽略大小写兜底”的规则定位字典键。
    # 只要同一结构字段出现多个大小写变体，就拒绝静默选取其中一个，避免读写落到错误节点。
    param([System.Collections.IDictionary]$Object, [string]$Name)
    $matches = [System.Collections.Generic.List[string]]::new()
    foreach ($key in $Object.Keys) {
        $keyText = [string]$key
        if ($keyText.Equals($Name, [StringComparison]::Ordinal)) {
            $matches.Add($keyText)
        }
        elseif ($keyText.Equals($Name, [StringComparison]::OrdinalIgnoreCase)) {
            $matches.Add($keyText)
        }
    }
    if ($matches.Count -gt 1) {
        throw "JSON 对象包含多个仅大小写不同的结构键 '$Name'：$($matches -join ', ')"
    }
    if ($matches.Count -eq 1) {
        return $matches[0]
    }
    return $null
}

function Set-JsonProperty {
    # 按结构字段名新增或更新字典项；更新时保留用户文件中已有的键大小写。
    param([System.Collections.IDictionary]$Object, [string]$Name, [object]$Value)
    $propertyName = Find-JsonPropertyName -Object $Object -Name $Name
    if ($null -eq $propertyName) {
        $Object.Add($Name, $Value)
    }
    else {
        $Object[$propertyName] = $Value
    }
}

function Remove-JsonProperty {
    # 按与读取相同的大小写规则移除一个字典项；不存在时保持幂等。
    param([System.Collections.IDictionary]$Object, [string]$Name)
    $propertyName = Find-JsonPropertyName -Object $Object -Name $Name
    if ($null -ne $propertyName) {
        $Object.Remove($propertyName)
    }
}

function ConvertTo-JsonPreservingKeys {
    # 递归序列化 JSON 对象，逐项写出字典键，避免 ConvertTo-Json 将仅大小写不同的键折叠。
    # 标量仍交给 PowerShell 负责转义、数字格式和布尔/null 表示；对象/数组由本函数递归处理。
    # 缩进和换行由本函数生成，以同时保留重复大小写键和配置文件的可读格式。
    param(
        [object]$Value,
        [int]$IndentLevel = 0
    )
    $indent = ('  ' * $IndentLevel) -join ''
    $childIndent = ('  ' * ($IndentLevel + 1)) -join ''
    $lineBreak = [Environment]::NewLine
    if ($null -eq $Value) {
        return 'null'
    }
    if ($Value -is [System.Collections.IDictionary]) {
        if ($Value.Count -eq 0) {
            return '{}'
        }
        $members = [System.Collections.Generic.List[string]]::new()
        foreach ($key in $Value.Keys) {
            $encodedKey = ConvertTo-JsonPreservingKeys -Value ([string]$key) -IndentLevel ($IndentLevel + 1)
            $encodedValue = ConvertTo-JsonPreservingKeys -Value $Value[$key] -IndentLevel ($IndentLevel + 1)
            $members.Add($childIndent + $encodedKey + ': ' + $encodedValue)
        }
        return '{' + $lineBreak + ($members -join (',' + $lineBreak)) + $lineBreak + $indent + '}'
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        if (@($Value).Count -eq 0) {
            return '[]'
        }
        $items = [System.Collections.Generic.List[string]]::new()
        foreach ($item in $Value) {
            $encodedItem = ConvertTo-JsonPreservingKeys -Value $item -IndentLevel ($IndentLevel + 1)
            $items.Add($childIndent + $encodedItem)
        }
        return '[' + $lineBreak + ($items -join (',' + $lineBreak)) + $lineBreak + $indent + ']'
    }
    return [string](ConvertTo-Json -InputObject $Value -Compress -Depth 10)
}

function Add-ObjectProperty {
    # 对 JSON 字典执行新增或覆盖；保留旧函数名以减少调用方变化，但不再依赖 PSCustomObject 属性。
    param([System.Collections.IDictionary]$Object, [string]$Name, [object]$Value)
    Set-JsonProperty -Object $Object -Name $Name -Value $Value
}

function Read-JsonObject {
    # 缺失或空文件按空对象处理；已有内容以保留大小写键的字典解析，并要求根节点为对象。
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return New-JsonObject
    }
    $text = Get-Content -Raw -LiteralPath $Path
    if ([string]::IsNullOrWhiteSpace($text)) {
        return New-JsonObject
    }
    $value = $text | ConvertFrom-Json -AsHashtable
    if ($value -isnot [System.Collections.IDictionary]) {
        throw "JSON 配置根节点必须是对象：$Path"
    }
    return $value
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
    # 使用保留重复大小写键的序列化，并复用文本原子写入和备份策略。
    param([string]$Path, [object]$Value)
    $json = ConvertTo-JsonPreservingKeys -Value $Value
    Write-TextAtomic -Path $Path -Content ($json + [Environment]::NewLine)
}

function Remove-AikbHookHandlers {
    # 删除命令中引用 aikb-hook.ps1 的组，同时保留同组内的非 AIKB handlers。
    param([object]$Hooks, [string]$Event)
    $propertyName = Find-JsonPropertyName -Object $Hooks -Name $Event
    if ($null -eq $propertyName) {
        return
    }
    $keptGroups = [System.Collections.Generic.List[object]]::new()
    foreach ($group in @($Hooks[$propertyName])) {
        if ($null -eq $group) { continue }
        $handlersPropertyName = Find-JsonPropertyName -Object $group -Name 'hooks'
        if ($null -eq $handlersPropertyName) {
            $keptGroups.Add($group)
            continue
        }
        $keptHandlers = @($group[$handlersPropertyName] | Where-Object {
            $commandPropertyName = Find-JsonPropertyName -Object $_ -Name 'command'
            $null -eq $commandPropertyName -or [string]$_[$commandPropertyName] -notmatch [regex]::Escape($script:HookScriptName)
        })
        if ($keptHandlers.Count -gt 0) {
            $group[$handlersPropertyName] = [object[]]$keptHandlers
            $keptGroups.Add($group)
        }
    }
    $Hooks[$propertyName] = [object[]]$keptGroups.ToArray()
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
    $handler = New-JsonObject
    Set-JsonProperty -Object $handler -Name 'type' -Value 'command'
    Set-JsonProperty -Object $handler -Name 'command' -Value $Command
    Set-JsonProperty -Object $handler -Name 'timeout' -Value $Timeout
    if ($Shell) {
        Set-JsonProperty -Object $handler -Name 'shell' -Value $Shell
    }
    $group = if ($Matcher) {
        $newGroup = New-JsonObject
        Set-JsonProperty -Object $newGroup -Name 'matcher' -Value $Matcher
        Set-JsonProperty -Object $newGroup -Name 'hooks' -Value ([object[]]@($handler))
        $newGroup
    }
    else {
        $newGroup = New-JsonObject
        Set-JsonProperty -Object $newGroup -Name 'hooks' -Value ([object[]]@($handler))
        $newGroup
    }
    $groups = [System.Collections.Generic.List[object]]::new()
    $propertyName = Find-JsonPropertyName -Object $Hooks -Name $Event
    if ($null -ne $propertyName) {
        foreach ($existingGroup in @($Hooks[$propertyName])) { $groups.Add($existingGroup) }
    }
    $groups.Add($group)
    Set-JsonProperty -Object $Hooks -Name $Event -Value ([object[]]$groups.ToArray())
}

function Update-HooksJson {
    # 根据 Agent 生成对应 shell 语法的生命周期 hooks，并保留用户其他 hooks。
    param([string]$Path, [string]$Agent)
    $config = Read-JsonObject -Path $Path
    $hooksPropertyName = Find-JsonPropertyName -Object $config -Name 'hooks'
    if ($null -eq $hooksPropertyName) {
        $hooks = New-JsonObject
        Set-JsonProperty -Object $config -Name 'hooks' -Value $hooks
    }
    else {
        $hooks = $config[$hooksPropertyName]
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
    $hooksPropertyName = Find-JsonPropertyName -Object $config -Name 'hooks'
    if ($null -eq $hooksPropertyName) { return }
    foreach ($event in @('SessionStart', 'PreCompact', 'Stop', 'SessionEnd')) {
        Remove-AikbHookHandlers -Hooks $config[$hooksPropertyName] -Event $event
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
    $serversPropertyName = Find-JsonPropertyName -Object $config -Name 'mcpServers'
    if ($null -eq $serversPropertyName) {
        $servers = New-JsonObject
        Set-JsonProperty -Object $config -Name 'mcpServers' -Value $servers
    }
    else {
        $servers = $config[$serversPropertyName]
    }
    $aikbPropertyName = Find-JsonPropertyName -Object $servers -Name 'aikb'
    if ($null -ne $aikbPropertyName) {
        $aikb = $servers[$aikbPropertyName]
        $envPropertyName = Find-JsonPropertyName -Object $aikb -Name 'env'
        $managedMarkerName = if ($null -ne $envPropertyName) { Find-JsonPropertyName -Object $aikb[$envPropertyName] -Name 'AIKB_MANAGED' } else { $null }
        if ($null -eq $managedMarkerName -or [string]$aikb[$envPropertyName][$managedMarkerName] -ne '1') {
            throw "Claude Code 已存在非 AIKB 安装器管理的 mcpServers.aikb：$Path"
        }
    }
    $launcherCommand = "& (Join-Path `$env:AIKB_HOME 'system/tools/aikb-mcp/scripts/aikb.ps1') serve --agent claude-code"
    $server = New-JsonObject
    Set-JsonProperty -Object $server -Name 'type' -Value 'stdio'
    Set-JsonProperty -Object $server -Name 'command' -Value 'pwsh'
    Set-JsonProperty -Object $server -Name 'args' -Value ([object[]]@('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $launcherCommand))
    $serverEnvironment = New-JsonObject
    Set-JsonProperty -Object $serverEnvironment -Name 'AIKB_MANAGED' -Value '1'
    Set-JsonProperty -Object $server -Name 'env' -Value $serverEnvironment
    Set-JsonProperty -Object $servers -Name 'aikb' -Value $server
    Write-JsonAtomic -Path $Path -Value $config
}

function Remove-ClaudeMcp {
    # 仅当 Claude MCP 对象带 AIKB_MANAGED=1 时才删除 aikb 服务。
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    $config = Read-JsonObject -Path $Path
    $serversPropertyName = Find-JsonPropertyName -Object $config -Name 'mcpServers'
    if ($null -ne $serversPropertyName) {
        $servers = $config[$serversPropertyName]
        $aikbPropertyName = Find-JsonPropertyName -Object $servers -Name 'aikb'
        if ($null -ne $aikbPropertyName) {
            $aikb = $servers[$aikbPropertyName]
            $envPropertyName = Find-JsonPropertyName -Object $aikb -Name 'env'
            $managedMarkerName = if ($null -ne $envPropertyName) { Find-JsonPropertyName -Object $aikb[$envPropertyName] -Name 'AIKB_MANAGED' } else { $null }
            if ($null -ne $managedMarkerName -and [string]$aikb[$envPropertyName][$managedMarkerName] -eq '1') {
                Remove-JsonProperty -Object $servers -Name 'aikb'
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
