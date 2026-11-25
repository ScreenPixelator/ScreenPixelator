@echo off
title ScreenPixelator 1.0 installer
set "PS1=%~dp0files\install.ps1"
if not exist "%PS1%" set "PS1=%~dp0install.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
if %errorlevel%==0 (
  echo.
  echo Done. This window closes in 5 seconds...
  timeout /t 5 >nul
) else (
  echo.
  echo Something went wrong ^(see above^).
  pause
)
