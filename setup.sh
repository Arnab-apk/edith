#!/usr/bin/env bash
# For Git Bash / MSYS users: runs the Windows installer (the app itself is Windows-only).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$here/setup.ps1")" "$@"
