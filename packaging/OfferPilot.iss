#ifndef ProgramSource
#define ProgramSource "..\dist\OfferPilot"
#endif
[Setup]
#ifdef IsolatedQA
AppId=OfferPilot.Lite.IsolatedQA
#else
AppId=OfferPilot.Lite
#endif
AppName=OfferPilot Lite
AppVersion=1.0.0-rc.2
DefaultDirName={localappdata}\Programs\OfferPilot
DefaultGroupName=OfferPilot
PrivilegesRequired=lowest
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
#ifdef IsolatedQA
OutputBaseFilename=OfferPilot-QA-Setup
#else
OutputBaseFilename=OfferPilot-Setup
#endif
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\OfferPilot.exe
CloseApplications=no
RestartApplications=no
CloseApplicationsFilter=OfferPilot.exe

[Files]
Source: "{#ProgramSource}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

#ifndef IsolatedQA
[Icons]
Name: "{group}\OfferPilot"; Filename: "{app}\OfferPilot.exe"; AppUserModelID: "OfferPilot.Lite"
Name: "{autodesktop}\OfferPilot"; Filename: "{app}\OfferPilot.exe"; AppUserModelID: "OfferPilot.Lite"

[Run]
Filename: "{app}\OfferPilot.exe"; Description: "打开 OfferPilot"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{userstartup}\OfferPilot.lnk"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "OfferPilot"; Flags: uninsdeletevalue
#endif
