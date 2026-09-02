$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'scripts\start.ps1'
& $launcher
exit $LASTEXITCODE
