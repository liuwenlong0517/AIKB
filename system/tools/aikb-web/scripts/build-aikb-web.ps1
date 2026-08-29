# 构建 AIKB WebUI 前端并检查后端 Python 语法；不修改知识事实源或本机环境变量。
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$webRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$frontendRoot = Join-Path $webRoot 'frontend'
$backendRoot = Join-Path $webRoot 'backend'

Push-Location $frontendRoot
try {
    npm ci
    if ($LASTEXITCODE -ne 0) { throw "npm ci 失败，退出码：$LASTEXITCODE" }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "前端构建失败，退出码：$LASTEXITCODE" }
}
finally {
    Pop-Location
}

python -m compileall -q $backendRoot
if ($LASTEXITCODE -ne 0) { throw "后端语法检查失败，退出码：$LASTEXITCODE" }
Write-Host 'AIKB WebUI 构建完成。'
