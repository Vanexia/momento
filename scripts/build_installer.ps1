[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$isccCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
)
$iscc = $isccCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not (Test-Path -LiteralPath $python)) {
    throw "The project Python environment is missing."
}
if (-not $iscc) {
    throw "ISCC.exe was not found. Install Inno Setup 6 before building."
}
if ($env:MOMENTO_INCLUDE_YOUTUBE_OAUTH -eq "1") {
    throw "This script creates the standard public build and will not include an OAuth identity."
}
$inno = Get-ChildItem "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall" |
    Get-ItemProperty |
    Where-Object { $_.DisplayName -eq "Inno Setup version 6.7.3" } |
    Select-Object -First 1
if (-not $inno -or $inno.DisplayVersion -ne "6.7.3") {
    throw "The public release requires Inno Setup 6.7.3."
}

$running = Get-Process -Name "Momento" -ErrorAction SilentlyContinue
if ($running) {
    throw "Quit Momento from its tray menu before building. A recording must never be force-stopped."
}

Push-Location $root
try {
    $dirty = git status --porcelain --untracked-files=normal
    if ($dirty) {
        throw "Commit or remove working-tree changes before creating a public source archive."
    }

    $dist = Join-Path $root "dist"
    $bundle = Join-Path $dist "Momento"
    $sourceDir = Join-Path $dist "source"
    $installerDir = Join-Path $dist "installer"
    $work = Join-Path $root "build\pyinstaller_work\public"
    foreach ($path in @($bundle, $sourceDir, $installerDir, $work)) {
        $resolved = [System.IO.Path]::GetFullPath($path)
        if (-not $resolved.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean a path outside the project: $resolved"
        }
        if (Test-Path -LiteralPath $resolved) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
    New-Item -ItemType Directory -Path $sourceDir -Force | Out-Null

    & $python "tests\smoke_first_run.py"
    if ($LASTEXITCODE -ne 0) { throw "Fresh-user checks failed." }
    & $python "tests\smoke_welcome_setup.py"
    if ($LASTEXITCODE -ne 0) { throw "First-run wizard checks failed." }
    & $python "tests\smoke_installer_contract.py"
    if ($LASTEXITCODE -ne 0) { throw "Installer contract checks failed." }
    & $python "tests\smoke_encoder_portability.py"
    if ($LASTEXITCODE -ne 0) { throw "Encoder portability checks failed." }
    & $python "tests\smoke_release_environment.py"
    if ($LASTEXITCODE -ne 0) { throw "The release environment does not match its lock." }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw "The release environment has dependency conflicts." }

    $expectedTools = @{
        "resources\ffmpeg\ffmpeg.exe" = "1326DDE4C84FF1F96FE6B8916C5BED29E163E9B5DCCF995F6F3DB069D143EC5E"
        "resources\ffmpeg\ffprobe.exe" = "B49CCC7C6547B141AD5A2F6EC69CC04323D7133D7704D70B331B904C63EECB07"
    }
    foreach ($relative in $expectedTools.Keys) {
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $root $relative)).Hash
        if ($actual -ne $expectedTools[$relative]) {
            throw "$relative does not match the pinned FFmpeg 8.1.2 release."
        }
    }

    & $python -m PyInstaller "build\pyinstaller.spec" --noconfirm --clean --workpath $work
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

    $manifest = Join-Path $bundle "RELEASE_MANIFEST.txt"
    $manifestLines = @(
        "Momento 0.2.1 release manifest",
        "Git commit: $(git rev-parse HEAD)",
        "Format: SHA256  bytes  relative path",
        ""
    )
    $manifestLines += Get-ChildItem -LiteralPath $bundle -Recurse -File |
        Where-Object { $_.FullName -ne $manifest } |
        Sort-Object FullName |
        ForEach-Object {
            $relative = [System.IO.Path]::GetRelativePath($bundle, $_.FullName)
            $fileHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash
            "$fileHash  $($_.Length)  $relative"
        }
    Set-Content -LiteralPath $manifest -Encoding utf8 -Value $manifestLines

    # Personal-data and private-runtime scan of public source and bundle.
    & $python "tests\smoke_public_release.py"
    if ($LASTEXITCODE -ne 0) { throw "The public-release privacy scan failed." }

    $sourceArchive = Join-Path $sourceDir "Momento-0.2.1-source.zip"
    & git archive --format=zip --output=$sourceArchive HEAD
    if ($LASTEXITCODE -ne 0) { throw "Could not create the corresponding source archive." }
    & $python "tests\smoke_source_archive.py" $sourceArchive
    if ($LASTEXITCODE -ne 0) { throw "The corresponding source privacy scan failed." }

    $ffmpegSource = Join-Path $sourceDir "ffmpeg-8.1.2.tar.xz"
    Invoke-WebRequest -UseBasicParsing -Uri "https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz" -OutFile $ffmpegSource
    $ffmpegSourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ffmpegSource).Hash
    if ($ffmpegSourceHash -ne "464BEB5E7BF0C311E68B45AE2F04E9CC2AF88851ABB4082231742A74D97B524C") {
        throw "The FFmpeg corresponding-source archive failed its SHA-256 check."
    }

    & $iscc "build\installer.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }

    $installer = Join-Path $installerDir "MomentoSetup-0.2.1.exe"
    if (-not (Test-Path -LiteralPath $installer)) {
        throw "The installer was not produced."
    }
    & "tests\smoke_installed_release.ps1" -InstallerPath $installer
    if ($LASTEXITCODE -ne 0) { throw "Installed-release checks failed." }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash
    Set-Content -LiteralPath "$installer.sha256" -Encoding ascii -Value "$hash  MomentoSetup-0.2.1.exe"
    Write-Host "Installer: $installer"
    Write-Host "SHA256:   $hash"
}
finally {
    Pop-Location
}
