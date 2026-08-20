#define MyAppName "Momento"
#define MyAppVersion "0.2.6"
#define MyAppPublisher "Momento"
#define MyAppExeName "Momento.exe"

[Setup]
AppId={{8C8A50E4-FCF2-4E1E-9B9C-046B9ED5F3AA}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\Momento
DefaultGroupName=Momento
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.18362
SetupMutex=Momento.GameRecorder.Update
CloseApplications=no
RestartApplications=no
SetupIconFile=..\resources\icons\momento.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
LicenseFile=..\LICENSE
OutputDir=..\dist\installer
OutputBaseFilename=MomentoSetup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\Momento\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\BUILD_INFO.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\SOURCE_OFFER.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Momento"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Momento"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Momento"; Flags: nowait postinstall skipifsilent; Check: not IsUpdateMode
Filename: "{app}\{#MyAppExeName}"; Parameters: "--updated={code:GetAttemptToken}"; Flags: nowait runhidden; Check: IsUpdateMode

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  RunValueName = 'Momento';
  AppMutexName = 'Momento.GameRecorder.Instance';
  UpdateReadyPrefix = 'Local\Momento.GameRecorder.UpdateReady.';
  ProcessSynchronize = $00100000;
  EventModifyState = $0002;
  WaitObject0 = $00000000;
  WaitTimeout = $00000102;
  WaitFailed = $FFFFFFFF;
  UpdateParentWaitTimeoutMs = 30000;

var
  UpdateMode: Boolean;
  UpdateAttemptToken: String;
  UpdateParentPid: Integer;
  ParentProcessHandle: THandle;
  UpdateParentExited: Boolean;
  UpdateInstallSucceeded: Boolean;

function OpenProcess(
  DesiredAccess: DWORD; InheritHandle: BOOL; ProcessId: DWORD
): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function OpenEvent(
  DesiredAccess: DWORD; InheritHandle: BOOL; Name: String
): THandle;
  external 'OpenEventW@kernel32.dll stdcall';
function SetEvent(EventHandle: THandle): BOOL;
  external 'SetEvent@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: DWORD): DWORD;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';

function HasCommandLineParam(const Wanted: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), Wanted) = 0 then
    begin
      Result := True;
      Exit;
    end;
end;

function CommandLineValue(const Prefix: String): String;
var
  I: Integer;
  Value: String;
begin
  Result := '';
  for I := 1 to ParamCount do
  begin
    Value := ParamStr(I);
    if CompareText(Copy(Value, 1, Length(Prefix)), Prefix) = 0 then
    begin
      Result := Copy(Value, Length(Prefix) + 1, MaxInt);
      Exit;
    end;
  end;
end;

function IsLowerHexToken(const Value: String): Boolean;
var
  I: Integer;
begin
  Result := Length(Value) = 64;
  if not Result then
    Exit;
  for I := 1 to Length(Value) do
    if Pos(Value[I], '0123456789abcdef') = 0 then
    begin
      Result := False;
      Exit;
    end;
end;

function IsUpdateMode(): Boolean;
begin
  Result := UpdateMode;
end;

function GetAttemptToken(Param: String): String;
begin
  Result := UpdateAttemptToken;
end;

function OpenUpdateParent(): Boolean;
begin
  ParentProcessHandle := OpenProcess(
    ProcessSynchronize, False, DWORD(UpdateParentPid)
  );
  Result := ParentProcessHandle <> 0;
end;

function SignalUpdateReady(): Boolean;
var
  ReadyHandle: THandle;
begin
  ReadyHandle := OpenEvent(
    EventModifyState, False, UpdateReadyPrefix + UpdateAttemptToken
  );
  Result := ReadyHandle <> 0;
  if Result then
  begin
    Result := SetEvent(ReadyHandle);
    CloseHandle(ReadyHandle);
  end;
end;

function InitializeSetup(): Boolean;
var
  ParentText: String;
