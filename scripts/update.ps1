[CmdletBinding()]
param(
    [ValidateSet('Check', 'Apply')]
    [string]$Mode = 'Check',
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$versionFile = Join-Path $projectRoot 'VERSION'
$releaseFile = Join-Path $projectRoot 'release.json'

function Write-Result($Value) {
    if ($Json) {
        $Value | ConvertTo-Json -Depth 8 -Compress
    }
    else {
        if ($Value.message) { Write-Host ('[PULSE] {0}' -f $Value.message) -ForegroundColor Cyan }
        $Value | Format-List | Out-Host
    }
}

function Convert-SemVer([string]$Tag) {
    $match = [regex]::Match($Tag, '^v(\d+)\.(\d+)\.(\d+)$')
    if (-not $match.Success) { return $null }
    return [version]('{0}.{1}.{2}' -f $match.Groups[1].Value, $match.Groups[2].Value, $match.Groups[3].Value)
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

function Resolve-Repository {
    if (-not (Test-Path -LiteralPath $releaseFile)) { throw '缺少 release.json，无法连接版本仓库。' }
    try { $config = Get-Content -LiteralPath $releaseFile -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw 'release.json 无法读取，已停止更新。' }
    if ([string]$config.release_channel -ne 'stable') { throw 'release.json 的发布通道无效，已停止更新。' }
    $candidate = ([string]$config.repository).Trim().TrimEnd('/')
    if ($candidate -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
        throw '还没有设置私有 GitHub 仓库。发布者必须在根目录 release.json 的 repository 中填写 owner/repository。'
    }
    return $candidate
}

function Get-ReleaseState {
    if (-not (Test-Path -LiteralPath $versionFile)) { throw '缺少 VERSION 文件，无法判断当前版本。' }
    if (-not (Test-Path -LiteralPath $releaseFile)) { throw '缺少 release.json，无法连接版本仓库。' }
    $currentTag = (Get-Content -LiteralPath $versionFile -Raw -Encoding UTF8).Trim()
    $currentVersion = Convert-SemVer $currentTag
    if (-not $currentVersion) { throw ('VERSION 内容“{0}”不是 v主版本.次版本.修订号 格式。' -f $currentTag) }
    $releaseConfig = Get-Content -LiteralPath $releaseFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$releaseConfig.version -ne $currentTag) { throw 'release.json 与 VERSION 的当前版本不一致。' }
    if ([string]$releaseConfig.asset_name -ne ('PULSE-Windows-{0}.zip' -f $currentTag)) {
        throw 'release.json 的当前版本资产名不一致。'
    }

    $gh = Get-Command gh.exe -ErrorAction SilentlyContinue
    if (-not $gh) { throw '未安装 GitHub CLI。请先运行“一键启动.bat”完成环境安装。' }
    & $gh.Source auth status --hostname github.com *> $null
    if ($LASTEXITCODE -ne 0) {
        throw '这是一套私有仓库更新。请先在此电脑打开终端运行 gh auth login，并用拥有仓库访问权限的 GitHub 账号登录。'
    }

    $repository = Resolve-Repository
    $releaseText = (& $gh.Source release list --repo $repository --limit 100 --json tagName,publishedAt,isDraft,isPrerelease 2>$null) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw ('无法读取私有仓库 {0} 的版本，请检查账号权限和网络。' -f $repository) }
    # Windows PowerShell 5.1 returns a JSON array as one nested pipeline
    # value when ConvertFrom-Json is wrapped directly in @(...).  Parse first,
    # then enumerate and wrap the filtered result so zero/one/many releases
    # all have a reliable Count property.
    $parsedReleases = $releaseText | ConvertFrom-Json
    $releases = @($parsedReleases | Where-Object {
        -not $_.isDraft -and -not $_.isPrerelease -and (Convert-SemVer ([string]$_.tagName))
    })
    if ($releases.Count -eq 0) { throw '仓库中还没有可用的稳定 v* Release。' }

    $latest = $releases | Sort-Object @{ Expression = { Convert-SemVer ([string]$_.tagName) }; Descending = $true } | Select-Object -First 1
    $latestTag = [string]$latest.tagName
    $latestVersion = Convert-SemVer $latestTag
    $available = $latestVersion -gt $currentVersion

    return [pscustomobject]@{
        ok = $true
        status = if ($available) { 'update_available' } else { 'up_to_date' }
        updateAvailable = $available
        currentVersion = $currentTag
        latestVersion = $latestTag
        repository = $repository
        publishedAt = [string]$latest.publishedAt
        message = if ($available) { '发现新版本 {0}，可以开始更新。' -f $latestTag } else { '当前已是最新版本 {0}。' -f $currentTag }
    }
}

