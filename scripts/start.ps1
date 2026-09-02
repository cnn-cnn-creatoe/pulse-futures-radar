[CmdletBinding()]
param(
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:RuntimeRoot = Join-Path $script:ProjectRoot '.runtime'
$script:MinimumPython = [version]'3.11.0'
$script:MinimumNode = [version]'22.13.0'

function Write-Step([string]$Message) {
    Write-Host ('[PULSE] {0}' -f $Message) -ForegroundColor Cyan
}

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    # Preserve process-only entries (notably WindowsApps and the Python
    # launcher) while also picking up tools that winget just installed.
    $segments = New-Object System.Collections.Generic.List[string]
    foreach ($source in @($machinePath, $userPath, $env:Path)) {
        foreach ($segment in ([string]$source -split ';')) {
            $trimmed = $segment.Trim()
            if ($trimmed -and -not $segments.Contains($trimmed)) { $segments.Add($trimmed) }
        }
    }
    $env:Path = $segments -join ';'
}

function Install-WingetPackage([string]$Id, [string]$DisplayName) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw ('缺少 {0}，并且系统没有 winget。请先从 Microsoft Store 安装“应用安装程序”，然后重新双击“一键启动.bat”。' -f $DisplayName)
    }

    Write-Step ('正在通过 winget 安装或更新 {0}…' -f $DisplayName)
    $process = Start-Process -FilePath $winget.Source -ArgumentList @(
        'install', '--id', $Id, '--exact', '--silent',
        '--accept-package-agreements', '--accept-source-agreements'
    ) -NoNewWindow -PassThru
    try { Wait-Process -Id $process.Id -Timeout 600 -ErrorAction Stop }
    catch {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw ('{0} 自动安装超过 10 分钟，已停止等待。请检查网络或手动安装后重试。' -f $DisplayName)
    }
    if ($process.ExitCode -ne 0) {
        throw ('{0} 自动安装失败（winget 退出码 {1}）。请手动安装后重新启动。' -f $DisplayName, $process.ExitCode)
    }
    Refresh-ProcessPath
}

function Convert-ToVersion([string]$Text) {
    $match = [regex]::Match($Text, '(\d+)\.(\d+)\.(\d+)')
    if (-not $match.Success) { return $null }
    return [version]('{0}.{1}.{2}' -f $match.Groups[1].Value, $match.Groups[2].Value, $match.Groups[3].Value)
}

function Find-CompatiblePython {
    $candidates = @(
        @{ Name = 'py.exe'; Prefix = @('-3.12') },
        @{ Name = 'py.exe'; Prefix = @('-3.11') },
        @{ Name = 'py.exe'; Prefix = @('-3') },
        @{ Name = 'python.exe'; Prefix = @() }
    )
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Name -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        try {
            # `--version` avoids Windows PowerShell 5.1 native-argument quote
            # rewriting that can corrupt a Python `-c` expression.
            $text = (& $command.Source @($candidate.Prefix) --version 2>&1 | Select-Object -Last 1)
            $version = Convert-ToVersion ([string]$text)
            if ($LASTEXITCODE -eq 0 -and $version -and $version -ge $script:MinimumPython) {
                return [pscustomobject]@{ File = $command.Source; Prefix = @($candidate.Prefix); Version = $version }
            }
        }
        catch { }
    }
    return $null
}