begin
  ParentProcessHandle := 0;
  UpdateParentExited := False;
  UpdateInstallSucceeded := False;
  UpdateMode := HasCommandLineParam('/MOMENTOUPDATE');
  if not UpdateMode then
  begin
    Result := not CheckForMutexes(AppMutexName);
    if not Result then
      SuppressibleMsgBox(
        'Momento is still running. Quit it from the system tray before installing.',
        mbError, MB_OK, IDOK
      );
    Exit;
  end;

  ParentText := CommandLineValue('/PARENTPID=');
  UpdateAttemptToken := CommandLineValue('/ATTEMPTTOKEN=');
  UpdateParentPid := StrToIntDef(ParentText, 0);
  if (UpdateParentPid <= 0) or not IsLowerHexToken(UpdateAttemptToken) then
  begin
    Log('Update handoff parameters are invalid.');
    Result := False;
    Exit;
  end;
  if not OpenUpdateParent() then
  begin
    Log('Could not open the update parent process.');
    Result := False;
    Exit;
  end;
  if not SignalUpdateReady() then
  begin
    Log('Could not signal update handoff readiness.');
    CloseHandle(ParentProcessHandle);
    ParentProcessHandle := 0;
    Result := False;
    Exit;
  end;
  Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  WaitResult: DWORD;
begin
  Result := '';
  if not UpdateMode then
    Exit;

  WaitResult := WaitForSingleObject(
    ParentProcessHandle, UpdateParentWaitTimeoutMs
  );
  if WaitResult = WaitObject0 then
  begin
    UpdateParentExited := True;
    Exit;
  end;

  if WaitResult = WaitTimeout then
    Log('Timed out waiting for the exact update parent to exit.')
  else if WaitResult = WaitFailed then
    Log('Waiting for the exact update parent failed.')
  else
    Log('Waiting for the exact update parent returned an unexpected result.');
  Result := 'Momento could not safely hand the update to Setup.';
end;

procedure RemoveObsoleteRuntime();
var
  ImageFormats: String;
  PyAvLibraries: String;
  FindRec: TFindRec;
