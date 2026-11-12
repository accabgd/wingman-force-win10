@echo off
title WingMan Force driver
cd /d "%~dp0"
echo Logitech WingMan Force (J-UA9) driver - keep this window open while playing.
echo Force feedback comes from Microsoft Flight Simulator (SimConnect) - do NOT start XPForce.
echo.
rem Options: python wingman_force.py --help   (e.g. --gusts 150, --aircraft b738, --no-ffb)
python wingman_force.py --msfs %*
pause
