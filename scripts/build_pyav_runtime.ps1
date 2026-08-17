[CmdletBinding()]
param(
    [string]$ManifestPath,
    [string]$SourceCache,
    [string]$ToolchainRoot,
    [string]$PythonWheelCache,
    [string]$WorkRoot,
    [string]$OutputDir,
    [switch]$ValidateOnly,
    [switch]$SkipReproducibilityCheck
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Set-StrictMode -Version Latest
Import-Module Microsoft.PowerShell.Utility

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not $ManifestPath) { $ManifestPath = Join-Path $RepoRoot "build\pyav_runtime.json" }
if (-not $SourceCache) { $SourceCache = Join-Path $RepoRoot "tmp\corresponding-sources" }
if (-not $ToolchainRoot) { $ToolchainRoot = Join-Path $RepoRoot "tmp\ffmpeg-build-tools\msys64" }
if (-not $PythonWheelCache) { $PythonWheelCache = Join-Path $RepoRoot "tmp\ffmpeg-build-tools\python-build-wheels" }
if (-not $WorkRoot) { $WorkRoot = Join-Path $RepoRoot "tmp\pyav-runtime-build" }
if (-not $OutputDir) { $OutputDir = Join-Path $RepoRoot "dist\pyav-runtime" }

$ManifestPath = [IO.Path]::GetFullPath($ManifestPath)
$SourceCache = [IO.Path]::GetFullPath($SourceCache)
$ToolchainRoot = [IO.Path]::GetFullPath($ToolchainRoot)
$PythonWheelCache = [IO.Path]::GetFullPath($PythonWheelCache)
$WorkRoot = [IO.Path]::GetFullPath($WorkRoot)
$OutputDir = [IO.Path]::GetFullPath($OutputDir)
$Bash = Join-Path $ToolchainRoot "usr\bin\bash.exe"
$ShellBuilder = Join-Path $RepoRoot "scripts\build_pyav_runtime.sh"
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Verifier = Join-Path $RepoRoot "scripts\verify_pyav_runtime.py"
$TmpRoot = [IO.Path]::GetFullPath((Join-Path $RepoRoot "tmp"))

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-PinnedFiles($Items, [string]$CacheRoot, [string]$Kind) {
    foreach ($Item in $Items) {
        $Path = Join-Path $CacheRoot $Item.filename
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
            throw "Missing pinned $Kind input: $Path"
        }
        $Actual = Get-Sha256 $Path
        if ($Actual -ne $Item.sha256.ToLowerInvariant()) {
            throw "Hash mismatch for $($Item.filename): expected $($Item.sha256), got $Actual"
        }
    }
}

