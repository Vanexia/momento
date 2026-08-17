#define MyAppName "Momento"
#define MyAppVersion "0.2.1"
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
AppMutex=Momento.GameRecorder.Instance
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

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\licenses"

[Files]
Source: "..\dist\Momento\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\BUILD_INFO.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\source\Momento-{#MyAppVersion}-source.zip"; DestDir: "{app}\source"; Flags: ignoreversion
Source: "..\dist\source\ffmpeg-8.1.2.tar.xz"; DestDir: "{app}\source"; Flags: ignoreversion

[Icons]
Name: "{group}\Momento"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Momento"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Momento"; Flags: nowait postinstall skipifsilent

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  RunValueName = 'Momento';

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

procedure CurStepChanged(CurStep: TSetupStep);
var
  PreviousAutostart: String;
begin
  if (CurStep = ssPostInstall) and
     (not HasCommandLineParam('/NOMIGRATEAUTOSTART')) then
    if RegQueryStringValue(HKCU, RunKey, RunValueName, PreviousAutostart) then
      RegWriteStringValue(
        HKCU, RunKey, RunValueName,
        '"' + ExpandConstant('{app}\{#MyAppExeName}') + '"'
      );
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
    DeleteFile(ExpandConstant('{userappdata}\Momento\window_state.ini'));
    DeleteFile(ExpandConstant('{userappdata}\Momento\youtube_token.dat'));
    DeleteFile(ExpandConstant('{userappdata}\Momento\youtube_avatar.png'));
    DeleteFile(ExpandConstant('{userappdata}\Momento\momento.lock'));
    DelTree(ExpandConstant('{userappdata}\Momento\logs'), True, True, True);
  end;
end;
