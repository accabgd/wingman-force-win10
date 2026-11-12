@echo off
rem Builds build\installer\WingManForce-Setup-<version>.exe
rem Needs: Python 3.10+ with PyInstaller (pip install pyinstaller), Inno Setup 6.
cd /d "%~dp0.."
python -m PyInstaller --noconfirm --onedir --console --name WingManForce --hidden-import msfs_ffb ^
  --distpath build\dist --workpath build\work --specpath build wingman_force.py || exit /b 1
set ISCC="%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
%ISCC% installer\WingManForce.iss || exit /b 1
echo.
echo Installer: build\installer
