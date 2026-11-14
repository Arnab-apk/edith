@echo off
rem Control panel: menu with no args, or: control setup ^| start ^| doctor ^| config show ^| config set KEY VALUE ^| autostart on/off ^| logs
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run setup.bat first.& pause & exit /b 1)
".venv\Scripts\python.exe" -m bot.cli %*
if "%~1"=="" pause
