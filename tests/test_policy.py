"""Guardrails in bot.actions.policy apply to every caller through registry.run_action."""
import pytest

from bot.actions import policy
from bot.actions import registry as r


@pytest.fixture
def actions():
    saved = dict(r.REGISTRY)
    r.REGISTRY.clear()
    ran = []

    @r.action("shell_cmd", "Run a shell command.", dangerous=True, category="shell")
    async def shell_cmd(command: str) -> r.ActionResult:
        ran.append(command)
        return r.ActionResult(text="ran")

    @r.action("power_lock", "Lock the PC.", dangerous=True, category="power")
    async def power_lock() -> r.ActionResult:
        ran.append("lock")
        return r.ActionResult(text="locked")

    @r.action("system_status", "Status.", category="system")
    async def system_status() -> r.ActionResult:
        ran.append("status")
        return r.ActionResult(text="ok")

    yield ran
    r.REGISTRY.clear()
    r.REGISTRY.update(saved)


@pytest.mark.parametrize("cmd", [
    "format C: /q", "diskpart", "bcdedit /set {current} safeboot minimal", "vssadmin delete shadows /all /quiet",
    "wmic shadowcopy delete", "cipher /w:C:", "rd /s /q C:\\", "Remove-Item -Recurse -Force C:\\",
    "del /f /s /q C:\\Windows\\System32", "reg delete HKLM\\SOFTWARE\\Microsoft /f",
    "Set-MpPreference -DisableRealtimeMonitoring $true", "Add-MpPreference -ExclusionPath C:\\",
    "netsh advfirewall set allprofiles state off", "Set-NetFirewallProfile -Enabled False",
    "net user hacker P@ss123 /add", "net localgroup administrators hacker /add", "reg add HKCU\\x /v DisableTaskMgr /d 1",
    "Format-Volume -DriveLetter D", "Clear-Disk -Number 1 -RemoveData",
])
async def test_catastrophic_shell_blocked_even_when_confirmed(actions, cmd, monkeypatch):
    monkeypatch.delenv("EDITH_ALLOW_RISKY_SHELL", raising=False)
    res = await r.run_action("shell_cmd", command=cmd)
    assert not res.ok and res.data.get("blocked") and "Blocked for safety" in res.text
    assert actions == []  # the command never reached the shell


@pytest.mark.parametrize("cmd", [
    "dir", "Get-Date", "ipconfig /all", "del report.txt", "rd /s /q build", "Remove-Item .\\tmp -Recurse",
    "format-table Name", "git status", "netsh wlan show profiles", "net user", "echo C: drive is full",
])
async def test_ordinary_commands_still_run(actions, cmd):
    res = await r.run_action("shell_cmd", command=cmd)
    assert res.ok and actions == [cmd]


async def test_risky_shell_override(actions, monkeypatch):
    monkeypatch.setenv("EDITH_ALLOW_RISKY_SHELL", "1")
    assert (await r.run_action("shell_cmd", command="diskpart")).ok


async def test_safe_mode_blocks_only_dangerous(actions, monkeypatch):
    monkeypatch.setenv("EDITH_SAFE_MODE", "1")
    assert not (await r.run_action("power_lock")).ok
    assert (await r.run_action("system_status")).ok
    assert actions == ["status"]


async def test_disabled_actions_and_categories(actions, monkeypatch):
    monkeypatch.setenv("EDITH_DISABLED_ACTIONS", "system_status, category:power")
    assert not (await r.run_action("system_status")).ok
    assert not (await r.run_action("power_lock")).ok
    assert (await r.run_action("shell_cmd", command="dir")).ok
    assert actions == ["dir"]


async def test_every_run_and_block_is_audited(actions, monkeypatch, caplog):
    monkeypatch.delenv("EDITH_ALLOW_RISKY_SHELL", raising=False)
    with caplog.at_level("INFO", logger="audit"):
        await r.run_action("system_status")
        await r.run_action("shell_cmd", command="diskpart")
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "audit"]
    assert any(m.startswith("RAN system_status") for m in lines)
    assert any(m.startswith("BLOCKED shell_cmd") for m in lines)


def test_summary_mentions_state(monkeypatch):
    monkeypatch.setenv("EDITH_SAFE_MODE", "1")
    monkeypatch.setenv("EDITH_DISABLED_ACTIONS", "webcam_photo")
    s = policy.summary()
    assert "safe mode ON" in s and "webcam_photo" in s and "risky shell blocked" in s
