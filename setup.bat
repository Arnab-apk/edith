@echo off
rem Double-click to install/configure Edith.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
pause
