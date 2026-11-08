; Inno Setup script for the AviationLog Windows installer, called by desktop/build.py:
;   ISCC.exe /DAppVersion=0.3 /DSourceDir=dist\AviationLog /DOutputDir=dist /DIconFile=aviationlog.ico aviationlog.iss
; Installs per user (no administrator rights needed). The data folder
; %LOCALAPPDATA%\AviationLog is kept when uninstalling.

#ifndef AppVersion
  #define AppVersion "0.0"
#endif

[Setup]
AppId={{6F0C2B7E-6A3D-4C55-9E0B-3D2A9C1E7A41}
AppName=AviationLog
AppVersion={#AppVersion}
AppVerName=AviationLog {#AppVersion}
AppPublisher=AviationLog
AppPublisherURL=https://github.com/siliconserver/aviationlog
AppSupportURL=https://github.com/siliconserver/aviationlog/issues
DefaultDirName={autopf}\AviationLog
DefaultGroupName=AviationLog
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=AviationLog-{#AppVersion}-windows-x64-setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\AviationLog.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "de"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; files of an older version (e.g. removed libraries) must not linger
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\pgsql"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\AviationLog"; Filename: "{app}\AviationLog.exe"
Name: "{autodesktop}\AviationLog"; Filename: "{app}\AviationLog.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AviationLog.exe"; Description: "{cm:LaunchProgram,AviationLog}"; Flags: nowait postinstall skipifsilent
