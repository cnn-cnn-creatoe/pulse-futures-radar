[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = [System.IO.Path]::GetFullPath($ProjectRoot)
$backendRoot = Join-Path $root 'backend'
$python = Join-Path $root '.venv\Scripts\python.exe'
$runtime = Join-Path $root '.runtime'
$stdoutLog = Join-Path $runtime 'backend.stdout.log'
$stderrLog = Join-Path $runtime 'backend.stderr.log'

New-Item -ItemType Directory -Path $runtime -Force | Out-Null
Set-Location -LiteralPath $backendRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'

try {
    # Uvicorn writes normal INFO lines to stderr.  Windows PowerShell 5.1 can
    # turn redirected native stderr into a terminating NativeCommandError, so
    # launch it as a child process with real file redirection instead.
    $process = Start-Process -FilePath $python -ArgumentList @(
        '-u', '-m', 'uvicorn', 'monitor.api:app',
        '--host', '127.0.0.1', '--port', '8765'
    ) -WorkingDirectory $backendRoot -NoNewWindow -PassThru -Wait `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
    exit $process.ExitCode
}
catch {
    ('[{0}] 后端启动失败：{1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $_.Exception.Message) | Add-Content -LiteralPath $stderrLog -Encoding UTF8
    exit 1
}
