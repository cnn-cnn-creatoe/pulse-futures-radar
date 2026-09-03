[CmdletBinding()]
param(
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runtimeRoot = Join-Path $projectRoot '.runtime'

function Write-StopMessage([string]$Message, [ConsoleColor]$Color = [ConsoleColor]::Cyan) {
    if (-not $Quiet) { Write-Host ('[PULSE] {0}' -f $Message) -ForegroundColor $Color }
}

function Get-DescendantIds([int]$RootId) {
    $processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $result = New-Object System.Collections.Generic.List[int]
    $queue = New-Object System.Collections.Generic.Queue[int]
    $queue.Enqueue($RootId)
    while ($queue.Count -gt 0) {
        $parentId = $queue.Dequeue()
        foreach ($child in $processes | Where-Object { [int]$_.ParentProcessId -eq $parentId }) {
            $childId = [int]$child.ProcessId
            if (-not $result.Contains($childId)) {
                $result.Add($childId)
                $queue.Enqueue($childId)
            }
        }
    }
    return @($result)
}

function Stop-OwnedRunner([string]$Name, [string]$PidFile, [string]$RunnerFile) {
    if (-not (Test-Path -LiteralPath $PidFile)) {
        Write-StopMessage ('{0}没有 PID 记录，跳过。' -f $Name)
        return $true
    }

    $raw = (Get-Content -LiteralPath $PidFile -Raw -ErrorAction SilentlyContinue).Trim()
    $processId = 0
    if (-not [int]::TryParse($raw, [ref]$processId)) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        Write-StopMessage ('{0}的 PID 记录无效，已清理。' -f $Name) Yellow
        return $true
    }

    $process = Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $processId) -ErrorAction SilentlyContinue
    if (-not $process) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        Write-StopMessage ('{0}已经停止。' -f $Name)
        return $true
    }

    $expectedRunner = [System.IO.Path]::GetFullPath($RunnerFile)
    $commandLine = [string]$process.CommandLine
    $belongsToProject = $commandLine.IndexOf($expectedRunner, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $commandLine.IndexOf($projectRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0
    if (-not $belongsToProject) {
        Write-StopMessage ('安全检查未通过：PID {0} 不是本项目启动的{1}，因此没有结束它。' -f $processId, $Name) Red
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return $false
    }

    $descendants = @(Get-DescendantIds $processId)
    [array]::Reverse($descendants)
    foreach ($childId in $descendants) {
        Stop-Process -Id $childId -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue

    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline -and (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
        Start-Sleep -Milliseconds 250
    }
    if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
        Write-StopMessage ('{0}未能在限定时间内停止。' -f $Name) Red
        return $false
    }

    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Write-StopMessage ('{0}已停止。' -f $Name) Green
    return $true
}

function Stop-VerifiedPortOwner([int]$Port, [string]$Name) {
    $connections = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    if ($connections.Count -eq 0) { return $true }

    foreach ($ownerId in @($connections | Select-Object -ExpandProperty OwningProcess -Unique)) {
        $process = Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $ownerId) -ErrorAction SilentlyContinue
        if (-not $process) { continue }
        $commandLine = [string]$process.CommandLine
        if ($commandLine.IndexOf($projectRoot, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            Write-StopMessage ('安全检查未通过：端口 {0} 的进程不属于本项目，因此没有结束它。' -f $Port) Red
            return $false
        }

        $descendants = @(Get-DescendantIds ([int]$ownerId))
        [array]::Reverse($descendants)
        foreach ($childId in $descendants) {
            Stop-Process -Id $childId -Force -ErrorAction SilentlyContinue
        }
        Stop-Process -Id ([int]$ownerId) -Force -ErrorAction SilentlyContinue
    }

    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) {
        Start-Sleep -Milliseconds 250
    }
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
        Write-StopMessage ('{0}的端口 {1} 未能释放。' -f $Name, $Port) Red
        return $false
    }
    Write-StopMessage ('{0}残留进程已清理。' -f $Name) Green
    return $true
}

try {
    if (-not (Test-Path -LiteralPath $runtimeRoot)) {
        Write-StopMessage '没有发现运行中的本项目服务。'
        exit 0
    }

    $dashboardOk = Stop-OwnedRunner '网页看板' (Join-Path $runtimeRoot 'dashboard.pid') (Join-Path $PSScriptRoot 'run-dashboard.ps1')
    $backendOk = Stop-OwnedRunner '实时行情服务' (Join-Path $runtimeRoot 'backend.pid') (Join-Path $PSScriptRoot 'run-backend.ps1')
    $dashboardPortOk = Stop-VerifiedPortOwner 5173 '网页看板'
    $backendPortOk = Stop-VerifiedPortOwner 8765 '实时行情服务'
    if (-not $dashboardOk -or -not $backendOk -or -not $dashboardPortOk -or -not $backendPortOk) { exit 1 }
    Write-StopMessage 'PULSE 已全部关闭。' Green
    exit 0
}
catch {
    Write-StopMessage ('关闭失败：{0}' -f $_.Exception.Message) Red
    exit 1
}
