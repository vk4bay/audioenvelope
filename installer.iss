; =============================================================================
; installer.iss  —  Inno Setup 6 script for Audio Envelope Oscilloscope
;
; Produces: dist\AudioEnvelope_Setup.exe
;
; To compile manually:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
; Or use the build script:
;   .\build.ps1 -Installer
; =============================================================================

#define AppName    "Audio Envelope Oscilloscope"
#define AppVersion "1.0"
#define AppPublisher "BDARS"
#define ExeName    "AudioEnvelope.exe"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisherURL=https://bdars.org
AppSupportURL=https://bdars.org
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\BDARS\AudioEnvelope
DefaultGroupName=BDARS\Audio Envelope Oscilloscope
AllowNoIcons=yes

; Output location and filename
OutputDir=dist
OutputBaseFilename=AudioEnvelope_Setup

; Compression (lzma gives best size, can be slow to compress)
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes

; Require Windows 10 or later (Win 11 is 10.0.22000+)
MinVersion=10.0

; Run as user — no admin rights needed (installs to user AppData if non-admin)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; 64-bit Windows
ArchitecturesInstallIn64BitMode=x64compatible

; Appearance
WizardStyle=modern
WizardSmallImageFile=

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"
Name: "startmenuicon"; Description: "Create a &Start Menu entry"; GroupDescription: "Additional icons:"; Flags: checkedonce

[Files]
; The single-file executable produced by PyInstaller
Source: "dist\{#ExeName}"; DestDir: "{app}"; Flags: ignoreversion

; Include the VC++ redistributable if needed (comment out if not required)
; Source: "vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
; Start Menu
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}"; \
  Comment: "Real-time audio oscilloscope and spectrograph"; \
  Tasks: startmenuicon

; Desktop
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; \
  Comment: "Real-time audio oscilloscope and spectrograph"; \
  Tasks: desktopicon

; Uninstaller in Start Menu
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"; \
  Tasks: startmenuicon

[Run]
; Offer to launch immediately after install
Filename: "{app}\{#ExeName}"; \
  Description: "Launch {#AppName} now"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up any files written by the app into its own folder
Type: filesandordirs; Name: "{app}"

[Code]
// Optionally check for a connected audio device before finishing
// (left as a hook — uncomment and customise if needed)
// function NextButtonClick(CurPageID: Integer): Boolean;
// begin
//   Result := True;
// end;

