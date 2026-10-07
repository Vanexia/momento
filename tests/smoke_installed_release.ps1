[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath
)

$ErrorActionPreference = "Stop"
$installer = (Resolve-Path -LiteralPath $InstallerPath).Path
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$mockPython = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"
$mockWindow = Join-Path $repoRoot "tests\mock_game_window.py"
$testRoot = Join-Path $env:TEMP ("MementosInstallerTest-" + [guid]::NewGuid().ToString("N"))
$installDir = Join-Path $testRoot "Installed"
$profileRoot = Join-Path $testRoot "Profile"
$appData = Join-Path $profileRoot "AppData\Roaming"
$localAppData = Join-Path $profileRoot "AppData\Local"
$recordings = Join-Path $profileRoot "Recordings"
New-Item -ItemType Directory -Path $appData, $localAppData, $recordings -Force | Out-Null

$oldAppData = $env:APPDATA
$oldLocalAppData = $env:LOCALAPPDATA
$oldUserProfile = $env:USERPROFILE
$oldPath = $env:PATH
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$oldRunValues = @{}
foreach ($brand in @("Momento", "Mementos")) {
    $oldRunValues[$brand] = Get-ItemPropertyValue -Path $runKey -Name $brand -ErrorAction SilentlyContinue
}
$process = $null
$target = $null
$uninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8C8A50E4-FCF2-4E1E-9B9C-046B9ED5F3AA}_is1"
$uninstallKeyNative = "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8C8A50E4-FCF2-4E1E-9B9C-046B9ED5F3AA}_is1"
$uninstallBackup = Join-Path $testRoot "existing-uninstall.reg"
$hadUninstallKey = Test-Path -LiteralPath $uninstallKey
$startMenuGroup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Mementos"
$desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Mementos.lnk"
$hadDesktopShortcut = Test-Path -LiteralPath $desktopShortcut
$legacyStartMenuGroup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Momento"
$legacyShortcut = Join-Path $legacyStartMenuGroup "Momento.lnk"
$legacyDesktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Momento.lnk"
$hadLegacyDesktopShortcut = Test-Path -LiteralPath $legacyDesktopShortcut
$shortcutBackups = @()
foreach ($shortcutPath in @(
    (Join-Path $startMenuGroup "Mementos.lnk"), $desktopShortcut,
    $legacyShortcut, $legacyDesktopShortcut
)) {
    $shortcutBackups += @{
        Path = $shortcutPath
        Backup = Join-Path $testRoot ("shortcut-" + $shortcutBackups.Count + ".lnk")
        Existed = Test-Path -LiteralPath $shortcutPath
        ParentExisted = Test-Path -LiteralPath (Split-Path -Parent $shortcutPath)
    }
}

if (-not (Test-Path -LiteralPath $mockPython) -or -not (Test-Path -LiteralPath $mockWindow)) {
    throw "The mock-window test harness is incomplete."
}
if ($hadUninstallKey) {
    & reg.exe export $uninstallKeyNative $uninstallBackup /y | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not back up the existing uninstall registration." }
}
foreach ($item in $shortcutBackups) {
    if ($item.Existed) {
        Copy-Item -LiteralPath $item.Path -Destination $item.Backup -Force
    }
}

function Invoke-Installer([switch]$ExpectBlocked, [switch]$MigrateAutostart) {
    $arguments = @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
        ('/DIR="' + $installDir + '"')
    )
    if (-not $MigrateAutostart) { $arguments += "/NOMIGRATEAUTOSTART" }
    $setup = Start-Process -FilePath $installer -ArgumentList $arguments -PassThru -Wait -WindowStyle Hidden
    if ($ExpectBlocked) {
        if ($setup.ExitCode -eq 0) {
            throw "Setup did not block while Mementos held its installer mutex."
        }
    }
    elseif ($setup.ExitCode -ne 0) {
        throw "Setup failed with exit code $($setup.ExitCode)."
    }
}

