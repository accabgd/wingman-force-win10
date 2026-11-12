; Inno Setup script for the Logitech WingMan Force (J-UA9) driver.
; Build: installer\build_installer.bat (PyInstaller + Inno Setup 6).

#define AppVersion "1.0.0"
#ifndef DistDir
  #define DistDir "..\build\dist\WingManForce"
#endif

[Setup]
AppId={{6F0B6A1E-3C47-4C5B-9E4D-0A1B2C3D4E5F}
AppName=WingMan Force (J-UA9) driver
AppVersion={#AppVersion}
AppPublisher=accabgd
AppPublisherURL=https://github.com/accabgd/wingman-force-win10
AppSupportURL=https://github.com/accabgd/wingman-force-win10/issues
DefaultDirName={autopf}\WingMan Force
DefaultGroupName=WingMan Force
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\build\installer
OutputBaseFilename=WingManForce-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE
InfoAfterFile=winusb_guide.txt
UninstallDisplayName=WingMan Force (J-UA9) driver
UsedUserAreasWarning=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "..\third_party\vJoy\vJoySetup_v2.2.2.0_Win10_Win11.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "..\third_party\vJoy\LICENSE.txt"; DestDir: "{app}\third_party\vJoy"
Source: "..\README.md"; DestDir: "{app}"
Source: "..\LICENSE"; DestDir: "{app}"
Source: "winusb_guide.txt"; DestDir: "{app}"

[Icons]
Name: "{group}\WingMan Force (MSFS force feedback)"; Filename: "{app}\WingManForce.exe"; Parameters: "--msfs"; WorkingDir: "{app}"
Name: "{group}\WingMan Force (joystick only, no force feedback)"; Filename: "{app}\WingManForce.exe"; Parameters: "--no-ffb"; WorkingDir: "{app}"
Name: "{group}\WingMan Force button test"; Filename: "{app}\WingManForce.exe"; Parameters: "--buttons"; WorkingDir: "{app}"
Name: "{group}\How to set up WinUSB (read me)"; Filename: "{app}\winusb_guide.txt"
Name: "{group}\Uninstall WingMan Force"; Filename: "{uninstallexe}"
Name: "{autodesktop}\WingMan Force"; Filename: "{app}\WingManForce.exe"; Parameters: "--msfs"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
; XPForce ignores devices named "vJoy Device"; give the virtual joystick the stick's name.
Root: HKCU; Subkey: "System\CurrentControlSet\Control\MediaProperties\PrivateProperties\Joystick\OEM\VID_1234&PID_BEAD"; ValueType: string; ValueName: "OEMName"; ValueData: "WingMan Force J-UA9"

[Run]
Filename: "{tmp}\vJoySetup_v2.2.2.0_Win10_Win11.exe"; Parameters: "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART"; StatusMsg: "Installing vJoy 2.2.2.0 (virtual joystick driver)..."; Check: InstallVJoy
Filename: "{commonpf64}\vJoy\x64\vJoyConfig.exe"; Parameters: "1 -f -a x y z -b 9 -p 1 -e all"; StatusMsg: "Configuring the virtual joystick..."; Flags: runhidden waituntilterminated
Filename: "{sys}\mmc.exe"; Parameters: "devmgmt.msc"; Description: "Open Device Manager now to assign WinUSB to the stick (needed once - see the instructions)"; Flags: postinstall shellexec skipifsilent
Filename: "{app}\winusb_guide.txt"; Description: "Show the WinUSB instructions"; Flags: postinstall shellexec skipifsilent unchecked

[Code]
var
  VJoyAsked, VJoyWanted: Boolean;

function InstallVJoy: Boolean;
begin
  if not VJoyAsked then
  begin
    VJoyAsked := True;
    if not FileExists(ExpandConstant('{commonpf64}\vJoy\x64\vJoyConfig.exe')) then
      VJoyWanted := True
    else
      VJoyWanted := MsgBox('vJoy is already installed.' + #13#10#13#10 +
        'This driver needs vJoy 2.2.2.0 (older versions mix up force-feedback effects).' + #13#10 +
        'Install vJoy 2.2.2.0 now? Choose Yes if you are not sure which version you have.' + #13#10#13#10 +
        '(If an older vJoy is installed, uninstall it first via Settings > Apps, then run this setup again.)',
        mbConfirmation, MB_YESNO) = IDYES;
  end;
  Result := VJoyWanted;
end;