function Find-PayloadRoot([string]$ExtractRoot, [string]$ExpectedTag) {
    $directVersion = Join-Path $ExtractRoot 'VERSION'
    if (Test-Path -LiteralPath $directVersion) { $candidate = $ExtractRoot }
    else {
        $children = @(Get-ChildItem -LiteralPath $ExtractRoot -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'VERSION') })
        if ($children.Count -ne 1) { throw '下载包结构无效：找不到唯一的 VERSION 文件。' }
        $candidate = $children[0].FullName
    }
    $payloadTag = (Get-Content -LiteralPath (Join-Path $candidate 'VERSION') -Raw -Encoding UTF8).Trim()
    if ($payloadTag -ne $ExpectedTag) { throw ('下载包版本为 {0}，预期为 {1}，已停止更新。' -f $payloadTag, $ExpectedTag) }
    foreach ($required in @('scripts\start.ps1', 'scripts\stop.ps1', 'scripts\update.ps1', 'scripts\update-worker.ps1', 'scripts\update-cleanup.ps1', 'release-manifest.json', '一键启动.bat')) {
        if (-not (Test-Path -LiteralPath (Join-Path $candidate $required))) { throw ('下载包缺少 {0}，已停止更新。' -f $required) }
    }
    return [System.IO.Path]::GetFullPath($candidate)
}

