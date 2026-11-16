"""Automation actions (Edith-callable): reminders, scheduled jobs, triggers, macros.

These need the running Application/JobQueue, which bot.modules.automation.register(app) stores via
bot.automation.set_app(). Created items report to the owner's chat (first ALLOWED_USER_IDS entry).
Actions that can create or run dangerous steps are dangerous=True, so the caller (Edith brain)
confirms with the exact steps (they are in the call arguments) before they run.
"""
from __future__ import annotations

from typing import Literal

from .. import automation as auto
from ..automation import macros, scheduler, triggers
from .registry import ActionResult, action

STEP_HELP = ("Steps: one or more registered actions separated by ';', each written as "
             "'action_name positional args key=value', e.g. 'screenshot' or 'volume level=30; notify text=done'.")


def _err(e: Exception) -> ActionResult:
    return ActionResult(text=str(e), ok=False)


def _whole(text: str, what: Literal["when", "duration", "clock"]) -> None:
    """The time argument must be consumed completely (no stray words that could become steps)."""
    parse = {"when": scheduler.parse_when, "duration": scheduler.take_duration, "clock": scheduler.take_clock}[what]
    _, rest = parse(text.strip().removeprefix("at ") if what == "clock" else text)
    if rest.strip():
        raise auto.AutomationError(f"Could not understand {text!r} as a {'time' if what != 'duration' else what}.")


@action("notify", "Send the owner a plain text notification. Mostly used as a step inside automations "
        "(reminders, scheduled jobs, triggers, macros).", category="automation",
        params={"text": "message to send"})
async def notify(text: str = "") -> ActionResult:
    return ActionResult(text=text or "🔔 Notification")


@action("create_reminder", "Remind the owner with a Telegram message at a given time. "
        "when examples: '10m', '2h30m', 'in 45m', '22:00', '9pm', 'tomorrow 9:00'.",
        category="automation", params={"when": "time or delay", "text": "what to remind about"})
async def create_reminder(when: str, text: str) -> ActionResult:
    try:
        _whole(when, "when")
        plan = scheduler.plan_reminder(f"{when} {text}")
        task = await scheduler.commit(plan, auto.owner_chat_id())
    except auto.AutomationError as e:
        return _err(e)
    return ActionResult(text=f"✅ #{task.id} {task.label}", data={"id": task.id})


@action("schedule_action", "Schedule registered actions to run later or repeatedly. kind: 'in' (one-shot after "
        "a delay like '10m'), 'at' (one-shot at a time like '22:00' or 'tomorrow 9:00'), 'daily' (every day at "
        "HH:MM), 'weekly' (chosen weekdays at a time: 'weekdays 9:00', 'weekends 10:00', 'monday 8:00', "
        "'mon,wed,fri 18:30'), 'every' (repeat at an interval like '30m', min 1 minute). " + STEP_HELP,
        dangerous=True, category="automation",
        params={"kind": "in | at | daily | weekly | every",
                "when": "delay, time, interval, or days + time for weekly", "steps": "the steps to run"})
async def schedule_action(kind: Literal["in", "at", "daily", "weekly", "every"], when: str,
                          steps: str) -> ActionResult:
    try:
        weekly = scheduler.parse_weekly(when) if kind != "in" else None
        if weekly is not None or kind == "weekly":
            if weekly is None:
                raise auto.AutomationError(f"Could not understand {when!r}: use days + time, e.g. "
                                           "'weekdays 9:00', 'weekends 10:00', 'mon,wed,fri 18:30'.")
            if weekly[2].strip():
                raise auto.AutomationError(f"Could not understand {when!r} as days + time.")
            plan = scheduler.plan_weekly(f"{when} {steps}")
        else:
            _whole(when, "duration" if kind in ("in", "every") else "clock" if kind == "daily" else "when")
            if kind == "every":
                plan = scheduler.plan_every(f"{when} {steps}")
            elif kind == "in":
                plan = scheduler.plan_in(f"{when} {steps}")
            elif kind == "daily":
                plan = scheduler.plan_at(f"{when} {steps}", daily=True)
            else:
                plan = scheduler.plan_at(f"{when} {steps}", daily=False)
        task = await scheduler.commit(plan, auto.owner_chat_id())
    except auto.AutomationError as e:
        return _err(e)
    return ActionResult(text=f"✅ #{task.id} {task.label}", data={"id": task.id})