function Ensure-Tools {
    Refresh-ProcessPath

    $python = Find-CompatiblePython
    if (-not $python) {
        Install-WingetPackage 'Python.Python.3.12' 'Python 3.12'
        $python = Find-CompatiblePython
    }
    if (-not $python) { throw '未找到 Python 3.11 或更高版本。请安装 64 位 Python 3.12，并勾选“Add Python to PATH”。' }
    Write-Step ('Python {0} 已就绪。' -f $python.Version)

    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    $nodeVersion = if ($node) { Convert-ToVersion ([string](& $node.Source --version 2>$null)) } else { $null }
    $nodeIsLtsLine = $nodeVersion -and (($nodeVersion.Major % 2) -eq 0)
    if (-not $nodeVersion -or $nodeVersion -lt $script:MinimumNode -or -not $nodeIsLtsLine) {
        Install-WingetPackage 'OpenJS.NodeJS.LTS' 'Node.js LTS'
        $node = Get-Command node.exe -ErrorAction SilentlyContinue
        $nodeVersion = if ($node) { Convert-ToVersion ([string](& $node.Source --version 2>$null)) } else { $null }
    }
    if (-not $nodeVersion -or $nodeVersion -lt $script:MinimumNode -or (($nodeVersion.Major % 2) -ne 0)) {
        throw '未找到符合要求的 Node.js LTS（至少 22.13）。请从 nodejs.org 安装 LTS 版后重试。'
    }
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw 'Node.js 已存在，但 npm.cmd 不在 PATH 中。请重新安装 Node.js LTS。' }
    Write-Step ('Node.js {0} 已就绪。' -f $nodeVersion)

    if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
        Install-WingetPackage 'Git.Git' 'Git'
    }
    if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) { throw 'Git 安装后仍未找到，请重启电脑再试。' }
    Write-Step 'Git 已就绪。'

    if (-not (Get-Command gh.exe -ErrorAction SilentlyContinue)) {
        Install-WingetPackage 'GitHub.cli' 'GitHub CLI'
    }
    if (-not (Get-Command gh.exe -ErrorAction SilentlyContinue)) { throw 'GitHub CLI 安装后仍未找到，请重启电脑再试。' }
    Write-Step 'GitHub CLI 已就绪（只有检查私有版本更新时才需要登录）。'

    return $python
}

function Get-OwnedRunner([string]$PidFile, [string]$RunnerFile) {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $raw = (Get-Content -LiteralPath $PidFile -Raw -ErrorAction SilentlyContinue).Trim()
    $processId = 0
    if (-not [int]::TryParse($raw, [ref]$processId)) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return $null
    }
    $process = Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $processId) -ErrorAction SilentlyContinue
    if (-not $process) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return $null
    }
    $runner = [System.IO.Path]::GetFullPath($RunnerFile)
    $commandLine = [string]$process.CommandLine
    if ($commandLine.IndexOf($runner, [StringComparison]::OrdinalIgnoreCase) -lt 0 -or
        $commandLine.IndexOf($script:ProjectRoot, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        return $null
    }
    return $process
}

function Assert-PortFree([int]$Port) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        throw ('本机端口 {0} 已被其他程序占用。请关闭占用程序后再启动；本脚本不会结束不属于 PULSE 的进程。' -f $Port)
    }
}

function Wait-HttpReady([string]$Url, [int]$TimeoutSeconds, [string]$Name) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                Write-Step ('{0}已就绪。' -f $Name)
                return
            }
        }
        catch { }
        Start-Sleep -Seconds 2
    }
    throw ('等待{0}就绪超时。请查看 .runtime 文件夹中的日志。' -f $Name)
}

