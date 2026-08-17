param(
    [string]$Destination
)

$ErrorActionPreference = 'Stop'

$Version = '8.1.2'
$ArchiveName = "ffmpeg-$Version-essentials_build.zip"
$ExpectedSha256 = 'DB580001CAA24AC104C8CB856CD113A87B0A443F7BDF47D8C12B1D740584A2EC'
$DownloadUrl = "https://www.gyan.dev/ffmpeg/builds/packages/$ArchiveName"

$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'momento\__main__.py'))) {
    throw "Could not locate the Momento project root from $PSScriptRoot"
}

if (-not $Destination) {
    $Destination = Join-Path $ProjectRoot 'resources\ffmpeg'
}
$Destination = [System.IO.Path]::GetFullPath($Destination)

$TempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$WorkDir = [System.IO.Path]::GetFullPath(
    (Join-Path $TempRoot "momento-ffmpeg-$([guid]::NewGuid().ToString('N'))")
)
if (-not $WorkDir.StartsWith($TempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use a temporary directory outside $TempRoot"
}

New-Item -ItemType Directory -Path $WorkDir | Out-Null
try {
    $ArchivePath = Join-Path $WorkDir $ArchiveName
    Write-Host "Downloading FFmpeg $Version..."
    Invoke-WebRequest -UseBasicParsing -Uri $DownloadUrl -OutFile $ArchivePath

    $ActualSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ArchivePath).Hash
    if ($ActualSha256 -ne $ExpectedSha256) {
        throw "FFmpeg checksum mismatch. Expected $ExpectedSha256, got $ActualSha256"
    }

    $Extracted = Join-Path $WorkDir 'extracted'
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $Extracted
    $Ffmpeg = Get-ChildItem -LiteralPath $Extracted -Recurse -File -Filter 'ffmpeg.exe' |
        Select-Object -First 1
    if ($null -eq $Ffmpeg) {
        throw 'The verified FFmpeg archive did not contain ffmpeg.exe'
    }
    $Ffprobe = Join-Path $Ffmpeg.Directory.FullName 'ffprobe.exe'
    if (-not (Test-Path -LiteralPath $Ffprobe)) {
        throw 'The verified FFmpeg archive did not contain ffprobe.exe'
    }

    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Copy-Item -LiteralPath $Ffmpeg.FullName -Destination (Join-Path $Destination 'ffmpeg.exe') -Force
    Copy-Item -LiteralPath $Ffprobe -Destination (Join-Path $Destination 'ffprobe.exe') -Force
    Write-Host "Installed verified FFmpeg $Version tools in $Destination"
}
finally {
    if (Test-Path -LiteralPath $WorkDir) {
        $ResolvedWorkDir = [System.IO.Path]::GetFullPath($WorkDir)
        if ($ResolvedWorkDir.StartsWith($TempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $ResolvedWorkDir -Recurse -Force
        }
    }
}
