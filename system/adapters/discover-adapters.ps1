# 扫描 adapters/ 的直接子目录并读取 adapter.json，输出统一的适配器摘要。
# 共享目录没有清单时会自然被跳过，避免把公共模块误报为 Agent 适配器。
$ErrorActionPreference = 'Stop'

Get-ChildItem -LiteralPath $PSScriptRoot -Directory |
    ForEach-Object {
        $manifestPath = Join-Path $_.FullName 'adapter.json'
        if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
            $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
            [pscustomobject]@{
                Id = $manifest.id
                Name = $manifest.displayName
                Platforms = ($manifest.platforms -join ',')
                McpStdio = [bool]$manifest.capabilities.mcpStdio
                Hooks = [bool]($manifest.capabilities.sessionStart -or $manifest.capabilities.preCompact -or $manifest.capabilities.stop -or $manifest.capabilities.sessionEnd)
                Path = $_.FullName
            }
        }
    }
