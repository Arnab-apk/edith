"""Edith.exe - tiny launcher (stdlib only, built with PyInstaller: see tools/build_exe.bat).

Put Edith.exe in the project folder (next to setup.bat). Double-click it:
  * first run  -> runs the installer + setup wizard (setup.bat)
  * afterwards -> starts the bot (same as start.bat)
  * Edith.exe menu | setup | doctor | logs ...  -> passes the command to the control panel
It contains no application code and no secrets: the app itself stays in this folder.
"""
import os
import subprocess
import sys
from pathlib import Path


def app_dir() -> Path:
    exe = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    here = exe.parent
    for cand in (here, here.parent):  # exe next to setup.bat, or run from tools/ during development
        if (cand / "bot" / "cli.py").exists():
            return cand
    return here


def main() -> int:
    root = app_dir()
    os.chdir(root)
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("Edith")
    except Exception:
        pass
    if not (root / "bot" / "cli.py").exists():
        print("Edith.exe must be inside the Edith folder (next to setup.bat).\n"
              "Download it from https://github.com/Arnab-apk/edith")
        input("Press Enter to close...")
        return 1
    py = root / ".venv" / "Scripts" / "python.exe"
    args = sys.argv[1:]
    if not py.exists() or (not (root / ".env").exists() and not args):
        print("First run: installing Edith...\n")
        rc = subprocess.call(["cmd", "/c", str(root / "setup.bat")])
        if rc != 0 or not py.exists():
            return rc or 1
        if not (root / ".env").exists():
            return 0
        return 0  # the wizard already offered to start the bot
    if args and args[0] == "menu":
        args = []
    elif not args:
        args = ["start"]
    rc = subprocess.call([str(py), "-m", "bot.cli", *args])
    if rc != 0:
        input("\nEdith stopped with an error. Press Enter to close...")
    return rc


if __name__ == "__main__":
    sys.exit(main())
