[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = [System.IO.Path]::GetFullPath($ProjectRoot)
$dashboardRoot = Join-Path $root 'dashboard'
$runtime = Join-Path $root '.runtime'
$logFile = Join-Path $runtime 'dashboard.log'
$npm = (Get-Command npm.cmd -ErrorAction Stop).Source

New-Item -ItemType Directory -Path $runtime -Force | Out-Null
Set-Location -LiteralPath $dashboardRoot
$env:VITE_CONFIG_NATIVE_IGNORE_WARNING = 'true'

try {
    & $npm run dev -- --host 127.0.0.1 --port 5173 *>> $logFile
    exit $LASTEXITCODE
}
catch {
    ('[{0}] 看板启动失败：{1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $_.Exception.Message) | Add-Content -LiteralPath $logFile -Encoding UTF8
    exit 1
}
