@echo off
rem Runs the bot. Keep this window open; close it (or Ctrl+C) to stop the bot.
title Edith
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run setup.bat first.& pause & exit /b 1)
".venv\Scripts\python.exe" -m bot.cli start
pause
