# 运行 AIKB WebUI、共享知识核心和控制仓结构回归；验证不修改正式知识。
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = 'utf-8'
$webRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$controlRoot = (Resolve-Path -LiteralPath (Join-Path $webRoot '../../..')).Path
$backendRoot = Join-Path $webRoot 'backend'
$frontendRoot = Join-Path $webRoot 'frontend'

python -m unittest discover -s (Join-Path $controlRoot 'system/tools/aikb-mcp/tests') -v
if ($LASTEXITCODE -ne 0) { throw "共享核心测试失败，退出码：$LASTEXITCODE" }

# 与正式 ``uvicorn --app-dir backend`` 使用相同的 Web 包可见边界。测试从
# backend 工作目录发现，既不依赖调用者的 PYTHONPATH，也不会因测试文件加载
# 顺序不同而让部分模块偶然可导入、部分模块失败。
Push-Location $backendRoot
try {
    python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw "Web 后端测试失败，退出码：$LASTEXITCODE" }
}
finally {
    Pop-Location
}

Push-Location $frontendRoot
try {
    npm run typecheck
    if ($LASTEXITCODE -ne 0) { throw "前端类型检查失败，退出码：$LASTEXITCODE" }
    npm run lint
    if ($LASTEXITCODE -ne 0) { throw "前端 lint 失败，退出码：$LASTEXITCODE" }
    npm test
    if ($LASTEXITCODE -ne 0) { throw "前端测试失败，退出码：$LASTEXITCODE" }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "前端构建失败，退出码：$LASTEXITCODE" }
}
finally {
    Pop-Location
}

pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $controlRoot 'system/tests/validate-structure.ps1') -KnowledgePath $env:AIKB_KNOWLEDGE_HOME
if ($LASTEXITCODE -ne 0) { throw "AIKB 结构校验失败，退出码：$LASTEXITCODE" }
Write-Host 'AIKB WebUI 全部校验通过。'
