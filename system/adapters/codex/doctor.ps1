param([string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }))
& (Join-Path $PSScriptRoot '..\doctor.ps1') -Agents codex -CodexHome $CodexHome
exit 0
