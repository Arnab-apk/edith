@echo off
rem Builds Edith.exe (a small launcher) into the project folder. Needs uv.
cd /d "%~dp0.."
uvx pyinstaller --onefile --console --name Edith --distpath . --workpath build\pyi --specpath build\pyi tools\launcher.py