begin
  DelTree(
    ExpandConstant('{app}\_internal\PyQt6\Qt6\translations'),
    True, True, True
  );
  ImageFormats := ExpandConstant(
    '{app}\_internal\PyQt6\Qt6\plugins\imageformats\'
  );
  DeleteFile(ImageFormats + 'qgif.dll');
  DeleteFile(ImageFormats + 'qicns.dll');
  DeleteFile(ImageFormats + 'qtga.dll');
  DeleteFile(ImageFormats + 'qtiff.dll');
  DeleteFile(ImageFormats + 'qwbmp.dll');
  DeleteFile(ImageFormats + 'qwebp.dll');

  PyAvLibraries := ExpandConstant('{app}\_internal\av.libs\');
  if FindFirst(PyAvLibraries + '*.dll', FindRec) then
  begin
    try
      repeat
        if (CompareText(FindRec.Name, 'avcodec-62-4d28b54037f2761423840318c68e5a32.dll') <> 0) and
           (CompareText(FindRec.Name, 'avdevice-62-2802c4446b384f78b9e92b17563c14d5.dll') <> 0) and
           (CompareText(FindRec.Name, 'avfilter-11-d64fa8e58ac927e2679aa711773901ba.dll') <> 0) and
           (CompareText(FindRec.Name, 'avformat-62-282ffcbf655477408ab5c1c3f4adf54e.dll') <> 0) and
           (CompareText(FindRec.Name, 'avutil-60-833ee04a13e9310cb90177b2d206b51c.dll') <> 0) and
           (CompareText(FindRec.Name, 'libgcc_s_seh-1-fb5a9b1bb254026169325ae2b3cad1cc.dll') <> 0) and
           (CompareText(FindRec.Name, 'libstdc++-6-7c98bad87f582095f4ac9a5958b22abc.dll') <> 0) and
           (CompareText(FindRec.Name, 'libvpl-6b8c4104601b4dc1ce504e4781e02378.dll') <> 0) and
           (CompareText(FindRec.Name, 'libwinpthread-1-ed858c25be05f072fe6dc08bd9b9fc79.dll') <> 0) and
           (CompareText(FindRec.Name, 'libx264-165-ba2f6715c25b2cff1d57af10039bb25e.dll') <> 0) and
           (CompareText(FindRec.Name, 'swresample-6-2d4ebf3ac3b90ec6232a79211c05d5ef.dll') <> 0) and
           (CompareText(FindRec.Name, 'swscale-9-500b7eada7986e0a5e17199f8cad8cb7.dll') <> 0) then
          DeleteFile(PyAvLibraries + FindRec.Name);
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
  DelTree(ExpandConstant('{app}\source'), True, True, True);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  PreviousAutostart: String;
begin
  if CurStep = ssDone then
    UpdateInstallSucceeded := True;

  if CurStep = ssPostInstall then
  begin
    RemoveObsoleteRuntime();
    if not HasCommandLineParam('/NOMIGRATEAUTOSTART') then
      if RegQueryStringValue(HKCU, RunKey, RunValueName, PreviousAutostart) then
        RegWriteStringValue(
          HKCU, RunKey, RunValueName,
          '"' + ExpandConstant('{app}\{#MyAppExeName}') + '"'
        );
  end;
end;

procedure RelaunchAfterFailedUpdate();
var
  AppPath: String;
  ResultCode: Integer;
begin
  if (ParentProcessHandle = 0) or
     (WaitForSingleObject(ParentProcessHandle, 0) <> WaitObject0) then
  begin
    Log('Skipping update recovery because the exact parent may still be running.');
    Exit;
  end;

  AppPath := ExpandConstant('{app}\{#MyAppExeName}');
  if not FileExists(AppPath) then
  begin
    Log('Could not relaunch Momento after the failed update: executable is unavailable.');
    Exit;
  end;

  if Exec(
    AppPath, '--updated=' + UpdateAttemptToken, ExtractFileDir(AppPath),
    SW_HIDE, ewNoWait, ResultCode
  ) then
    Log('Relaunched Momento after the failed update was rolled back.')
  else
    Log(
      'Could not relaunch Momento after the failed update: ' +
      SysErrorMessage(ResultCode)
    );
end;

procedure DeinitializeSetup();
begin
  if UpdateMode and UpdateParentExited and (not UpdateInstallSucceeded) then
    RelaunchAfterFailedUpdate();

  if ParentProcessHandle <> 0 then
  begin
    CloseHandle(ParentProcessHandle);
    ParentProcessHandle := 0;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  PurgeUserData: Boolean;
begin
  if CurUninstallStep <> usUninstall then
    Exit;

  RegDeleteValue(HKCU, RunKey, RunValueName);
  PurgeUserData := HasCommandLineParam('/PURGEUSERDATA');
  if (not PurgeUserData) and (not UninstallSilent) then
    PurgeUserData := MsgBox(
      'Also remove Momento settings, logs, and local account state?' + #13#10 + #13#10 +
      'Recordings and clips are never removed.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2
    ) = IDYES;
  if PurgeUserData then
  begin
    DeleteFile(ExpandConstant('{userappdata}\Momento\config.json'));
    DeleteFile(ExpandConstant('{userappdata}\Momento\config.json.tmp'));
    DelTree(
      ExpandConstant('{userappdata}\Momento\config.json.broken-*.txt'),
      False, True, False
    );
    DeleteFile(ExpandConstant('{userappdata}\Momento\window_state.ini'));
    DeleteFile(ExpandConstant('{userappdata}\Momento\youtube_token.dat'));
    DeleteFile(ExpandConstant('{userappdata}\Momento\youtube_token.dat.tmp'));
    DeleteFile(ExpandConstant('{userappdata}\Momento\youtube_avatar.png'));
    DeleteFile(ExpandConstant('{userappdata}\Momento\youtube_avatar.png.tmp'));
    DeleteFile(ExpandConstant('{userappdata}\Momento\youtube_oauth_client.dat'));
    DeleteFile(ExpandConstant('{userappdata}\Momento\youtube_oauth_client.dat.tmp'));
    DeleteFile(ExpandConstant('{userappdata}\Momento\momento.lock'));
    DelTree(ExpandConstant('{userappdata}\Momento\logs'), True, True, True);
  end;
end;
