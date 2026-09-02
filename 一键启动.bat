@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
set "PULSE_EXIT=%ERRORLEVEL%"
if not "%PULSE_EXIT%"=="0" (
  echo.
  echo PULSE failed to start. Please read the Chinese error message above.
  pause
)
exit /b %PULSE_EXIT%
