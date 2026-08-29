# 启动仅绑定 127.0.0.1 的 AIKB WebUI；第一阶段不允许通过参数扩大监听范围。
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,

    [switch]$Reload
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$webRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$backendRoot = Join-Path $webRoot 'backend'
$frontendIndex = Join-Path $webRoot 'frontend/dist/index.html'

if (-not (Test-Path -LiteralPath $frontendIndex -PathType Leaf)) {
    throw '缺少前端构建产物。请先运行 scripts/build-aikb-web.ps1。'
}
if (-not $env:AIKB_HOME -or -not $env:AIKB_KNOWLEDGE_HOME) {
    throw '当前进程缺少 AIKB_HOME 或 AIKB_KNOWLEDGE_HOME。请先完成 AIKB 路径配置并重启终端。'
}

$arguments = @('-m', 'uvicorn', 'aikb_web.main:app', '--app-dir', $backendRoot, '--host', '127.0.0.1', '--port', "$Port")
if ($Reload) { $arguments += '--reload' }
Write-Host "AIKB WebUI：http://127.0.0.1:$Port"
& python @arguments
exit $LASTEXITCODE
