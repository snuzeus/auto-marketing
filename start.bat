@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\start.ps1"
if errorlevel 1 (
  echo.
  echo [ERROR] See the message above.
)
pause
