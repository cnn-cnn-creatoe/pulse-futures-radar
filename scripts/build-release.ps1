[CmdletBinding()]
param(
    [string]$Version,
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
if (-not $Version) { $Version = (Get-Content -LiteralPath (Join-Path $projectRoot 'VERSION') -Raw).Trim() }
if ($Version -notmatch '^v\d+\.\d+\.\d+$') { throw '版本号必须使用 v1.2.3 格式。' }
if ((Get-Content -LiteralPath (Join-Path $projectRoot 'VERSION') -Raw).Trim() -ne $Version) {
    throw '参数版本号与 VERSION 文件不一致。'
}
$releaseConfigPath = Join-Path $projectRoot 'release.json'
if (-not (Test-Path -LiteralPath $releaseConfigPath)) { throw '缺少 release.json。' }
try { $releaseConfig = Get-Content -LiteralPath $releaseConfigPath -Raw | ConvertFrom-Json }
catch { throw 'release.json 无法读取。' }
if ([string]$releaseConfig.version -ne $Version -or
    [string]$releaseConfig.release_channel -ne 'stable') {
    throw 'release.json 与当前稳定版本不一致。'
}
$repository = [string]$releaseConfig.repository
if ($repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') { throw 'release.json 的 repository 必须是 owner/repository。' }
$expectedAssetName = 'PULSE-Windows-{0}.zip' -f $Version
if ([string]$releaseConfig.asset_name -ne $expectedAssetName) { throw 'release.json 的 asset_name 与版本号不一致。' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $projectRoot 'release' }
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null

$tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\')
$stageRoot = Join-Path $tempBase ('pulse-release-{0}' -f [guid]::NewGuid().ToString('N'))
$payloadRoot = Join-Path $stageRoot 'PULSE'
$archive = Join-Path $outputRoot ('PULSE-Windows-{0}.zip' -f $Version)
$checksumFile = '{0}.sha256' -f $archive

function Get-Sha256([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $algorithm = [System.Security.Cryptography.SHA256]::Create()
        try { return ([System.BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '') }
        finally { $algorithm.Dispose() }
    }
    finally { $stream.Dispose() }
}

try {
    New-Item -ItemType Directory -Path $payloadRoot -Force | Out-Null
    $excludedDirectories = @(
        (Join-Path $projectRoot '.git'), (Join-Path $projectRoot '.runtime'), (Join-Path $projectRoot '.venv'),
        (Join-Path $projectRoot 'release'), (Join-Path $projectRoot '回测结果'),
        (Join-Path $projectRoot 'backend\data'), (Join-Path $projectRoot 'dashboard\node_modules'),
        (Join-Path $projectRoot 'dashboard\dist'), (Join-Path $projectRoot 'dashboard\.next'),
        (Join-Path $projectRoot 'dashboard\.vinext'), (Join-Path $projectRoot 'dashboard\.wrangler'),
        (Join-Path $projectRoot 'dashboard\.openai'),
        '.git', '__pycache__', '.pytest_cache'
    )
    $excludedFiles = @(
        '*.sqlite', '*.sqlite-wal', '*.sqlite-shm', '*.sqlite3', '*.sqlite3-wal', '*.sqlite3-shm',
        '*.db', '*.db-wal', '*.db-shm', '*.dpapi', '*.log', '*.pid', '*.zip', '*.tar.gz',
        '*.sha256', '*.tsbuildinfo', '.env', '.env.*', '打开在线演示.url'
    )
    $robocopyArgs = @($projectRoot, $payloadRoot, '/E', '/COPY:DAT', '/DCOPY:DAT', '/R:1', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/XD') + $excludedDirectories + @('/XF') + $excludedFiles
    & robocopy.exe @robocopyArgs | Out-Null
    if ($LASTEXITCODE -gt 7) { throw ('准备发布文件失败（robocopy 退出码 {0}）。' -f $LASTEXITCODE) }

    foreach ($forbidden in @('backend\data', '.runtime', '.venv', 'dashboard\node_modules', 'dashboard\dist', 'dashboard\.next', 'dashboard\.vinext', 'dashboard\.openai')) {
        if (Test-Path -LiteralPath (Join-Path $payloadRoot $forbidden)) { throw ('发布包安全检查失败，包含了 {0}。' -f $forbidden) }
    }
    if (Get-ChildItem -LiteralPath $payloadRoot -Directory -Recurse -Force | Where-Object { $_.Name -eq '.git' } | Select-Object -First 1) {
        throw '发布包安全检查失败，发现嵌套的 Git 元数据。'
    }
    if (Get-ChildItem -LiteralPath $payloadRoot -File -Recurse | Where-Object { $_.Name -match '(?i)(\.(?:sqlite|sqlite3|db)(?:-wal|-shm)?$|\.dpapi$|\.pid$|^\.env(?:\.|$))' } | Select-Object -First 1) {
        throw '发布包安全检查失败，发现数据库、密钥或环境配置文件。'
    }

    # The updater uses this allow-list to remove program files that disappear
    # in a later release, while its preservation rules protect all local data.
    $payloadPrefixLength = $payloadRoot.Length + 1
    $manifestFiles = @(
        Get-ChildItem -LiteralPath $payloadRoot -File -Recurse | ForEach-Object {
            $_.FullName.Substring($payloadPrefixLength).Replace('\', '/')
        }
        'release-manifest.json'
    ) | Sort-Object -Unique
    [pscustomobject]@{ version = $Version; files = @($manifestFiles) } |
        ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $payloadRoot 'release-manifest.json') -Encoding UTF8

    if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
    if (Test-Path -LiteralPath $checksumFile) { Remove-Item -LiteralPath $checksumFile -Force }
    Compress-Archive -LiteralPath $payloadRoot -DestinationPath $archive -CompressionLevel Optimal
    $hash = Get-Sha256 $archive
    ('{0}  {1}' -f $hash.ToLowerInvariant(), (Split-Path -Leaf $archive)) |
        Set-Content -LiteralPath $checksumFile -Encoding ASCII
    Write-Host ('发布包：{0}' -f $archive) -ForegroundColor Green
    Write-Host ('校验文件：{0}' -f $checksumFile) -ForegroundColor Green
    Write-Host ('SHA256：{0}' -f $hash) -ForegroundColor Green
}
finally {
    $fullStage = [System.IO.Path]::GetFullPath($stageRoot)
    if ($fullStage.StartsWith(($tempBase + '\'), [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $fullStage)) {
        Remove-Item -LiteralPath $fullStage -Recurse -Force -ErrorAction SilentlyContinue
    }
}
