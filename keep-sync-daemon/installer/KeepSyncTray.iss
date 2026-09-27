; Inno Setup script for the Keep Sync Windows tray -- https://jrsoftware.org/isinfo.php
; Build:  iscc installer\KeepSyncTray.iss /DAppVersion=2.2.15 /DVersionTag=v2.2.15
; Normally driven by the "Build tray app" GitHub Actions workflow, which passes both
; defines from `git describe`. AppVersion must be numeric-only (Inno's own upgrade-detection
; logic parses it); VersionTag keeps the 'v' for the output filename, matching this repo's
; tag convention and core.WINDOWS_INSTALLER_ASSET_PREFIX's expectations.
;
; Single self-contained PyInstaller --onedir build (see build-windows-tray.yml for why
; not --onefile: onefile's runtime extraction to %TEMP% is a well-known antivirus
; false-positive trigger, surfacing as "python312.dll not found") -- unlike the sibling
; Index2SP project, there's no framework-dependent variant to build here.

#define AppName "Keep Sync"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef VersionTag
  #define VersionTag "v" + AppVersion
#endif
#define AppPublisher "Keep Sync"
#define AppExeName "KeepSyncTray.exe"
#define AppUrl "https://github.com/BigWebstas/SuperProd-Keep-Sync"

[Setup]
; Fixed and permanent: Inno's upgrade detection keys off this. Never change it.
AppId={{FF684E5B-5715-4DA6-83DB-58A2BDAF3880}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppSupportURL={#AppUrl}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
SetupIconFile=..\packaging\windows\keepsync.ico
OutputDir=..\dist
OutputBaseFilename=KeepSyncTray-Setup-{#VersionTag}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Per-user install: no UAC prompt, installs under %LOCALAPPDATA%\Programs when not admin.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; No AppMutex to register on the Python side -- Inno's Restart Manager integration
; detects a running KeepSyncTray.exe holding its own file open and offers to close it.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startup"; Description: "Start {#AppName} automatically when I sign in to Windows"; GroupDescription: "Startup:"
Name: "runafterinstall"; Description: "Run {#AppName} now"; GroupDescription: "After installation:"; Flags: unchecked

[Files]
; The whole onedir output (KeepSyncTray.exe + its _internal\ dependencies),
; not just the exe -- see the note above on why this isn't --onefile.
Source: "..\dist\KeepSyncTray\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config.example.json"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Registry]
; Same value name (the app's display name) the tray's own "Start automatically" checkbox
; manages via keep_sync_tray.set_run_at_startup(), so the two never fight over the entry.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "{#AppName}"; ValueData: """{app}\{#AppExeName}"""; \
  Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Run {#AppName}"; \
  Flags: nowait postinstall skipifsilent; Tasks: runafterinstall

[UninstallRun]
; Best-effort: stop a running tray instance before removing files.
Filename: "{sys}\taskkill.exe"; Parameters: "/IM {#AppExeName} /F"; Flags: runhidden; RunOnceId: "KillKeepSyncTray"

; No [UninstallDelete] for {app}: unlike Index2SP, this app writes config.json
; and its logs INTO the install directory (see keep_sync_tray.APP_DIR/CONFIG_PATH),
; not to %APPDATA%. Inno's default uninstall only removes the files it itself
; installed (the onedir tree, README.md, config.example.json), which leaves
; config.json, the logs, and the state token in ~/.sp-keep-sync all in place --
; reinstalling later picks the sync config back up rather than starting from a
; blank setup.
