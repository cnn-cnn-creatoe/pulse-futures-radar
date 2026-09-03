[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [Parameter(Mandatory = $true)][string]$PayloadRoot,
    [Parameter(Mandatory = $true)][string]$TempRoot,
    [Parameter(Mandatory = $true)][string]$ExpectedTag
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$payload = [System.IO.Path]::GetFullPath($PayloadRoot).TrimEnd('\')
$temp = [System.IO.Path]::GetFullPath($TempRoot).TrimEnd('\')
$systemTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\')
$runtime = Join-Path $root '.runtime'
$statusFile = Join-Path $runtime 'update-status.json'
$logFile = Join-Path $runtime 'update.log'
$lockFile = Join-Path $runtime 'update.lock'
$stage = '初始化更新器'

function Write-UpdateLog([string]$Message) {
    ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message) | Add-Content -LiteralPath $logFile -Encoding UTF8
}

function Write-UpdateStatus([string]$Status, [string]$Message) {
    [pscustomobject]@{
        status = $Status
        version = $ExpectedTag
        message = $Message
        updatedAt = (Get-Date).ToUniversalTime().ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $statusFile -Encoding UTF8
}

function Test-Preserved([string]$RelativePath) {
    $path = $RelativePath.Replace('/', '\')
    foreach ($prefix in @('.git\', '.runtime\', '.venv\', 'backend\data\', 'dashboard\node_modules\', 'dashboard\dist\', 'release\', '回测结果\')) {
        if ($path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    if ($path -match '(^|\\)(__pycache__|\.pytest_cache|\.wrangler)(\\|$)') { return $true }
    if ($path -match '(^|\\)\.env($|\.)') { return $true }
    if ($path -match '(?i)(telegram-token\.dpapi|telegram-settings\.json|settings\.json|\.sqlite3(?:-wal|-shm)?|\.log)$') { return $true }
    return $false
}

function Assert-ChildPath([string]$Candidate, [string]$Parent, [string]$Label) {
    $full = [System.IO.Path]::GetFullPath($Candidate)
    $base = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    if (-not $full.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) {
        throw ('安全检查失败：{0}不在允许目录内。' -f $Label)
    }
}

function Invoke-ScriptProcess([string]$ScriptPath, [string[]]$ExtraArguments) {
    $powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
    $arguments = @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $ScriptPath)) + $ExtraArguments
    $process = Start-Process -FilePath $powerShell -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden -Wait -PassThru
    return $process.ExitCode
}

function Start-DeferredCleanup {
    $cleanupScript = Join-Path $temp 'pulse-update-cleanup.ps1'
    if (-not (Test-Path -LiteralPath $cleanupScript)) { return }
    $powerShell = (Get-Command powershell.exe -ErrorAction SilentlyContinue).Source
    if (-not $powerShell) { return }
    Start-Process -FilePath $powerShell -ArgumentList @(
        '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $cleanupScript),
        '-Target', ('"{0}"' -f $temp), '-WaitForProcessId', [string]$PID
    ) -WorkingDirectory $systemTemp -WindowStyle Hidden | Out-Null
}

New-Item -ItemType Directory -Path $runtime -Force | Out-Null
Start-Sleep -Seconds 3

try {
    $stage = '检查更新目录'
    Assert-ChildPath $payload $temp '更新内容'
    if (Test-Path -LiteralPath (Join-Path $root '.git')) {
        throw '检测到 Git 开发工作区，临时更新器拒绝覆盖。'
    }
    if (-not $temp.StartsWith(($systemTemp + '\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw '安全检查失败：临时更新目录不在系统临时目录中。'
    }
    $payloadVersion = (Get-Content -LiteralPath (Join-Path $payload 'VERSION') -Raw).Trim()
    if ($payloadVersion -ne $ExpectedTag) { throw '应用更新前版本校验失败。' }

    $stage = '关闭旧版本服务'
    Write-UpdateStatus 'stopping' '正在安全关闭本项目服务。'
    Write-UpdateLog ('开始安装 {0}。' -f $ExpectedTag)
    $stopCode = Invoke-ScriptProcess (Join-Path $root 'scripts\stop.ps1') @('-Quiet')
    if ($stopCode -ne 0) { throw '无法安全关闭现有服务，因此没有覆盖程序文件。' }

    $stage = '准备备份目录'
    $backupRoot = Join-Path $temp 'backup'
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    $newFiles = New-Object System.Collections.Generic.List[string]
    $payloadPrefixLength = $payload.Length + 1
    $files = @(Get-ChildItem -LiteralPath $payload -File -Recurse)
    $oldManifest = $null
    $oldManifestFile = Join-Path $root 'release-manifest.json'
    if (Test-Path -LiteralPath $oldManifestFile) {
        try { $oldManifest = Get-Content -LiteralPath $oldManifestFile -Raw | ConvertFrom-Json } catch { $oldManifest = $null }
    }
    $newManifest = Get-Content -LiteralPath (Join-Path $payload 'release-manifest.json') -Raw | ConvertFrom-Json

    $stage = '替换程序文件'
    Write-UpdateStatus 'installing' '正在覆盖程序文件；历史数据和个人设置保持不变。'
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($payloadPrefixLength)
        if (Test-Preserved $relative) { continue }
        $destination = Join-Path $root $relative
        try { Assert-ChildPath $destination $root '目标文件' }
        catch { throw ('无法解析目标文件“{0}”：{1}' -f $relative, $_.Exception.Message) }
        $destinationDirectory = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $destinationDirectory)) {
            New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
        }

        if (Test-Path -LiteralPath $destination) {
            $backup = Join-Path $backupRoot $relative
            $backupDirectory = Split-Path -Parent $backup
            New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
            Copy-Item -LiteralPath $destination -Destination $backup -Force
        }
        else {
            $newFiles.Add($destination)
        }
        Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
    }

    $stage = '清理旧版本文件'
    if ($oldManifest -and $oldManifest.files) {
        $newSet = @{}
        foreach ($newRelative in @($newManifest.files)) {
            $newSet[([string]$newRelative).Replace('/', '\').ToLowerInvariant()] = $true
        }
        foreach ($oldRelativeValue in @($oldManifest.files)) {
            $oldRelative = ([string]$oldRelativeValue).Replace('/', '\')
            if (-not $oldRelative -or $oldRelative.Contains('..') -or [System.IO.Path]::IsPathRooted($oldRelative)) { continue }
            if ($newSet.ContainsKey($oldRelative.ToLowerInvariant()) -or (Test-Preserved $oldRelative)) { continue }
            $obsolete = Join-Path $root $oldRelative
            Assert-ChildPath $obsolete $root '旧版本残留文件'
            if (-not (Test-Path -LiteralPath $obsolete -PathType Leaf)) { continue }
            $backup = Join-Path $backupRoot $oldRelative
            $backupDirectory = Split-Path -Parent $backup
            New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
            Copy-Item -LiteralPath $obsolete -Destination $backup -Force
            Remove-Item -LiteralPath $obsolete -Force
        }
    }

    $stage = '启动新版本服务'
    Write-UpdateStatus 'restarting' '程序文件已更新，正在重新启动。'
    $startCode = Invoke-ScriptProcess (Join-Path $root 'scripts\start.ps1') @()
    if ($startCode -ne 0) { throw '新版本已经安装，但自动重启失败。请双击“一键启动.bat”并查看 .runtime 日志。' }

    Write-UpdateStatus 'complete' ('已更新到 {0} 并重新启动。' -f $ExpectedTag)
    Write-UpdateLog ('更新到 {0} 完成。' -f $ExpectedTag)
    Remove-Item -LiteralPath $lockFile -Force -ErrorAction SilentlyContinue
    Start-DeferredCleanup
    exit 0
}
catch {
    $message = '{0}失败：{1}' -f $stage, $_.Exception.Message
    Write-UpdateLog ('更新失败：{0}' -f $message)
    if ($_.ScriptStackTrace) { Write-UpdateLog ('位置：{0}' -f $_.ScriptStackTrace) }

    $rollbackSucceeded = $true
    try {
        $backupRoot = Join-Path $temp 'backup'
        if (Test-Path -LiteralPath $backupRoot) {
            $backupPrefixLength = $backupRoot.Length + 1
            foreach ($backup in @(Get-ChildItem -LiteralPath $backupRoot -File -Recurse)) {
                $relative = $backup.FullName.Substring($backupPrefixLength)
                if (Test-Preserved $relative) { continue }
                $destination = Join-Path $root $relative
                Assert-ChildPath $destination $root '回滚文件'
                $destinationDirectory = Split-Path -Parent $destination
                New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
                Copy-Item -LiteralPath $backup.FullName -Destination $destination -Force
            }
        }
        if ($null -ne (Get-Variable -Name newFiles -ErrorAction SilentlyContinue)) {
            foreach ($newFile in $newFiles) {
                Assert-ChildPath $newFile $root '回滚新增文件'
                if (Test-Path -LiteralPath $newFile -PathType Leaf) { Remove-Item -LiteralPath $newFile -Force -ErrorAction SilentlyContinue }
            }
        }
        Write-UpdateLog '已尝试恢复更新前的程序文件。'
    }
    catch {
        $rollbackSucceeded = $false
        Write-UpdateLog ('自动恢复也失败：{0}' -f $_.Exception.Message)
    }

    if ($rollbackSucceeded) {
        try { [void](Invoke-ScriptProcess (Join-Path $root 'scripts\start.ps1') @()) } catch { }
    }
    else {
        $message = '{0}；旧版本恢复未完成，为避免运行混合文件，程序没有自动重启。' -f $message
        Write-UpdateLog '旧版本恢复未完成，已跳过自动重启。'
    }
    Write-UpdateStatus 'error' $message
    Remove-Item -LiteralPath $lockFile -Force -ErrorAction SilentlyContinue
    Start-DeferredCleanup
    exit 1
}
