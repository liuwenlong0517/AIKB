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
