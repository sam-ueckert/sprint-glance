@echo off
rem Double-click to install. Runs install.ps1 without changing the machine's execution policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
