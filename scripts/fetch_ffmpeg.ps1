param(
    [string]$Destination,
    [string]$ArchivePath
)

$ErrorActionPreference = 'Stop'

$Version = '8.1.2'
$HelperRevision = '1'
$ArchiveName = "Momento-ffmpeg-helper-$Version-$HelperRevision.zip"
$ExpectedSha256 = 'BB8E4FC7A4E8E3BB5EA4F509BFA49E01BAD1932F8CD1E4399D145D90C080F0B5'
$DownloadUrl = "https://github.com/Vanexia/momento/releases/download/v0.2.4/$ArchiveName"

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
    if ($ArchivePath) {
        $VerifiedArchive = [System.IO.Path]::GetFullPath($ArchivePath)
        if (-not (Test-Path -LiteralPath $VerifiedArchive -PathType Leaf)) {
            throw "The local helper archive was not found: $VerifiedArchive"
        }
    }
    else {
        $VerifiedArchive = Join-Path $WorkDir $ArchiveName
        Write-Host "Downloading Momento FFmpeg helper $Version-$HelperRevision..."
        Invoke-WebRequest -UseBasicParsing -Uri $DownloadUrl -OutFile $VerifiedArchive
    }

    $ActualSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $VerifiedArchive).Hash
    if ($ActualSha256 -ne $ExpectedSha256) {
        throw "FFmpeg helper checksum mismatch. Expected $ExpectedSha256, got $ActualSha256"
    }

    $Extracted = Join-Path $WorkDir 'extracted'
    Expand-Archive -LiteralPath $VerifiedArchive -DestinationPath $Extracted
    $ExpectedFiles = @('ffmpeg.exe', 'ffprobe.exe', 'LICENSE.txt', 'README.txt', 'SHA256SUMS.txt')
    $ActualFiles = Get-ChildItem -LiteralPath $Extracted -File | Select-Object -ExpandProperty Name
    if (@(Compare-Object $ExpectedFiles $ActualFiles).Count -ne 0) {
        throw 'The verified FFmpeg helper archive has an unexpected layout.'
    }
    $ExpectedTools = @{
        'ffmpeg.exe' = 'A53993C4FBFBC3FA9ED201AE03502F053182699B3580C7523DC66D176D0371FC'
        'ffprobe.exe' = 'DD7364CD03D86CB5F91FD028174CB6D5F1B2F3BA2606095676E0596B216A4D4D'
    }
    foreach ($Name in $ExpectedTools.Keys) {
        $ToolPath = Join-Path $Extracted $Name
        $ToolHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ToolPath).Hash
        if ($ToolHash -ne $ExpectedTools[$Name]) {
            throw "$Name did not match the reviewed minimal helper build."
        }
    }

    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    foreach ($Name in $ExpectedFiles) {
        Copy-Item -LiteralPath (Join-Path $Extracted $Name) `
            -Destination (Join-Path $Destination $Name) -Force
    }
    Write-Host "Installed verified Momento FFmpeg helper $Version-$HelperRevision in $Destination"
}
finally {
    if (Test-Path -LiteralPath $WorkDir) {
        $ResolvedWorkDir = [System.IO.Path]::GetFullPath($WorkDir)
        if ($ResolvedWorkDir.StartsWith($TempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $ResolvedWorkDir -Recurse -Force
        }
    }
}
