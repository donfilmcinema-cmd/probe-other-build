; Установщик DZOD Vocalist и DZOD Revoicer (Inno Setup): ISCC /DAppVer=0.3.0 [/DRevoicer] installer.iss
; Ставит программу для текущего пользователя, без прав администратора.
; Движок (нейросети) программа ставит сама при первом запуске — в выбранную папку на диске.
#ifndef AppVer
  #define AppVer "0.0.0"
#endif
#ifdef Revoicer
  #define AppName "DZOD Revoicer"
  #define AppGuid "{{7C5B3E21-6F0A-4D8E-9B2C-3A1E5D7F9B40}"
  #define FileBase "DZOD-Revoicer"
#else
  #define AppName "DZOD Vocalist"
  #define AppGuid "{{20D8E816-E368-4193-BF82-A2F5B94D0E03}"
  #define FileBase "DZOD-Vocalist"
#endif
[Setup]
AppId={#AppGuid}
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher=ООО "Нейрофильм"
AppPublisherURL=https://neirofilm.ru
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\out
OutputBaseFilename={#FileBase}-{#AppVer}-setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppName}.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
