@echo off
rem Configure vJoy device 1 for the Logitech WingMan Force (J-UA9). Run as administrator.
set VJC="C:\Program Files\vJoy\x64\vJoyConfig.exe"
if not exist %VJC% (
  echo vJoy is not installed. Run third_party\vJoy\vJoySetup_v2.2.2.0_Win10_Win11.exe first.
  pause
  exit /b 1
)
echo Configuring vJoy device 1: X, Y, Z axes, 9 buttons, 1 hat, all force-feedback effects...
%VJC% 1 -f -a x y z -b 9 -p 1 -e all
rem Display name: XPForce ignores devices called "vJoy Device".
reg add "HKCU\System\CurrentControlSet\Control\MediaProperties\PrivateProperties\Joystick\OEM\VID_1234&PID_BEAD" /v OEMName /t REG_SZ /d "WingMan Force J-UA9" /f
echo Done.
pause
