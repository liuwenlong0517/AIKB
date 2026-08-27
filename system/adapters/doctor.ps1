# 检查 AIKB 双仓环境、运行时命令、适配器清单及目标 Agent 配置是否就绪。
# 诊断不自动修复用户配置；会写入两条明确标记的本机审计 probe 以验证真实链路。
param(
    [ValidateSet('codex', 'claude-code')]
    [string[]]$Agents = @('codex', 'claude-code'),
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }),
    [string]$ClaudeHome = $(Join-Path $HOME '.claude'),
    [string]$ClaudeUserConfig = $(Join-Path $HOME '.claude.json'),
    [ValidateSet('User', 'Process')]
    [string]$EnvironmentTarget = 'User'
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$results = [System.Collections.Generic.List[object]]::new()

$userAikbHome = [Environment]::GetEnvironmentVariable('AIKB_HOME', 'User')
$processAikbHome = $env:AIKB_HOME
$userKnowledgeHome = [Environment]::GetEnvironmentVariable('AIKB_KNOWLEDGE_HOME', 'User')
$processKnowledgeHome = $env:AIKB_KNOWLEDGE_HOME
$userHomeValid = $false
if ($userAikbHome -and (Test-Path -LiteralPath $userAikbHome -PathType Container)) {
    $userHomeValid = (Resolve-Path -LiteralPath $userAikbHome).Path.Equals($repoRoot, [StringComparison]::OrdinalIgnoreCase)
}
$processHomeValid = $false
if ($processAikbHome -and (Test-Path -LiteralPath $processAikbHome -PathType Container)) {
    $processHomeValid = (Resolve-Path -LiteralPath $processAikbHome).Path.Equals($repoRoot, [StringComparison]::OrdinalIgnoreCase)
}
if ($EnvironmentTarget -eq 'User') {
    $results.Add([pscustomobject]@{ Check = 'AIKB_HOME:User'; Passed = $userHomeValid; Detail = $(if ($userAikbHome) { $userAikbHome } else { '未设置' }) })
}
$results.Add([pscustomobject]@{ Check = 'AIKB_HOME:Process'; Passed = $processHomeValid; Detail = $(if ($processAikbHome) { $processAikbHome } else { '未继承，请重启终端或 Agent' }) })

function Test-AikbKnowledgeHome {
    # 知识仓必须同时具备目录、契约文件和兼容的 kind/contract_version。
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Container)) { return $false }
    $manifestPath = Join-Path $Path '.aikb-knowledge.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { return $false }
    try {
        $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
        return $manifest.kind -eq 'aikb-knowledge' -and $manifest.contract_version -eq 1
    }
    catch { return $false }
}

if ($EnvironmentTarget -eq 'User') {
    $results.Add([pscustomobject]@{
        Check = 'AIKB_KNOWLEDGE_HOME:User'
        Passed = Test-AikbKnowledgeHome -Path $userKnowledgeHome
        Detail = $(if ($userKnowledgeHome) { $userKnowledgeHome } else { '未设置' })
    })
}
$results.Add([pscustomobject]@{
    Check = 'AIKB_KNOWLEDGE_HOME:Process'
    Passed = Test-AikbKnowledgeHome -Path $processKnowledgeHome
    Detail = $(if ($processKnowledgeHome) { $processKnowledgeHome } else { '未继承，请重启终端或 Agent' })
})

$python = Get-Command python -ErrorAction SilentlyContinue
$results.Add([pscustomobject]@{ Check = 'Python'; Passed = $null -ne $python; Detail = $(if ($python) { $python.Source } else { '未找到 Python' }) })
$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
$results.Add([pscustomobject]@{ Check = 'PowerShell 7'; Passed = $null -ne $pwsh; Detail = $(if ($pwsh) { $pwsh.Source } else { '未找到 pwsh' }) })

foreach ($adapter in & (Join-Path $PSScriptRoot 'discover-adapters.ps1')) {
    $results.Add([pscustomobject]@{ Check = "Adapter:$($adapter.Id)"; Passed = $true; Detail = $adapter.Path })
}

