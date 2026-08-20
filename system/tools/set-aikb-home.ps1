param(
    [string]$Path = $(Join-Path $PSScriptRoot '..\..'),
    [ValidateSet('User', 'Process')]
    [string]$Target = 'User',
    [switch]$PassThru
)

$ErrorActionPreference = 'Stop'
if (-not $IsWindows) {
    throw 'AIKB 当前只支持在 Windows 上设置用户级 AIKB_HOME。'
}
$resolved = (Resolve-Path -LiteralPath $Path).Path

foreach ($required in @('ENTRY_RULES.md', 'system', 'content')) {
    if (-not (Test-Path -LiteralPath (Join-Path $resolved $required))) {
        throw "目标不是有效的 AIKB 仓库，缺少：$required"
    }
}

$environmentTarget = if ($Target -eq 'User') {
    [EnvironmentVariableTarget]::User
}
else {
    [EnvironmentVariableTarget]::Process
}

$previous = [Environment]::GetEnvironmentVariable('AIKB_HOME', $environmentTarget)
[Environment]::SetEnvironmentVariable('AIKB_HOME', $resolved, $environmentTarget)

if ($Target -eq 'User') {
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
$stored = [Environment]::GetEnvironmentVariable('AIKB_HOME', $environmentTarget)
if ($stored -ne $resolved) {
    throw "AIKB_HOME 写入后校验失败：$stored"
}

$result = [pscustomobject]@{
    Name = 'AIKB_HOME'
    Target = $Target
    Previous = $previous
    Value = $stored
    Changed = $previous -ne $stored
}

if ($PassThru) {
    $result
}
else {
    Write-Host "已将 $Target 级 AIKB_HOME 设置为：$stored"
    if ($Target -eq 'User') {
        Write-Host '当前 PowerShell 已可使用；请重新启动 Codex、Claude Code 和其他已打开的终端，使其继承新的用户环境变量。'
    }
}