function Convert-ToMsysPath([string]$Path) {
    $Full = [IO.Path]::GetFullPath($Path).Replace("\", "/")
    if ($Full -notmatch '^([A-Za-z]):/(.*)$') {
        throw "Cannot convert path to MSYS form: $Path"
    }
    return "/$($Matches[1].ToLowerInvariant())/$($Matches[2])"
}

function Assert-GeneratedPath([string]$Path) {
    $Full = [IO.Path]::GetFullPath($Path)
    $Prefix = $TmpRoot.TrimEnd("\") + "\"
    if (-not $Full.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Generated build path must stay inside ${TmpRoot}: $Full"
    }
}

function Reset-GeneratedDirectory([string]$Path) {
    Assert-GeneratedPath $Path
    $Marker = Join-Path $Path ".momento-pyav-generated"
    if (Test-Path -LiteralPath $Path) {
        if (-not (Test-Path -LiteralPath $Marker -PathType Leaf)) {
            throw "Refusing to clean unmarked directory: $Path"
        }
        $MarkedPath = (Get-Content -LiteralPath $Marker -Raw).Trim()
        if ($MarkedPath -ne [IO.Path]::GetFullPath($Path)) {
            throw "Refusing to clean directory with an invalid marker: $Path"
        }
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
    New-Item -ItemType Directory -Path $Path | Out-Null
    [IO.File]::WriteAllText($Marker, [IO.Path]::GetFullPath($Path), [Text.UTF8Encoding]::new($false))
}

function Import-VsDevEnvironment {
    $VsWhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path -LiteralPath $VsWhere)) { throw "Visual Studio vswhere.exe is missing" }
    $VsRoot = (& $VsWhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath).Trim()
    if (-not $VsRoot) { throw "Visual Studio C++ Build Tools are missing" }
    $VsDevCmd = Join-Path $VsRoot "Common7\Tools\VsDevCmd.bat"
    $Lines = & cmd.exe /d /s /c "`"$VsDevCmd`" -no_logo -arch=amd64 -host_arch=amd64 >nul && set"
    if ($LASTEXITCODE -ne 0) { throw "VsDevCmd.bat failed with exit code $LASTEXITCODE" }
    foreach ($Line in $Lines) {
        $Parts = $Line -split "=", 2
        if ($Parts.Count -eq 2) { [Environment]::SetEnvironmentVariable($Parts[0], $Parts[1], "Process") }
    }
}

foreach ($Required in $ManifestPath, $SourceCache, $Bash, $ShellBuilder, $PythonExe, $Verifier) {
    if (-not (Test-Path -LiteralPath $Required)) { throw "Missing required build path: $Required" }
}
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
if ($Manifest.schema_version -ne 1) { throw "Unsupported PyAV runtime manifest schema" }
Assert-PinnedFiles $Manifest.sources $SourceCache "source"
Assert-PinnedFiles $Manifest.python_build_packages $PythonWheelCache "Python build"

$env:MSYSTEM = "UCRT64"
$env:CHERE_INVOKING = "1"
$VersionScript = @'
#!/usr/bin/env bash
set -euo pipefail
export PATH="/ucrt64/bin:/usr/bin:/bin:$PATH"
printf 'gcc=%s\n' "$(gcc -dumpfullversion)"
printf 'make=%s\n' "$(make --version | head -1 | awk '{print $3}')"
printf 'nasm=%s\n' "$(nasm -v | awk '{print $3}')"
printf 'pkgconf=%s\n' "$(pkg-config --version)"
printf 'cmake=%s\n' "$(cmake --version | head -1 | awk '{print $3}')"
printf 'ninja=%s\n' "$(ninja --version)"
'@
$Observed = @{}
$ProbePath = Join-Path $env:TEMP "momento-pyav-toolchain-probe-$PID.sh"
[IO.File]::WriteAllText($ProbePath, $VersionScript, [Text.UTF8Encoding]::new($false))
try {
    & $Bash (Convert-ToMsysPath $ProbePath) | ForEach-Object {
        $Parts = $_ -split "=", 2
        if ($Parts.Count -eq 2) { $Observed[$Parts[0]] = $Parts[1] }
    }
    if ($LASTEXITCODE -ne 0) { throw "MSYS2 toolchain probe failed" }
} finally {
    Remove-Item -LiteralPath $ProbePath -Force -ErrorAction SilentlyContinue
}
foreach ($Name in "gcc", "make", "nasm", "pkgconf", "cmake", "ninja") {
    $Expected = [string]$Manifest.toolchain.$Name
    if ($Observed[$Name] -ne $Expected) {
        throw "Toolchain mismatch for ${Name}: expected $Expected, got $($Observed[$Name])"
    }
}
$PythonIdentity = & $PythonExe -c "import platform,sys; print(f'{sys.version_info.major}.{sys.version_info.minor}|{platform.architecture()[0]}')"
if ($PythonIdentity.Trim() -ne "3.12|64bit") { throw "PyAV must be built by 64-bit CPython 3.12, got $PythonIdentity" }
Write-Output "PASS: PyAV build inputs and toolchain match the pinned contract"
if ($ValidateOnly) { return }

New-Item -ItemType Directory -Path $WorkRoot -Force | Out-Null
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$ExpectedWheelName = [string]$Manifest.artifact.filename
$FinalTarget = Join-Path $OutputDir $ExpectedWheelName
if (Test-Path -LiteralPath $FinalTarget) { throw "Refusing to overwrite existing wheel: $FinalTarget" }

function Build-One([string]$SlotPath) {
    Reset-GeneratedDirectory $SlotPath
    $NativeWork = Join-Path $SlotPath "native"
    $Prefix = Join-Path $SlotPath "prefix"
    $ArgsFile = Join-Path $SlotPath "ffmpeg-configure.args"
    $ConfigureText = [string]::Join("`n", [string[]]$Manifest.ffmpeg_configure) + "`n"
    [IO.File]::WriteAllText($ArgsFile, $ConfigureText, [Text.UTF8Encoding]::new($false))

    $ShellArgs = @(
        (Convert-ToMsysPath $SourceCache),
        (Convert-ToMsysPath $NativeWork),
        (Convert-ToMsysPath $Prefix),
        (Convert-ToMsysPath $ArgsFile),
        (Convert-ToMsysPath $RepoRoot)
    )
    & $Bash (Convert-ToMsysPath $ShellBuilder) @ShellArgs | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Minimal FFmpeg build failed with exit code $LASTEXITCODE" }
    $BuildVenv = Join-Path $SlotPath "build-venv"
    & $PythonExe -m venv $BuildVenv | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Build virtual environment creation failed" }
    $BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
    $PackageWheels = @($Manifest.python_build_packages | ForEach-Object { Join-Path $PythonWheelCache $_.filename })
    & $BuildPython -m pip install --disable-pip-version-check --no-index --no-deps @PackageWheels | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Pinned Python build-tool installation failed" }

    $PyAvSourceParent = Join-Path $SlotPath "pyav-source"
    New-Item -ItemType Directory -Path $PyAvSourceParent | Out-Null
    & tar.exe -xf (Join-Path $SourceCache "av-17.0.1.tar.gz") -C $PyAvSourceParent | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "PyAV source extraction failed" }
    $PyAvSource = Join-Path $PyAvSourceParent "av-17.0.1"

    $OldSourceDateEpoch = $env:SOURCE_DATE_EPOCH
    $OldPythonHashSeed = $env:PYTHONHASHSEED
    $OldCl = $env:CL
    $OldLink = $env:LINK
    try {
        $env:SOURCE_DATE_EPOCH = [string]$Manifest.toolchain.source_date_epoch
        $env:PYTHONHASHSEED = "0"
        $env:CL = "/Brepro /experimental:deterministic /pathmap:$SlotPath=/momento-pyav-build"
        $env:LINK = "/Brepro"
        Push-Location $PyAvSource
        try {
            $PyAvBuildLog = Join-Path $SlotPath "pyav-build.log"
            & $BuildPython setup.py "--ffmpeg-dir=$Prefix" bdist_wheel --dist-dir (Join-Path $SlotPath "raw-wheel") *> $PyAvBuildLog
            $PyAvBuildExitCode = $LASTEXITCODE
            if ($PyAvBuildExitCode -ne 0) {
                Get-Content -LiteralPath $PyAvBuildLog -Tail 200 | Out-Host
                throw "PyAV wheel build failed with exit code $PyAvBuildExitCode"
            }
            Write-Host "PASS: PyAV wheel compiled"
        } finally {
            Pop-Location
        }
    } finally {
        $env:SOURCE_DATE_EPOCH = $OldSourceDateEpoch
        $env:PYTHONHASHSEED = $OldPythonHashSeed
        $env:CL = $OldCl
        $env:LINK = $OldLink
    }

    $RawWheels = @(Get-ChildItem -LiteralPath (Join-Path $SlotPath "raw-wheel") -Filter "*.whl" -File)
    if ($RawWheels.Count -ne 1) { throw "Expected one raw PyAV wheel, found $($RawWheels.Count)" }
    $RepairedDir = Join-Path $SlotPath "repaired-wheel"
    New-Item -ItemType Directory -Path $RepairedDir | Out-Null
    & $BuildPython -m delvewheel repair --add-path (Join-Path $Prefix "bin") --wheel-dir $RepairedDir $RawWheels[0].FullName | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "delvewheel repair failed" }

    $Repaired = @(Get-ChildItem -LiteralPath $RepairedDir -Filter "*.whl" -File)
    if ($Repaired.Count -ne 1) { throw "Expected one repaired PyAV wheel, found $($Repaired.Count)" }
    $UnpackDir = Join-Path $SlotPath "wheel-unpack"
    & $BuildPython -m wheel unpack $Repaired[0].FullName --dest $UnpackDir | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Wheel unpack failed" }
    $UnpackedRoots = @(Get-ChildItem -LiteralPath $UnpackDir -Directory)
    if ($UnpackedRoots.Count -ne 1) { throw "Expected one unpacked wheel root" }
    $LicenseTarget = Join-Path $UnpackedRoots[0].FullName "av.libs\licenses"
    New-Item -ItemType Directory -Path $LicenseTarget -Force | Out-Null
    Copy-Item -Path (Join-Path $Prefix "licenses\*") -Destination $LicenseTarget -Force
    $Epoch = [DateTimeOffset]::FromUnixTimeSeconds([long]$Manifest.toolchain.source_date_epoch).UtcDateTime
    Get-ChildItem -LiteralPath $UnpackedRoots[0].FullName -Recurse -Force | ForEach-Object { $_.LastWriteTimeUtc = $Epoch }

    $FinalWheelDir = Join-Path $SlotPath "final-wheel"
    New-Item -ItemType Directory -Path $FinalWheelDir | Out-Null
    $OldSourceDateEpoch = $env:SOURCE_DATE_EPOCH
    try {
        $env:SOURCE_DATE_EPOCH = [string]$Manifest.toolchain.source_date_epoch
        & $BuildPython -m wheel pack $UnpackedRoots[0].FullName --dest-dir $FinalWheelDir | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "Wheel repack failed" }
    } finally {
        $env:SOURCE_DATE_EPOCH = $OldSourceDateEpoch
    }
    $BuiltWheels = @(Get-ChildItem -LiteralPath $FinalWheelDir -Filter "*.whl" -File)
    if ($BuiltWheels.Count -ne 1) { throw "Expected one final PyAV wheel, found $($BuiltWheels.Count)" }
    if ($BuiltWheels[0].Name -ne $ExpectedWheelName) {
        throw "Unexpected wheel name $($BuiltWheels[0].Name); expected $ExpectedWheelName"
    }

    $WheelHash = Get-Sha256 $BuiltWheels[0].FullName
    $Effective = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    $Effective.artifact.sha256 = $WheelHash
    $EffectivePath = Join-Path $SlotPath "effective-pyav-runtime.json"
    [IO.File]::WriteAllText($EffectivePath, ($Effective | ConvertTo-Json -Depth 20), [Text.UTF8Encoding]::new($false))
    & $PythonExe $Verifier $BuiltWheels[0].FullName --contract $EffectivePath | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Custom PyAV runtime verification failed" }
    return $BuiltWheels[0].FullName
}

Import-VsDevEnvironment
$Slot = Join-Path $WorkRoot "repro-slot"
$FirstWheel = Build-One $Slot
$ComparisonDir = Join-Path $WorkRoot "comparison"
New-Item -ItemType Directory -Path $ComparisonDir -Force | Out-Null
$FirstCopy = Join-Path $ComparisonDir "first.whl"
Copy-Item -LiteralPath $FirstWheel -Destination $FirstCopy -Force
$FirstHash = Get-Sha256 $FirstCopy

if ($SkipReproducibilityCheck) {
    $SelectedWheel = $FirstCopy
} else {
    $SecondWheel = Build-One $Slot
    $SecondHash = Get-Sha256 $SecondWheel
    if ($FirstHash -ne $SecondHash) {
        throw "Reproducibility failure: first=$FirstHash second=$SecondHash"
    }
    Write-Output "PASS: two clean PyAV builds are byte-for-byte reproducible ($FirstHash)"
    $SelectedWheel = $SecondWheel
}

Copy-Item -LiteralPath $SelectedWheel -Destination $FinalTarget
$FinalHash = Get-Sha256 $FinalTarget
[IO.File]::WriteAllText("$FinalTarget.sha256", "$FinalHash  $ExpectedWheelName`n", [Text.UTF8Encoding]::new($false))
$Size = (Get-Item -LiteralPath $FinalTarget).Length
Write-Output "PASS: custom PyAV wheel $FinalTarget"
Write-Output "SHA256: $FinalHash"
Write-Output "Bytes: $Size"
