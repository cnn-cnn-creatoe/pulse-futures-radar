param([string]$ProjectRoot, [switch]$VerifyOnly)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ownedLock = $false
$handedOff = $false
try {
    if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
    $root = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
    if (-not (Test-Path -LiteralPath (Join-Path $root 'scripts\stop.ps1'))) {
        if (Test-Path -LiteralPath (Join-Path $root 'PULSE\scripts\stop.ps1')) { $root = Join-Path $root 'PULSE' }
        else { throw '请把本文件放到原程序“一键启动.bat”旁边，再双击运行。不要放进新建的空文件夹。' }
    }
    foreach ($required in @('backend\monitor\api.py','scripts\start.ps1','scripts\stop.ps1')) {
        if (-not (Test-Path -LiteralPath (Join-Path $root $required))) { throw '目录不是受支持的合约推送安装目录，未修改任何文件。' }
    }
    if (Test-Path -LiteralPath (Join-Path $root '.git')) { throw '这是 Git 源码目录，请通过 Git 更新；本工具只修复安装版。' }
    $runtime = Join-Path $root '.runtime'
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    $log = Join-Path $runtime 'repair-update.log'
    $lock = Join-Path $runtime 'update.lock'
    if (-not $VerifyOnly) {
        # An old crashed lock may be reclaimed, but an active updater is never interrupted.
        $active = @(Get-CimInstance Win32_Process | Where-Object {
            $_.ProcessId -ne $PID -and $_.Name -eq 'powershell.exe' -and
            ([string]$_.CommandLine).IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            ([string]$_.CommandLine -match 'update-worker|update\.ps1.*Apply|repair-update\.ps1')
        })
        if ($active.Count) { throw '已有更新器正在运行。请等待完成后再试，不要重复启动。' }
        if (Test-Path -LiteralPath $lock) {
            $age = (Get-Date) - (Get-Item -LiteralPath $lock).LastWriteTime
            if ($age.TotalMinutes -lt 30) { throw '检测到近期更新任务，请等待原更新结束后重试。超过30分钟且更新器已退出时，本工具会自动修复残留标记。' }
            Remove-Item -LiteralPath $lock -Force
        }
        $handle = [IO.File]::Open($lock,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        $ownedLock = $true
    }
    Write-Host '正在检查公开仓库的最新正式版本（无需登录 GitHub）…'
    $repo = 'cnn-cnn-creatoe/pulse-futures-radar'
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers @{'User-Agent'='PULSE-Repair-Updater'} -TimeoutSec 30
    $tag = [string]$release.tag_name
    if ($release.draft -or $release.prerelease -or $tag -notmatch '^v\d+\.\d+\.\d+$') { throw '仓库没有有效的正式版本。' }
    $temp = Join-Path ([IO.Path]::GetTempPath()) ('pulse-repair-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temp -Force | Out-Null
    $name = "PULSE-Windows-$tag.zip"
    $zip = Join-Path $temp $name
    $hashFile = $zip + '.sha256'
    Write-Host "正在下载 $tag，请等待。原程序在下载期间可以继续运行。"
    $base = "https://github.com/$repo/releases/download/$tag/"
    Invoke-WebRequest -UseBasicParsing -Uri ($base+$name) -OutFile $zip -TimeoutSec 300
    Invoke-WebRequest -UseBasicParsing -Uri ($base+$name+'.sha256') -OutFile $hashFile -TimeoutSec 60
    $sum = (Get-Content -LiteralPath $hashFile -Raw).Trim()
    if ($sum -notmatch '^([0-9a-fA-F]{64})\s+\*?(.+)$') { throw '校验文件格式错误。' }
    $expected = $Matches[1]; $expectedName = $Matches[2]
    if ($expectedName -ne $name -or (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne $expected) { throw '下载包校验失败，未替换原程序。' }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        foreach ($entry in $archive.Entries) {
            $entryName = $entry.FullName.Replace('\','/')
            if (-not $entryName.StartsWith('PULSE/') -or $entryName -match '(^|/)\.\.(/|$)|:' -or $entryName -match '^PULSE/(backend/data|\.runtime|\.venv|backups|research|outputs)(/|$)') { throw '压缩包包含不安全或个人数据路径。' }
        }
    } finally { $archive.Dispose() }
    Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $temp 'payload') -Force
    $payload = Join-Path $temp 'payload\PULSE'
    $manifest = Get-Content -LiteralPath (Join-Path $payload 'release-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.version -ne $tag -or (Get-Content -LiteralPath (Join-Path $payload 'VERSION') -Raw).Trim() -ne $tag) { throw '版本清单不一致。' }
    $actual = @(Get-ChildItem -LiteralPath $payload -Recurse -File | ForEach-Object {$_.FullName.Substring($payload.Length+1).Replace('\','/')} | Sort-Object)
    $listed = @($manifest.files | Sort-Object -Unique)
    if ($actual.Count -ne $listed.Count -or (Compare-Object $actual $listed)) { throw '安装包文件与清单不一致。' }
    ('[{0}] 校验通过，目标版本 {1}，安装目录 {2}' -f (Get-Date),$tag,$root) | Add-Content -LiteralPath $log -Encoding UTF8
    if ($VerifyOnly) { Write-Host "验证通过：$tag。仅检查下载和清单，没有停止或覆盖程序。"; return }
    Copy-Item -LiteralPath (Join-Path $payload 'scripts\update-worker.ps1') -Destination (Join-Path $temp 'pulse-update-worker.ps1')
    Copy-Item -LiteralPath (Join-Path $payload 'scripts\update-cleanup.ps1') -Destination (Join-Path $temp 'pulse-update-cleanup.ps1')
    Write-Host '校验通过，开始关闭、覆盖和重启。设置及信号数据库将保留，请勿关机。'
    [pscustomobject]@{status="stopping";version=$tag;message="正在启动修复安装器";updatedAt=(Get-Date).ToUniversalTime().ToString("o")} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtime "update-status.json") -Encoding UTF8
    $handle.Dispose()
    $worker = Start-Process powershell.exe -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"'+$temp+'\pulse-update-worker.ps1"'),'-ProjectRoot',('"'+$root+'"'),'-PayloadRoot',('"'+$payload+'"'),'-TempRoot',('"'+$temp+'"'),'-ExpectedTag',$tag) -WindowStyle Hidden -PassThru
    $handedOff = $true
    $previous = ''
    while (-not $worker.HasExited) {
        Start-Sleep -Seconds 2
        try {
            $state = Get-Content -LiteralPath (Join-Path $runtime 'update-status.json') -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($state.version -eq $tag -and $state.message -ne $previous) { Write-Host $state.message; $previous=$state.message }
        } catch { }
        $worker.Refresh()
    }
    $state = Get-Content -LiteralPath (Join-Path $runtime 'update-status.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($state.status -ne 'complete' -or $state.version -ne $tag) { throw ('更新没有完成：'+$state.message+'。请查看 .runtime\update.log。') }
    Write-Host "修复更新完成：$tag。原设置和信号记录已保留。" -ForegroundColor Green
} catch {
    Write-Host ('修复更新失败：'+$_.Exception.Message) -ForegroundColor Red
    if ($ownedLock -and -not $handedOff) { $handle.Dispose(); Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue }
    exit 1
}