$checks = @()
# 只为用户选择的 Agent 构造检查项，避免把未使用的配置误报为失败。
if ($Agents -contains 'codex') {
    $checks += @{ Name = 'Codex Root'; Path = Join-Path $CodexHome 'AGENTS.md'; Pattern = 'AIKB_HOME.*ENTRY_RULES\.md' }
    $checks += @{ Name = 'Codex MCP'; Path = Join-Path $CodexHome 'config.toml'; Pattern = 'serve --agent codex' }
    $checks += @{ Name = 'Codex Hooks'; Path = Join-Path $CodexHome 'hooks.json'; Pattern = 'aikb-hook\.ps1' }
}
if ($Agents -contains 'claude-code') {
    $checks += @{ Name = 'Claude Root'; Path = Join-Path $ClaudeHome 'CLAUDE.md'; Pattern = 'AIKB_HOME.*ENTRY_RULES\.md' }
    $checks += @{ Name = 'Claude MCP'; Path = $ClaudeUserConfig; Pattern = 'serve --agent claude-code' }
    $checks += @{ Name = 'Claude Hooks'; Path = Join-Path $ClaudeHome 'settings.json'; Pattern = 'aikb-hook\.ps1' }
}
foreach ($check in $checks) {
    $exists = Test-Path -LiteralPath $check.Path -PathType Leaf
    $matched = $exists -and ((Get-Content -Raw -LiteralPath $check.Path) -match $check.Pattern)
    $results.Add([pscustomobject]@{ Check = $check.Name; Passed = $matched; Detail = $check.Path })
}

if ($python -and $processHomeValid -and (Test-AikbKnowledgeHome -Path $processKnowledgeHome)) {
    $toolRoot = Join-Path $repoRoot 'system\tools\aikb-mcp'
    $probeCode = @'
import json, sys
from aikb.audit import AuditStore
from aikb.config import Settings
from aikb.hooks import handle_hook
from aikb.server import MCPServer
s = Settings.load()
agent = sys.argv[1]
server = MCPServer(s, agent=agent)
server.handle({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","clientInfo":{"name":"aikb-doctor","version":"1"}}})
tool = server.handle({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_work_state","arguments":{}}})
handle_hook(agent, "pre-compact", {}, s)
loaded = AuditStore(s).read_events()
matches = [event for event in loaded["events"] if event.get("agent") == agent and event.get("operation") in {"initialize", "get_work_state", "pre-compact"}]
fallback_failures = sum(1 for event in loaded["events"] if event.get("_fallback") and event.get("status") == "failed")
print(json.dumps({"passed": bool(tool and not tool["result"]["isError"] and len(matches) >= 5), "last": matches[-1].get("timestamp") if matches else None, "fallback_failures": fallback_failures}, ensure_ascii=False, separators=(",",":")))
'@
    foreach ($agent in $Agents) {
        try {
            Push-Location -LiteralPath $toolRoot
            try {
                $probeOutput = & $python.Source -c $probeCode $agent
                $probeExitCode = $LASTEXITCODE
            }
            finally {
                Pop-Location
            }
            $probe = ($probeOutput -join "`n") | ConvertFrom-Json
            $probePassed = $probeExitCode -eq 0 -and [bool]$probe.passed
            $detail = "last=$($probe.last); fallback_failures=$($probe.fallback_failures)"
            $results.Add([pscustomobject]@{ Check = "Audit Probe:$agent"; Passed = $probePassed; Detail = $detail })
        }
        catch {
            $results.Add([pscustomobject]@{ Check = "Audit Probe:$agent"; Passed = $false; Detail = $_.Exception.Message })
        }
    }
}
else {
    $results.Add([pscustomobject]@{ Check = 'Audit Probe'; Passed = $false; Detail = 'Process 级双仓环境或 Python 不可用' })
}

$results | Format-Table -AutoSize
if (@($results | Where-Object { -not $_.Passed }).Count -gt 0) { exit 1 }
