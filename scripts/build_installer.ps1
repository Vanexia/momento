[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$bootstrapPython = Join-Path $root ".venv\Scripts\python.exe"
$releaseEnv = Join-Path $root "build\release_env"
$python = Join-Path $releaseEnv "Scripts\python.exe"
$pyavContractPath = Join-Path $root "build\pyav_runtime.json"
$pyavContract = Get-Content -LiteralPath $pyavContractPath -Raw | ConvertFrom-Json
$pyavWheel = Join-Path $root (Join-Path "dist\pyav-runtime" $pyavContract.artifact.filename)
$isccCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
)
$iscc = $isccCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not (Test-Path -LiteralPath $bootstrapPython)) {
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

    $resolvedReleaseEnv = [System.IO.Path]::GetFullPath($releaseEnv)
    $resolvedBuildRoot = [System.IO.Path]::GetFullPath((Join-Path $root "build")).TrimEnd('\') + '\'
    if (-not $resolvedReleaseEnv.StartsWith(
        $resolvedBuildRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to recreate a release environment outside the build directory."
    }
    & $bootstrapPython -m venv --clear $releaseEnv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the isolated release environment." }
    if (-not (Test-Path -LiteralPath $pyavWheel -PathType Leaf)) {
        throw "The verified custom PyAV wheel is missing. Run scripts\build_pyav_runtime.ps1 first."
    }
    $pyavHash = (Get-FileHash -LiteralPath $pyavWheel -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($pyavHash -ne $pyavContract.artifact.sha256) {
        throw "The custom PyAV wheel does not match build\pyav_runtime.json."
    }
    & $python -m pip install --disable-pip-version-check --no-input `
        --no-index --no-deps $pyavWheel
    if ($LASTEXITCODE -ne 0) { throw "Could not install the verified custom PyAV wheel." }
    & $python -m pip install --disable-pip-version-check --no-input `
        --only-binary=:all: --require-hashes `
        --requirement "requirements-release-hashed.txt"
    if ($LASTEXITCODE -ne 0) { throw "Could not install the hash-locked release wheels." }
    & $python -m pip install --disable-pip-version-check --no-input `
        --no-deps --no-build-isolation $root
    if ($LASTEXITCODE -ne 0) { throw "Could not install Momento into the release environment." }

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
    & $python "tests\smoke_update_metadata.py"
    if ($LASTEXITCODE -ne 0) { throw "Signed update metadata checks failed." }
    & $python "tests\smoke_update_client.py"
    if ($LASTEXITCODE -ne 0) { throw "Update client checks failed." }
    & $python "tests\smoke_update_attempts.py"
    if ($LASTEXITCODE -ne 0) { throw "Update attempt-state checks failed." }
    & $python "tests\smoke_update_lifecycle.py"
    if ($LASTEXITCODE -ne 0) { throw "Update lifecycle checks failed." }
    & $python "tests\smoke_update_service.py"
    if ($LASTEXITCODE -ne 0) { throw "Update service checks failed." }
    & $python "tests\smoke_update_handoff.py"
    if ($LASTEXITCODE -ne 0) { throw "Update handoff checks failed." }
    & $python "tests\smoke_update_runtime.py"
    if ($LASTEXITCODE -ne 0) { throw "Update runtime checks failed." }
    & $python "tests\smoke_single_instance.py"
    if ($LASTEXITCODE -ne 0) { throw "Update single-instance checks failed." }
    & $python "tests\smoke_update_release_tools.py"
    if ($LASTEXITCODE -ne 0) { throw "Update release tooling checks failed." }
    & $python "tests\smoke_pyav_runtime_contract.py" $pyavWheel
    if ($LASTEXITCODE -ne 0) { throw "The minimized PyAV runtime contract failed." }
    & $python "tests\smoke_encoder_portability.py"
    if ($LASTEXITCODE -ne 0) { throw "Encoder portability checks failed." }
    & $python "tests\check_ffmpeg.py"
    if ($LASTEXITCODE -ne 0) { throw "The bundled FFmpeg helper contract failed." }
    & $python "tests\smoke_ffmpeg_helper.py" "resources\ffmpeg"
    if ($LASTEXITCODE -ne 0) { throw "The bundled FFmpeg helper workflow failed." }
    & $python "tests\smoke_corresponding_source.py"
    if ($LASTEXITCODE -ne 0) { throw "The corresponding-source manifest failed." }
    & $python "tests\smoke_release_environment.py" --strict
    if ($LASTEXITCODE -ne 0) { throw "The release environment does not match its lock." }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw "The release environment has dependency conflicts." }

    $expectedTools = @{
        "resources\ffmpeg\ffmpeg.exe" = "A53993C4FBFBC3FA9ED201AE03502F053182699B3580C7523DC66D176D0371FC"
        "resources\ffmpeg\ffprobe.exe" = "DD7364CD03D86CB5F91FD028174CB6D5F1B2F3BA2606095676E0596B216A4D4D"
    }
    foreach ($relative in $expectedTools.Keys) {
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $root $relative)).Hash
        if ($actual -ne $expectedTools[$relative]) {
            throw "$relative does not match the pinned FFmpeg 8.1.2 release."
        }
    }

    & $python -m PyInstaller "build\pyinstaller.spec" --noconfirm --clean --workpath $work
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

    $forbiddenBundleFiles = Get-ChildItem -LiteralPath $bundle -Recurse -File |
        Where-Object {
            $_.Name -match '(?i)^(?:cv2.*|opencv_videoio_ffmpeg.*|Qt6Pdf\.dll|qpdf\.dll)$'
        }
    if ($forbiddenBundleFiles) {
        $names = ($forbiddenBundleFiles.FullName -join ', ')
        throw "The bundle contains an unused OpenCV or Qt PDF runtime: $names"
    }

    $manifest = Join-Path $bundle "RELEASE_MANIFEST.txt"
    $manifestLines = @(
        "Momento 0.2.2 release manifest",
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
    & $python "tests\smoke_git_history_privacy.py"
    if ($LASTEXITCODE -ne 0) { throw "The Git-history privacy scan failed." }

    $sourceArchive = Join-Path $sourceDir "Momento-0.2.2-source.zip"
    & git archive --format=zip --output=$sourceArchive HEAD
    if ($LASTEXITCODE -ne 0) { throw "Could not create the corresponding source archive." }
    & $python "tests\smoke_source_archive.py" $sourceArchive
    if ($LASTEXITCODE -ne 0) { throw "The corresponding source privacy scan failed." }

    $thirdPartySource = Join-Path $sourceDir "Momento-0.2.2-third-party-source.zip"
    & $python "scripts\build_corresponding_source.py" --output $thirdPartySource
    if ($LASTEXITCODE -ne 0) { throw "Could not build the third-party source bundle." }
    & $python "tests\smoke_corresponding_source.py" $thirdPartySource
    if ($LASTEXITCODE -ne 0) { throw "The third-party source bundle failed verification." }

    $helperArchive = Join-Path $sourceDir "Momento-ffmpeg-helper-8.1.2-1.zip"
    & $python "scripts\package_ffmpeg_helper.py" "resources\ffmpeg" $helperArchive
    if ($LASTEXITCODE -ne 0) { throw "Could not package the FFmpeg helper." }
    $helperHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $helperArchive).Hash
    if ($helperHash -ne "BB8E4FC7A4E8E3BB5EA4F509BFA49E01BAD1932F8CD1E4399D145D90C080F0B5") {
        throw "The packaged FFmpeg helper does not match the reviewed build."
    }

    & $iscc "build\installer.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }

    $installer = Join-Path $installerDir "MomentoSetup-0.2.2.exe"
    if (-not (Test-Path -LiteralPath $installer)) {
        throw "The installer was not produced."
    }
    & "tests\smoke_installed_release.ps1" -InstallerPath $installer
    if ($LASTEXITCODE -ne 0) { throw "Installed-release checks failed." }

    $publishedAt = [DateTime]::UtcNow.ToString(
        "yyyy-MM-dd'T'HH:mm:ss'Z'",
        [Globalization.CultureInfo]::InvariantCulture
    )
    $expiresAt = [DateTime]::UtcNow.AddDays(180).ToString(
        "yyyy-MM-dd'T'HH:mm:ss'Z'",
        [Globalization.CultureInfo]::InvariantCulture
    )
    # Stable SemVer components map to a monotonic integer while leaving ample
    # room for future minor and patch releases.
    $metadataVersion = [Int64](0 * 1000000000000 + 2 * 1000000 + 2)
    & $python "scripts\build_update_metadata.py" `
        --installer $installer `
        --version "0.2.2" `
        --minimum-updater-version "0.2.2" `
        --metadata-version $metadataVersion `
        --published-at $publishedAt `
        --expires-at $expiresAt `
        --output-dir $installerDir
    if ($LASTEXITCODE -ne 0) { throw "Could not sign the update release metadata." }
    $updateMetadata = Join-Path $installerDir "Momento-update.json"
    $updateSignature = Join-Path $installerDir "Momento-update.json.sig"
    if (-not (Test-Path -LiteralPath $updateMetadata) -or
        -not (Test-Path -LiteralPath $updateSignature)) {
        throw "The signed update release assets were not produced."
    }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash
    Set-Content -LiteralPath "$installer.sha256" -Encoding ascii -Value "$hash  MomentoSetup-0.2.2.exe"
    $checksumFile = Join-Path $dist "SHA256SUMS-0.2.2.txt"
    $checksumLines = @(
        $installer,
        $sourceArchive,
        $thirdPartySource,
        $helperArchive,
        $updateMetadata,
        $updateSignature
    ) | ForEach-Object {
        $assetHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash.ToLowerInvariant()
        "$assetHash  $([System.IO.Path]::GetFileName($_))"
    }
    Set-Content -LiteralPath $checksumFile -Encoding ascii -Value $checksumLines
    Write-Host "Installer: $installer"
    Write-Host "SHA256:   $hash"
    Write-Host "Checksums: $checksumFile"
}
finally {
    if (Test-Path -LiteralPath $releaseEnv) {
        try {
            $resolvedReleaseEnv = [System.IO.Path]::GetFullPath($releaseEnv)
            $resolvedBuildRoot = [System.IO.Path]::GetFullPath(
                (Join-Path $root "build")
            ).TrimEnd('\') + '\'
            if (-not $resolvedReleaseEnv.StartsWith(
                $resolvedBuildRoot,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                throw "Refusing to remove a release environment outside the build directory."
            }
            Remove-Item -LiteralPath $resolvedReleaseEnv -Recurse -Force
        }
        catch {
            Write-Warning "Could not remove the temporary release environment: $_"
        }
    }
    Pop-Location
}
