[Setup]
AppId=OfferPilot.Lite
AppName=OfferPilot Lite
AppVersion=0.2.0
DefaultDirName={localappdata}\Programs\OfferPilot
DefaultGroupName=OfferPilot
PrivilegesRequired=lowest
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=OfferPilot-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\OfferPilot.exe
CloseApplications=yes
CloseApplicationsFilter=OfferPilot.exe

[Files]
Source: "..\dist\OfferPilot\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\OfferPilot"; Filename: "{app}\OfferPilot.exe"; AppUserModelID: "OfferPilot.Lite"
Name: "{autodesktop}\OfferPilot"; Filename: "{app}\OfferPilot.exe"; AppUserModelID: "OfferPilot.Lite"

[Run]
Filename: "{app}\OfferPilot.exe"; Description: "打开 OfferPilot"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{userstartup}\OfferPilot.lnk"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "OfferPilot"; Flags: uninsdeletevalue
