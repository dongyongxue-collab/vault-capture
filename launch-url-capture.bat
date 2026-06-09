@echo off
set "SCRIPT_DIR=%~dp0"
start "" powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%start-url-capture.ps1"