function Assert-ReleaseManifest([string]$PayloadRoot, [string]$Version) {
    $manifestFile = Join-Path $PayloadRoot 'release-manifest.json'
    try { $manifest = Get-Content -LiteralPath $manifestFile -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw '发布清单无法读取，已停止更新。' }
    if ([string]$manifest.version -ne $Version -or -not $manifest.files) {
        throw '发布清单的版本或文件列表无效，已停止更新。'
    }
    $prefixLength = $PayloadRoot.TrimEnd('\').Length + 1
    $actualFiles = @(Get-ChildItem -LiteralPath $PayloadRoot -File -Recurse | ForEach-Object {
        $_.FullName.Substring($prefixLength).Replace('\', '/')
    } | Sort-Object)
    $listedFiles = @($manifest.files | ForEach-Object {
        $relative = [string]$_
        if (-not $relative -or $relative.Contains('..') -or [System.IO.Path]::IsPathRooted($relative)) {
            throw '发布清单包含不安全路径，已停止更新。'
        }
        $relative.Replace('\', '/')
    } | Sort-Object -Unique)
    if ($actualFiles.Count -ne $listedFiles.Count -or
        (Compare-Object -ReferenceObject $actualFiles -DifferenceObject $listedFiles -CaseSensitive:$false)) {
        throw '发布清单与 ZIP 内文件不一致，已停止更新。'
    }
}

try {
    $state = Get-ReleaseState
    if ($Mode -eq 'Check' -or -not $state.updateAvailable) {
        Write-Result $state
        exit 0
    }

    if (Test-Path -LiteralPath (Join-Path $projectRoot '.git')) {
        throw '检测到 Git 开发工作区。为避免覆盖未提交代码，一键更新只用于从 Release 解压安装的版本；开发工作区请使用 Git 自行更新。'
    }

    $runtimeRoot = Join-Path $projectRoot '.runtime'
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    $lockFile = Join-Path $runtimeRoot 'update.lock'
    if (Test-Path -LiteralPath $lockFile) {
        $age = (Get-Date) - (Get-Item -LiteralPath $lockFile).LastWriteTime
        if ($age.TotalMinutes -lt 30) { throw '已有更新任务正在进行，请稍候。' }
        Remove-Item -LiteralPath $lockFile -Force
    }
    $lockStream = [System.IO.File]::Open($lockFile, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    $lockStream.Dispose()

    $tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    $updateRoot = Join-Path $tempBase ('pulse-update-{0}-{1}' -f $state.latestVersion, [guid]::NewGuid().ToString('N'))
    $downloadRoot = Join-Path $updateRoot 'download'
    $extractRoot = Join-Path $updateRoot 'payload'
    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null

    $gh = (Get-Command gh.exe -ErrorAction Stop).Source
    $assetName = 'PULSE-Windows-{0}.zip' -f $state.latestVersion
    $assetPath = Join-Path $downloadRoot $assetName
    $checksumName = '{0}.sha256' -f $assetName
    $checksumPath = Join-Path $downloadRoot $checksumName
    & $gh release download $state.latestVersion --repo $state.repository --pattern $assetName --pattern $checksumName --dir $downloadRoot 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $assetPath) -or -not (Test-Path -LiteralPath $checksumPath)) {
        throw ('Release 必须同时提供 {0} 和 {1}；缺少任一文件都不会安装。' -f $assetName, $checksumName)
    }

    $checksumText = (Get-Content -LiteralPath $checksumPath -Raw -Encoding UTF8).Trim()
    $checksumMatch = [regex]::Match($checksumText, '^(?i)([0-9a-f]{64})(?:\s+\*?(.+))?$')
    if (-not $checksumMatch.Success) { throw 'Release 的 SHA256 文件格式无效。' }
    if ($checksumMatch.Groups[2].Success -and $checksumMatch.Groups[2].Value.Trim() -ne $assetName) {
        throw 'SHA256 文件中的资产名称与目标 ZIP 不一致。'
    }
    $expectedHash = $checksumMatch.Groups[1].Value.ToUpperInvariant()
    $actualHash = (Get-Sha256 $assetPath).ToUpperInvariant()
    if ($actualHash -ne $expectedHash) { throw 'Release 资产 SHA256 校验失败，文件可能损坏或被替换，已停止更新。' }

    Expand-Archive -LiteralPath $assetPath -DestinationPath $extractRoot -Force
    $payloadRoot = Find-PayloadRoot $extractRoot $state.latestVersion
    Assert-ReleaseManifest $payloadRoot $state.latestVersion
    $workerSource = Join-Path $payloadRoot 'scripts\update-worker.ps1'
    if (-not (Test-Path -LiteralPath $workerSource)) { throw '更新包缺少临时更新器，已停止更新。' }
    $workerCopy = Join-Path $updateRoot 'pulse-update-worker.ps1'
    Copy-Item -LiteralPath $workerSource -Destination $workerCopy -Force
    Copy-Item -LiteralPath (Join-Path $payloadRoot 'scripts\update-cleanup.ps1') -Destination (Join-Path $updateRoot 'pulse-update-cleanup.ps1') -Force

    $powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
    Start-Process -FilePath $powerShell -ArgumentList @(
        '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $workerCopy),
        '-ProjectRoot', ('"{0}"' -f $projectRoot),
        '-PayloadRoot', ('"{0}"' -f $payloadRoot),
        '-TempRoot', ('"{0}"' -f $updateRoot),
        '-ExpectedTag', $state.latestVersion
    ) -WorkingDirectory $tempBase -WindowStyle Hidden | Out-Null

    $result = [pscustomobject]@{
        ok = $true
        status = 'applying'
        updateAvailable = $true
        currentVersion = $state.currentVersion
        latestVersion = $state.latestVersion
        repository = $state.repository
        message = '更新包已下载并校验。应用将在几秒内关闭、覆盖程序文件并自动重新启动。历史数据和 Telegram 设置会保留。'
    }
    Write-Result $result
    exit 0
}
catch {
    if ($Mode -eq 'Apply' -and (Test-Path -LiteralPath (Join-Path $projectRoot '.runtime\update.lock'))) {
        Remove-Item -LiteralPath (Join-Path $projectRoot '.runtime\update.lock') -Force -ErrorAction SilentlyContinue
    }
    $updateVariable = Get-Variable -Name updateRoot -ErrorAction SilentlyContinue
    if ($updateVariable -and $updateVariable.Value) {
        $candidate = [System.IO.Path]::GetFullPath([string]$updateVariable.Value)
        $tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        if ($candidate.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $candidate)) {
            Remove-Item -LiteralPath $candidate -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    $failure = [pscustomobject]@{ ok = $false; status = 'error'; updateAvailable = $false; message = $_.Exception.Message }
    Write-Result $failure
    exit 1
}