@action("create_trigger", "Run steps when something happens on the PC (checked every ~30 s, with a cooldown so "
        "it never spams). condition examples: 'cpu>90 for 2m', 'ram>90', 'battery<20' (while unplugged), "
        "'temp>85', 'idle>30m', 'process chrome.exe starts|stops', 'file *.pdf in Downloads' (new file; steps "
        "may use {file}), 'network lost' / 'network back', 'usb plugged' / 'usb removed' ({drive}), "
        "'pc locked' / 'pc unlocked'; optional 'cooldown 30m'. Default step is notify. " + STEP_HELP,
        dangerous=True, category="automation",
        params={"condition": "the condition", "steps": "steps to run when it fires (default notify)"})
async def create_trigger(condition: str, steps: str = "notify") -> ActionResult:
    try:
        spec, rest = triggers.parse_condition(condition)
        if rest:
            raise auto.AutomationError(f"Unexpected text in condition: {rest!r}")
        plan = triggers.plan_trigger(f"{condition} then {steps or 'notify'}")
        task = await scheduler.commit(plan, auto.owner_chat_id())
        note = await triggers.readability_note(plan)
    except auto.AutomationError as e:
        return _err(e)
    return ActionResult(text=f"✅ #{task.id} {task.label}" + (f"\n{note}" if note else ""), data={"id": task.id})


@action("list_jobs", "List the owner's reminders, scheduled jobs, triggers and macros.", category="automation")
async def list_jobs() -> ActionResult:
    return ActionResult(text=await scheduler.list_text())


@action("cancel_job", "Cancel a reminder, scheduled job or trigger by its id (see list_jobs).",
        category="automation", params={"job_id": "the #id"})
async def cancel_job(job_id: int) -> ActionResult:
    if await scheduler.cancel(job_id):
        return ActionResult(text=f"🗑 Cancelled #{job_id}.")
    return ActionResult(text=f"No automation #{job_id}.", ok=False)


@action("save_macro", "Save (or overwrite) a named macro: a list of steps run in order by run_macro. " + STEP_HELP,
        dangerous=True, category="automation",
        params={"name": "macro name (a-z, 0-9, _ -)", "steps": "the steps"})
async def save_macro(name: str, steps: str) -> ActionResult:
    try:
        mname, plan = macros.plan_macro(f"{name}: {steps}")
        await macros.save(mname, plan)
    except auto.AutomationError as e:
        return _err(e)
    return ActionResult(text=f"💾 Saved {plan.label}")


@action("run_macro", "Run a saved macro. If it contains dangerous steps the owner is asked to confirm them.",
        dangerous=True, category="automation", params={"name": "macro name"})
async def run_macro(name: str) -> ActionResult:
    try:
        app = auto.get_app()
        return await macros.run_macro(app.bot, auto.owner_chat_id(), name, confirm=True)
    except auto.AutomationError as e:
        return _err(e)


@action("list_macros", "List saved macros and their steps.", category="automation")
async def list_macros() -> ActionResult:
    return ActionResult(text=await macros.list_text())


@action("delete_macro", "Delete a saved macro.", category="automation", params={"name": "macro name"})
async def delete_macro(name: str) -> ActionResult:
    try:
        ok = await macros.delete(name)
    except auto.AutomationError as e:
        return _err(e)
    return ActionResult(text=f"🗑 Deleted macro {name!r}." if ok else f"No macro named {name!r}.", ok=ok)
