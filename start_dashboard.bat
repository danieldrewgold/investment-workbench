@echo off
rem Double-click to start the Workbench dashboard (http://127.0.0.1:8765). Close this window to stop it.
cd /d "%~dp0"
python dashboard.py %*
pause
