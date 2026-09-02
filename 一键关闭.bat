@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1"
set "PULSE_EXIT=%ERRORLEVEL%"
if not "%PULSE_EXIT%"=="0" (
  echo.
  echo PULSE could not stop cleanly. Please read the Chinese error message above.
  pause
)
exit /b %PULSE_EXIT%
