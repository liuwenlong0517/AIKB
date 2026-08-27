# 设置并校验 AIKB_HOME 与 AIKB_KNOWLEDGE_HOME；默认目标为脚本所在控制仓及其 content/。
# User 级变更会广播环境刷新消息，但已运行的 Agent 仍需重启才能继承新值。
param(
    [string]$Path = $(Join-Path $PSScriptRoot '..\..'),
    [string]$KnowledgePath,
    [ValidateSet('User', 'Process')]
    [string]$Target = 'User',
    [switch]$PassThru
)

$ErrorActionPreference = 'Stop'
# 显式固定输出编码为 UTF-8，避免重定向/非控制台环境下 GBK 字节被按 UTF-8 解码成乱码。
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
if (-not $IsWindows) {
    throw 'AIKB 当前只支持在 Windows 上设置用户级 AIKB_HOME。'
}
$resolved = (Resolve-Path -LiteralPath $Path).Path
$resolvedKnowledge = if ($KnowledgePath) {
    (Resolve-Path -LiteralPath $KnowledgePath).Path
}
else {
    (Resolve-Path -LiteralPath (Join-Path $resolved 'content')).Path
}

foreach ($required in @('ENTRY_RULES.md', 'system')) {
    if (-not (Test-Path -LiteralPath (Join-Path $resolved $required))) {
        throw "目标不是有效的 AIKB 仓库，缺少：$required"
    }
}

$knowledgeManifestPath = Join-Path $resolvedKnowledge '.aikb-knowledge.json'
if (-not (Test-Path -LiteralPath $knowledgeManifestPath -PathType Leaf)) {
    throw "目标不是有效的 AIKB 知识仓，缺少：$knowledgeManifestPath"
}
$knowledgeManifest = Get-Content -Raw -LiteralPath $knowledgeManifestPath | ConvertFrom-Json
# 同时验证 kind 和契约版本，避免只凭目录存在就接受错误知识仓。
if ($knowledgeManifest.kind -ne 'aikb-knowledge' -or $knowledgeManifest.contract_version -ne 1) {
    throw "AIKB 知识仓契约不兼容：$knowledgeManifestPath"
}

$environmentTarget = if ($Target -eq 'User') {
    [EnvironmentVariableTarget]::User
}
else {
    [EnvironmentVariableTarget]::Process
}

$previous = [Environment]::GetEnvironmentVariable('AIKB_HOME', $environmentTarget)
$previousKnowledge = [Environment]::GetEnvironmentVariable('AIKB_KNOWLEDGE_HOME', $environmentTarget)
[Environment]::SetEnvironmentVariable('AIKB_HOME', $resolved, $environmentTarget)
[Environment]::SetEnvironmentVariable('AIKB_KNOWLEDGE_HOME', $resolvedKnowledge, $environmentTarget)

if ($Target -eq 'User') {
    # 通知现有 Windows 进程环境发生变化；失败不会替代后续的持久化校验。
    if (-not ('AikbEnvironment.NativeMethods' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace AikbEnvironment {
    public static class NativeMethods {
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern IntPtr SendMessageTimeout(
            IntPtr hWnd, uint message, UIntPtr wParam, string lParam,
            uint flags, uint timeout, out UIntPtr result);
    }
}
'@
    }
    [UIntPtr]$broadcastResult = [UIntPtr]::Zero
    [void][AikbEnvironment.NativeMethods]::SendMessageTimeout(
        [IntPtr]0xffff, 0x001A, [UIntPtr]::Zero, 'Environment', 0x0002, 5000, [ref]$broadcastResult
    )
}

# 当前进程立即可用；User 级变更由之后启动的 Agent 和终端自动继承。
$env:AIKB_HOME = $resolved
$env:AIKB_KNOWLEDGE_HOME = $resolvedKnowledge
$stored = [Environment]::GetEnvironmentVariable('AIKB_HOME', $environmentTarget)
$storedKnowledge = [Environment]::GetEnvironmentVariable('AIKB_KNOWLEDGE_HOME', $environmentTarget)
if ($stored -ne $resolved) {
    throw "AIKB_HOME 写入后校验失败：$stored"
}
if ($storedKnowledge -ne $resolvedKnowledge) {
    throw "AIKB_KNOWLEDGE_HOME 写入后校验失败：$storedKnowledge"
}

$result = [pscustomobject]@{
    Name = 'AIKB_HOME'
    Target = $Target
    Previous = $previous
    Value = $stored
    KnowledgePrevious = $previousKnowledge
    KnowledgeValue = $storedKnowledge
    Changed = $previous -ne $stored -or $previousKnowledge -ne $storedKnowledge
}

if ($PassThru) {
    $result
}
else {
    Write-Host "已将 $Target 级 AIKB_HOME 设置为：$stored"
    Write-Host "已将 $Target 级 AIKB_KNOWLEDGE_HOME 设置为：$storedKnowledge"
    if ($Target -eq 'User') {
        Write-Host '当前 PowerShell 已可使用；请重新启动 Codex、Claude Code 和其他已打开的终端，使其继承新的用户环境变量。'
    }
}