function Assert-IsolatedAppData {
    # Inno uses SHGetFolderPath, which is not guaranteed to follow APPDATA on
    # every Windows account. Never run the purge test against the real profile.
    $probe = & (Join-Path $repoRoot ".venv\Scripts\python.exe") -c @'
import ctypes
buffer = ctypes.create_unicode_buffer(32768)
result = ctypes.windll.shell32.SHGetFolderPathW(None, 26 | 0x4000, None, 0, buffer)
if result != 0:
    raise SystemExit(1)
print(buffer.value)
'@
    if ($LASTEXITCODE -ne 0 -or
        [IO.Path]::GetFullPath($probe).TrimEnd('\') -ne
        [IO.Path]::GetFullPath($appData).TrimEnd('\')) {
        throw "Windows AppData is not isolated; refusing to run the installed state tests."
    }
}

try {
    Invoke-Installer
    $exe = Join-Path $installDir "Mementos.exe"
    $uninstaller = Join-Path $installDir "unins000.exe"
    if (Test-Path -LiteralPath (Join-Path $installDir "Momento.exe")) {
        throw "Fresh installation unexpectedly created the legacy executable alias."
    }
    foreach ($required in @(
        $exe,
        $uninstaller,
        (Join-Path $installDir "LICENSE"),
        (Join-Path $installDir "THIRD_PARTY_NOTICES.txt"),
        (Join-Path $installDir "BUILD_INFO.txt"),
        (Join-Path $installDir "SOURCE_OFFER.txt"),
        (Join-Path $installDir "_internal\licenses\FFmpeg\LICENSE.txt"),
        (Join-Path $installDir "_internal\licenses\FFmpeg\README.txt"),
        (Join-Path $installDir "_internal\licenses\FFmpeg\SHA256SUMS.txt"),
        (Join-Path $installDir "_internal\resources\fonts\OFL.txt")
    )) {
        if (-not (Test-Path -LiteralPath $required)) {
            throw "Installed release is missing: $required"
        }
    }

    $registration = Get-ItemProperty -LiteralPath $uninstallKey
    if ([System.IO.Path]::GetFullPath($registration.InstallLocation).TrimEnd('\') -ne
        [System.IO.Path]::GetFullPath($installDir).TrimEnd('\')) {
        throw "Uninstall registration points outside the test installation."
    }
    $startMenuShortcut = Join-Path $startMenuGroup "Mementos.lnk"
    if (-not (Test-Path -LiteralPath $startMenuShortcut)) {
        throw "Setup did not create the Start Menu shortcut."
    }
    $shell = New-Object -ComObject WScript.Shell
    try {
        $shortcut = $shell.CreateShortcut($startMenuShortcut)
        if ([System.IO.Path]::GetFullPath($shortcut.TargetPath) -ne
            [System.IO.Path]::GetFullPath($exe)) {
            throw "The Start Menu shortcut does not target the installed executable."
        }
    }
    finally {
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
    }
    if (-not $hadDesktopShortcut -and -not $hadLegacyDesktopShortcut -and
        (Test-Path -LiteralPath $desktopShortcut)) {
        throw "Setup created the optional desktop shortcut without being asked."
    }

    $env:APPDATA = $appData
    $env:LOCALAPPDATA = $localAppData
    $env:USERPROFILE = $profileRoot
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    Assert-IsolatedAppData
    foreach ($tool in @("ffmpeg.exe", "ffprobe.exe")) {
        $toolPath = Join-Path $installDir "_internal\resources\ffmpeg\$tool"
        & $toolPath -version *> $null
        if ($LASTEXITCODE -ne 0) { throw "$tool failed outside the development PATH." }
    }

    $process = Start-Process -FilePath $exe -WorkingDirectory $testRoot -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 5
    if ($process.HasExited) {
        throw "The installed application exited during fresh-profile startup."
    }
    $config = Join-Path $appData "Mementos\config.json"
    if (-not (Test-Path -LiteralPath $config)) {
        throw "Fresh-profile startup did not create resumable setup state."
    }
    $json = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
    if ($json.setup_complete -ne $false) {
        throw "Setup was marked complete without the user finishing it."
    }

    Invoke-Installer -ExpectBlocked
    Stop-Process -Id $process.Id -Force
    $process.WaitForExit()
    $process = $null

    $json.setup_complete = $true
    $json.start_monitoring_on_launch = $true
    $json.output_folder = $recordings
    $json.known_games = @("pythonw.exe")
    $json.disabled_games = @()
    $json.mic_device = ""
    $json.system_audio_device = ""
    $json.framerate = 60
    $json.framerate_auto = $false
    $json.quality_preset = "custom"
    $json.custom_bitrate_kbps = 16000
    $json.show_recording_started_toast = $false
    $json.show_recording_saved_toast = $false
    $json.show_failure_toast = $false
    $json | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $config -Encoding utf8
    $process = Start-Process -FilePath $exe -ArgumentList "--show" -WorkingDirectory $testRoot -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 3
    if ($process.HasExited) {
        throw "The installed editor exited when opened without YouTube credentials."
    }
    $target = Start-Process -FilePath $mockPython -ArgumentList ('"' + $mockWindow + '"') -PassThru -WindowStyle Hidden

    $recording = $null
    $recordingDeadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $recordingDeadline) {
        if ($process.HasExited) { throw "Mementos exited while detecting the mock game." }
        $recording = Get-ChildItem -LiteralPath $recordings -Filter "*.mkv" -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
        if ($recording) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $recording) { throw "The installed app did not begin recording the mock game." }
    Start-Sleep -Seconds 6
    if (-not $target.HasExited) {
        [void]$target.CloseMainWindow()
        if (-not $target.WaitForExit(5000)) { Stop-Process -Id $target.Id -Force }
    }
    $target = $null

    $log = Join-Path $appData "Mementos\logs\mementos.log"
    $finalizeDeadline = [DateTime]::UtcNow.AddSeconds(30)
    $finalized = $false
    while ([DateTime]::UtcNow -lt $finalizeDeadline) {
        if ($process.HasExited) { throw "Mementos exited while finalizing the mock recording." }
        if ((Test-Path -LiteralPath $log) -and
            (Select-String -LiteralPath $log -SimpleMatch "finalised" -Quiet)) {
            $finalized = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $finalized) { throw "The mock recording did not finalize cleanly." }

    $ffprobe = Join-Path $installDir "_internal\resources\ffmpeg\ffprobe.exe"
    $probeJson = & $ffprobe -v error -select_streams v:0 -count_frames `
        -show_entries stream=codec_name,width,height,avg_frame_rate,nb_read_frames `
        -of json $recording.FullName
    if ($LASTEXITCODE -ne 0) { throw "Bundled ffprobe could not read the mock recording." }
    $probe = $probeJson | ConvertFrom-Json
    $videoStream = @($probe.streams)[0]
    if (-not $videoStream -or $videoStream.codec_name -ne "h264" -or
        [int]$videoStream.width -lt 640 -or [int]$videoStream.height -lt 360 -or
        [int]$videoStream.nb_read_frames -lt 180) {
        throw "The mock recording does not contain a healthy H.264 video stream."
    }
    Stop-Process -Id $process.Id -Force
    $process.WaitForExit()
    $process = $null

    # Simulate the previous name in an otherwise complete test installation.
    $legacyExe = Join-Path $installDir "Momento.exe"
    Copy-Item -LiteralPath $exe -Destination $legacyExe
    Remove-ItemProperty -Path $runKey -Name "Mementos" -ErrorAction SilentlyContinue
    Set-ItemProperty -Path $runKey -Name "Momento" -Value ('"' + $legacyExe + '"')
    Invoke-Installer -MigrateAutostart
    if (-not (Test-Path -LiteralPath $exe)) {
        throw "Upgrade did not preserve the installed application."
    }
    if (-not (Test-Path -LiteralPath $legacyExe) -or
        (Get-FileHash -LiteralPath $legacyExe -Algorithm SHA256).Hash -ne
        (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash) {
        throw "Upgrade did not refresh the legacy shortcut target with the new executable."
    }
    if ((Get-ItemPropertyValue -Path $runKey -Name "Mementos") -ne ('"' + $exe + '"') -or
        $null -ne (Get-ItemPropertyValue -Path $runKey -Name "Momento" -ErrorAction SilentlyContinue)) {
        throw "Upgrade did not migrate the existing startup entry."
    }

    $sentinel = Join-Path $appData "Mementos\preserve-on-default-uninstall.txt"
    $oauthSentinel = Join-Path $appData "Mementos\youtube_oauth_client.dat"
    Set-Content -LiteralPath $sentinel -Encoding ascii -Value "preserve"
    Set-Content -LiteralPath $oauthSentinel -Encoding ascii -Value "preserve-oauth"
    $uninstall = Start-Process -FilePath $uninstaller -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"
    ) -PassThru -Wait -WindowStyle Hidden
    if ($uninstall.ExitCode -ne 0) {
        throw "Uninstall failed with exit code $($uninstall.ExitCode)."
    }
    if (Test-Path -LiteralPath $exe) {
        throw "Uninstall left the application executable behind."
    }
    if (-not (Test-Path -LiteralPath $sentinel)) {
        throw "Default uninstall removed user data."
    }
    if (-not (Test-Path -LiteralPath $oauthSentinel)) {
        throw "Default uninstall removed the user's Google OAuth setup."
    }
    if (-not (Test-Path -LiteralPath $recording.FullName)) {
        throw "Default uninstall removed the mock recording."
    }

    Invoke-Installer
    $stateDir = Join-Path $appData "Mementos"
    New-Item -ItemType Directory -Path (Join-Path $stateDir "logs") -Force | Out-Null
    $purgeTargets = @(
        (Join-Path $stateDir "config.json"),
        (Join-Path $stateDir "config.json.tmp"),
        (Join-Path $stateDir "config.json.broken-test.txt"),
        (Join-Path $stateDir "window_state.ini"),
        (Join-Path $stateDir "youtube_token.dat"),
        (Join-Path $stateDir "youtube_token.dat.tmp"),
        (Join-Path $stateDir "youtube_avatar.png"),
        (Join-Path $stateDir "youtube_avatar.png.tmp"),
        (Join-Path $stateDir "youtube_oauth_client.dat"),
        (Join-Path $stateDir "youtube_oauth_client.dat.tmp"),
        (Join-Path $appData "Momento\momento.lock"),
        (Join-Path $stateDir "logs\mementos.log")
    )
    foreach ($targetPath in $purgeTargets) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $targetPath) -Force | Out-Null
        Set-Content -LiteralPath $targetPath -Encoding ascii -Value "purge-test"
    }
    Assert-IsolatedAppData
    $purge = Start-Process -FilePath $uninstaller -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/PURGEUSERDATA"
    ) -PassThru -Wait -WindowStyle Hidden
    if ($purge.ExitCode -ne 0) {
        throw "Purge uninstall failed with exit code $($purge.ExitCode)."
    }
    foreach ($targetPath in $purgeTargets) {
        if (Test-Path -LiteralPath $targetPath) {
            throw "Purge uninstall left user state behind: $targetPath"
        }
    }
    if (-not (Test-Path -LiteralPath $recording.FullName)) {
        throw "Purge uninstall removed the mock recording."
    }
    Write-Host "Installer, fresh profile, mock recording, upgrade, default uninstall, and purge checks passed."
}
finally {
    if ($target -and -not $target.HasExited) {
        [void]$target.CloseMainWindow()
        if (-not $target.WaitForExit(3000)) { Stop-Process -Id $target.Id -Force }
    }
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
        $process.WaitForExit()
    }
    $cleanupUninstaller = Join-Path $installDir "unins000.exe"
    if (Test-Path -LiteralPath $cleanupUninstaller) {
        Start-Process -FilePath $cleanupUninstaller -ArgumentList @(
            "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"
        ) -Wait -WindowStyle Hidden | Out-Null
    }
    foreach ($brand in $oldRunValues.Keys) {
        if ($null -ne $oldRunValues[$brand]) {
            Set-ItemProperty -Path $runKey -Name $brand -Value $oldRunValues[$brand]
        }
        else {
            Remove-ItemProperty -Path $runKey -Name $brand -ErrorAction SilentlyContinue
        }
    }
    $env:APPDATA = $oldAppData
    $env:LOCALAPPDATA = $oldLocalAppData
    $env:USERPROFILE = $oldUserProfile
    $env:PATH = $oldPath
    if ($hadUninstallKey -and (Test-Path -LiteralPath $uninstallBackup)) {
        & reg.exe import $uninstallBackup | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not restore the existing uninstall registration." }
    }
    foreach ($item in $shortcutBackups) {
        $parent = Split-Path -Parent $item.Path
        if ($item.Existed -and (Test-Path -LiteralPath $item.Backup)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
            Copy-Item -LiteralPath $item.Backup -Destination $item.Path -Force
        }
        elseif (-not $item.Existed -and (Test-Path -LiteralPath $item.Path)) {
            Remove-Item -LiteralPath $item.Path -Force
        }
        if (-not $item.ParentExisted -and (Test-Path -LiteralPath $parent) -and
            @(Get-ChildItem -LiteralPath $parent -Force).Count -eq 0) {
            [IO.Directory]::Delete($parent, $false)
        }
    }
    if (Test-Path -LiteralPath $testRoot) {
        $resolvedTestRoot = [System.IO.Path]::GetFullPath($testRoot)
        $resolvedTemp = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
        if (-not $resolvedTestRoot.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove a test directory outside the temporary root."
        }
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}
