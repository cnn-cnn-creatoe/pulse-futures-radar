[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Target,
    [Parameter(Mandatory = $true)][int]$WaitForProcessId
)

$ErrorActionPreference = 'SilentlyContinue'
$targetPath = [System.IO.Path]::GetFullPath($Target).TrimEnd('\')
$tempPath = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\') + '\'
if (-not $targetPath.StartsWith($tempPath, [StringComparison]::OrdinalIgnoreCase)) { exit 2 }
if ((Split-Path -Leaf $targetPath) -notlike 'pulse-update-*') { exit 3 }

$deadline = (Get-Date).AddMinutes(2)
while ((Get-Date) -lt $deadline -and (Get-Process -Id $WaitForProcessId -ErrorAction SilentlyContinue)) {
    Start-Sleep -Seconds 1
}
if (Get-Process -Id $WaitForProcessId -ErrorAction SilentlyContinue) { exit 4 }
if (Test-Path -LiteralPath $targetPath) { Remove-Item -LiteralPath $targetPath -Recurse -Force }
exit 0
