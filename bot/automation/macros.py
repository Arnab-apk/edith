"""Macros: named lists of steps.  /macro save work: open_app vscode; url https://github.com

Saving a macro with dangerous steps must be confirmed by the caller (plan.summary()).
Running one with dangerous steps ALWAYS asks again (run_macro(confirm=True)).
"""
from __future__ import annotations

import re
from typing import Any

from ..actions.registry import ActionResult
from . import AutomationError, Plan, db, describe_step, describe_steps, parse_steps, run_steps, step_is_dangerous, steps_dangerous

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_MACROS = 100


def check_name(name: str) -> str:
    name = name.strip().lower()
    if not NAME_RE.match(name):
        raise AutomationError("Macro names: 1-32 chars of a-z, 0-9, _ or - (e.g. work, night_mode).")
    return name


def plan_macro(text: str) -> tuple[str, Plan]:
    """'work: step; step' -> (name, plan)."""
    name, sep, rest = text.partition(":")
    if not sep:
        parts = text.split(None, 1)
        if len(parts) < 2:
            raise AutomationError("Usage: /macro save <name>: <action args>; <action args>...")
        name, rest = parts
    name = check_name(name)
    steps = parse_steps(rest)
    return name, Plan(kind="macro", spec={"name": name}, steps=steps,
                      label=f"macro '{name}': {describe_steps(steps)}")


async def save(name: str, plan: Plan) -> None:
    """Store the macro. The caller must have confirmed plan.summary() if plan.dangerous."""
    existing = await db("get_macro", name)
    if existing is None and len(await db("list_macros")) >= MAX_MACROS:
        raise AutomationError(f"Too many macros (max {MAX_MACROS}).")
    await db("save_macro", name, plan.steps, plan.dangerous)


def run_summary(name: str, steps: list[dict[str, Any]]) -> str:
    lines = [f"Run macro '{name}':"]
    lines += [f"  {i}. {describe_step(s)}{'  ⚠️ DANGEROUS' if step_is_dangerous(s) else ''}"
              for i, s in enumerate(steps, 1)]
    return "\n".join(lines)


async def run_macro(bot: Any, chat_id: int, name: str, *, confirm: bool = True) -> ActionResult:
    """Run a macro, sending each step's output to chat_id. Dangerous steps need a fresh Confirm
    (danger is re-evaluated against the current registry, not the flag saved at creation)."""
    name = check_name(name)
    steps = await db("get_macro", name)
    if steps is None:
        return ActionResult(text=f"No macro named {name!r}. See /macro list.", ok=False)
    dangerous = steps_dangerous(steps)
    if dangerous:
        if not confirm:
            return ActionResult(text=f"Macro {name!r} has dangerous steps and needs confirmation.", ok=False)
        from ..confirm import ask_confirm  # lazy: keeps telegram out of import time
        if not await ask_confirm(bot, chat_id, run_summary(name, steps), action="run_macro"):
            return ActionResult(text=f"Macro {name!r} cancelled.", ok=False)
    results = await run_steps(bot, chat_id, steps, header=f"▶️ Macro '{name}' ({len(steps)} steps)",
                              approved_dangerous=dangerous)
    ok = sum(r.ok for r in results)
    return ActionResult(text=f"Macro '{name}' finished: {ok}/{len(results)} step(s) ok.",
                        ok=ok == len(results), data={"ok": ok, "total": len(results)})


async def list_text() -> str:
    macros = await db("list_macros")
    if not macros:
        return "No macros. Create one: /macro save work: <action args>; <action args>"
    lines = []
    for name, steps in macros:
        warn = " ⚠️" if steps_dangerous(steps) else ""
        lines.append(f"• {name}{warn}: {describe_steps(steps)}")
    return "\n".join(lines)


async def delete(name: str) -> bool:
    return bool(await db("delete_macro", check_name(name)))
