"""Guardrails enforced for EVERY action, whoever asks (slash command, button, AI, automation).

registry.run_action() calls check() before running anything. These rules only ever ADD
protection: nothing here can skip the owner's Confirm button for a dangerous action.

Settings (.env or `control config set`):
  EDITH_SAFE_MODE=1                 block every dangerous action (look-only mode)
  EDITH_DISABLED_ACTIONS=a,b        block these actions; "category:power" blocks a whole group
  EDITH_ALLOW_RISKY_SHELL=1         allow the catastrophic shell commands listed below (not advised)
  EDITH_SKIP_CONFIRM=a,b            no Confirm prompt for these (never for shell/delete/move/automations);
                                    "all" = prompt only for those; "none" brings back every prompt
"""
from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .registry import Action

SHELL_ACTIONS = {"shell_cmd", "shell_ps", "run_program"}

# Commands that can destroy the system or weaken its defences. Blocked even after Confirm:
# one mis-tap or misheard voice note must never be able to brick the PC.
_DELETE = r"(?:del|erase|rd|rmdir|rm|ri|remove-item)"
CATASTROPHIC: list[tuple[str, re.Pattern[str]]] = [(why, re.compile(rx, re.I)) for why, rx in (
    ("formats or wipes a disk", r"\bformat(?:\.com)?\s+[a-z]:|\b(?:format-volume|clear-disk|initialize-disk|"
                                r"remove-partition)\b|\bdiskpart\b"),
    ("changes the boot configuration", r"\bbcdedit\b"),
    ("deletes backups / shadow copies", r"\b(?:vssadmin|wbadmin)\b[^\n]*\bdelete\b|\bshadowcopy\b[^\n]*\bdelete\b"),
    ("securely erases free space", r"\bcipher(?:\.exe)?\b[^\n]*\s/w"),
    ("deletes a whole drive", rf"\b{_DELETE}\b[^|;&\n]*\s[\"']?[a-z]:\\?\*?[\"']?(?:\s|$)"),
    ("deletes Windows system files", rf"\b{_DELETE}\b[^|;&\n]*[a-z]:\\windows\b"),
    ("deletes machine-wide registry keys", r"\breg(?:\.exe)?\s+delete\s+hklm|\bremove-item\b[^\n]*\bhklm:"),
    ("turns off antivirus protection", r"\bset-mppreference\b[^\n]*-disable|\badd-mppreference\b[^\n]*-exclusion"),
    ("turns off the firewall", r"\bnetsh\b[^\n]*firewall\b[^\n]*\bstate\s+off|\bset-netfirewallprofile\b"
                               r"[^\n]*-enabled\s+(?:\$?false|0)"),
    ("creates or promotes user accounts", r"\bnet\s+user\s+\S+\s+\S+\s+/add\b|\bnet\s+localgroup\s+"
                                          r"administrators\b[^\n]*/add\b"),
    ("hides software from Task Manager", r"\bdisabletaskmgr\b"),
)]


def _on(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def disabled_entries() -> set[str]:
    raw = os.getenv("EDITH_DISABLED_ACTIONS", "")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def risky_shell_reason(text: str) -> str | None:
    """Why a shell command is blocked, or None when it is allowed."""
    if _on("EDITH_ALLOW_RISKY_SHELL"):
        return None
    for why, rx in CATASTROPHIC:
        if rx.search(text or ""):
            return why
    return None


def check(act: "Action", args: dict[str, Any]) -> str | None:
    """Return a human-readable reason when `act` must not run, else None."""
    if act.dangerous and _on("EDITH_SAFE_MODE"):
        return (f"Safe mode is on, so {act.name} is blocked. "
                "Turn it off with: control config set EDITH_SAFE_MODE 0")
    off = disabled_entries()
    if act.name.lower() in off or (act.category and f"category:{act.category.lower()}" in off):
        return f"{act.name} is disabled on this PC (EDITH_DISABLED_ACTIONS)."
    if act.name in SHELL_ACTIONS:
        text = " ".join(str(args.get(k, "")) for k in ("command", "target", "args"))
        why = risky_shell_reason(text)
        if why:
            return (f"Blocked for safety: this command {why}. Edith never runs commands like this, "
                    "even when confirmed.")
    return None


# ---------------------------------------------------------------- when to ask Confirm
# Confirm is for things a mistake would really hurt. These are dangerous=True for safety reviews
# but harmless and instantly reversible, so they run without a prompt by default.
DEFAULT_NO_CONFIRM = frozenset({"power_lock", "power_monitor_off"})
SAFE_MOUSE_MODES = frozenset({"move", "move_by", "scroll"})  # clicks still confirm
# Never skippable, whatever the settings say: these can run code, destroy data or create
# unattended automations.
ALWAYS_CONFIRM = frozenset({"shell_cmd", "shell_ps", "run_program", "delete_path", "move_path",
                            "save_macro", "run_macro", "schedule_action", "create_trigger"})


def skip_confirm_set() -> set[str]:
    """EDITH_SKIP_CONFIRM=kill_process,webcam_photo adds to the defaults; "none" restores every prompt."""
    raw = os.getenv("EDITH_SKIP_CONFIRM", "").strip().lower()
    if raw in ("none", "off", "0"):
        return set()
    return set(DEFAULT_NO_CONFIRM) | {x.strip() for x in raw.split(",") if x.strip()}


def needs_confirm(act: "Action", args: dict[str, Any] | None = None) -> bool:
    """True when the owner must tap Confirm before `act` runs (slash commands and AI alike)."""
    if not act.dangerous:
        return False
    if act.name in ALWAYS_CONFIRM:
        return True
    if act.name == "mouse_control" and str((args or {}).get("action", "")).lower() in SAFE_MOUSE_MODES:
        return False
    if _skip_all():
        return False
    return act.name not in skip_confirm_set()


def _skip_all() -> bool:
    """EDITH_SKIP_CONFIRM=all: no prompt for anything except the ALWAYS_CONFIRM group."""
    return os.getenv("EDITH_SKIP_CONFIRM", "").strip().lower() == "all"


def summary() -> str:
    """One-line description of the active guardrails (for doctor and /status-style output)."""
    parts = ["safe mode ON" if _on("EDITH_SAFE_MODE") else "safe mode off"]
    off = disabled_entries()
    parts.append(f"{len(off)} disabled ({', '.join(sorted(off))})" if off else "nothing disabled")
    parts.append("risky shell ALLOWED" if _on("EDITH_ALLOW_RISKY_SHELL") else "risky shell blocked")
    if _skip_all():
        parts.append("Confirm only for shell/delete/move/programs/automations")
        return ", ".join(parts)
    skip = sorted(skip_confirm_set())
    parts.append(f"no Confirm for: {', '.join(skip)}" if skip else "Confirm for every dangerous action")
    return ", ".join(parts)