function Get-Sha256([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $algorithm = [System.Security.Cryptography.SHA256]::Create()
        try { return ([System.BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '') }
        finally { $algorithm.Dispose() }
    }
    finally { $stream.Dispose() }
}

function Get-DependencyMarker {
    $requirements = Join-Path $script:ProjectRoot 'backend\requirements.txt'
    $packageLock = Join-Path $script:ProjectRoot 'dashboard\package-lock.json'
    return [pscustomobject]@{
        requirements = Get-Sha256 $requirements
        packageLock = Get-Sha256 $packageLock
    }
}

function Test-MarkerValue($Existing, $Wanted) {
    if (-not $Existing) { return $false }
    $requirementsProperty = $Existing.PSObject.Properties['requirements']
    $packageLockProperty = $Existing.PSObject.Properties['packageLock']
    return $requirementsProperty -and $packageLockProperty -and
        $requirementsProperty.Value -eq $Wanted.requirements -and
        $packageLockProperty.Value -eq $Wanted.packageLock
}

try {
    New-Item -ItemType Directory -Path $script:RuntimeRoot -Force | Out-Null

    $backendPidFile = Join-Path $script:RuntimeRoot 'backend.pid'
    $dashboardPidFile = Join-Path $script:RuntimeRoot 'dashboard.pid'
    $backendRunner = Join-Path $PSScriptRoot 'run-backend.ps1'
    $dashboardRunner = Join-Path $PSScriptRoot 'run-dashboard.ps1'
    $ownedBackend = Get-OwnedRunner $backendPidFile $backendRunner
    $ownedDashboard = Get-OwnedRunner $dashboardPidFile $dashboardRunner

    if ($ownedBackend -and $ownedDashboard) {
        Write-Step '监测服务和看板已经在运行。'
        if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:5173/' }
        exit 0
    }
    if ($ownedBackend -or $ownedDashboard) {
        throw '检测到 PULSE 只启动了一部分。请先双击“一键关闭.bat”，再双击“一键启动.bat”。'
    }

    Assert-PortFree 8765
    Assert-PortFree 5173

    $python = Ensure-Tools
    $venvRoot = Join-Path $script:ProjectRoot '.venv'
    $venvPython = Join-Path $venvRoot 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        if (Test-Path -LiteralPath $venvRoot) {
            $archive = Join-Path $script:RuntimeRoot ('.venv-invalid-{0}' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
            Write-Step ('发现不完整的虚拟环境，已移到 {0}。' -f $archive)
            Move-Item -LiteralPath $venvRoot -Destination $archive
        }
        Write-Step '正在创建本机 Python 运行环境…'
        & $python.File @($python.Prefix) -m venv $venvRoot
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) { throw 'Python 虚拟环境创建失败。' }
    }

    $wantedMarker = Get-DependencyMarker
    $markerFile = Join-Path $script:RuntimeRoot 'dependencies.json'
    $existingMarker = $null
    if (Test-Path -LiteralPath $markerFile) {
        try { $existingMarker = Get-Content -LiteralPath $markerFile -Raw | ConvertFrom-Json } catch { $existingMarker = $null }
    }

    $nodeModules = Join-Path $script:ProjectRoot 'dashboard\node_modules'
    if (-not (Test-MarkerValue $existingMarker $wantedMarker) -or -not (Test-Path -LiteralPath $nodeModules)) {
        Write-Step '首次运行或依赖有更新，正在安装后端组件…'
        & $venvPython -m pip install --disable-pip-version-check --upgrade pip
        if ($LASTEXITCODE -ne 0) { throw 'pip 更新失败，请检查网络后重试。' }
        & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $script:ProjectRoot 'backend\requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw '后端组件安装失败，请检查网络后重试。' }

        Write-Step '正在安装网页组件，首次运行可能需要几分钟…'
        Push-Location (Join-Path $script:ProjectRoot 'dashboard')
        try {
            & (Get-Command npm.cmd -ErrorAction Stop).Source ci --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw '网页组件安装失败，请检查网络后重试。' }
        }
        finally { Pop-Location }

        $wantedMarker | ConvertTo-Json | Set-Content -LiteralPath $markerFile -Encoding UTF8
    }
    else {
        Write-Step '本机组件已安装，无需重复下载。'
    }

    $powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
    Write-Step '正在后台启动实时行情服务…'
    $backendProcess = Start-Process -FilePath $powerShell -ArgumentList @(
        '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $backendRunner),
        '-ProjectRoot', ('"{0}"' -f $script:ProjectRoot)
    ) -WorkingDirectory $script:ProjectRoot -WindowStyle Hidden -PassThru
    [string]$backendProcess.Id | Set-Content -LiteralPath $backendPidFile -Encoding ASCII

    Write-Step '正在后台启动网页看板…'
    $dashboardProcess = Start-Process -FilePath $powerShell -ArgumentList @(
        '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $dashboardRunner),
        '-ProjectRoot', ('"{0}"' -f $script:ProjectRoot)
    ) -WorkingDirectory $script:ProjectRoot -WindowStyle Hidden -PassThru
    [string]$dashboardProcess.Id | Set-Content -LiteralPath $dashboardPidFile -Encoding ASCII

    try {
        Wait-HttpReady 'http://127.0.0.1:8765/api/status' 180 '实时行情服务'
        Wait-HttpReady 'http://127.0.0.1:5173/' 180 '网页看板'
    }
    catch {
        & (Join-Path $PSScriptRoot 'stop.ps1') -Quiet
        throw
    }

    Write-Step '启动完成。首次使用时，页面会显示历史数据下载进度；完成后会自动持续保存实时数据。'
    if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:5173/' }
    exit 0
}
catch {
    Write-Host ('[PULSE] 启动失败：{0}' -f $_.Exception.Message) -ForegroundColor Red
    Write-Host ('日志位置：{0}' -f $script:RuntimeRoot) -ForegroundColor Yellow
    exit 1
}
