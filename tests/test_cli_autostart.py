"""control autostart: a per-user logon task that needs no admin rights."""
from pathlib import Path

from bot import cli


def test_autostart_script_is_per_user_and_robust():
    s = cli.autostart_script(Path(r"C:\Edith's apps\start.bat"), Path(r"C:\Edith's apps"))
    assert "schtasks" not in s and "-AtLogOn -User $u" in s
    assert "-AllowStartIfOnBatteries" in s and "-DontStopIfGoingOnBatteries" in s
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in s and "-RunLevel Limited" in s
    assert "'C:\Edith''s apps\start.bat'" in s  # quotes escaped for PowerShell


def test_autostart_off_when_absent(monkeypatch):
    import argparse
    monkeypatch.setattr(cli, "_task_exists", lambda: False)
    monkeypatch.setattr(cli, "_ps", lambda script: (_ for _ in ()).throw(AssertionError("must not run")))
    assert cli.cmd_autostart(argparse.Namespace(state="off")) == 0
