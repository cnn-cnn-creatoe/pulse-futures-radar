@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0repair-update.ps1" -ProjectRoot "%~dp0."
set "REPAIR_EXIT=%errorlevel%"
pause
exit /b %REPAIR_EXIT%