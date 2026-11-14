"""Reminders and scheduled jobs on PTB's JobQueue, persisted in SQLite and restored at startup.

Kinds:  remind (one-shot message) · once (one-shot steps) · every (repeating) · daily (HH:MM each day)
JobQueue job name = "auto:<task id>", job.data = task id.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, time as dtime, timedelta, tzinfo
from typing import Any

from . import (JOB_PREFIX, AutomationError, Plan, Task, db, describe_steps, fmt_duration, get_app,
               get_store, parse_steps, run_steps)

log = logging.getLogger("automation.scheduler")

MIN_INTERVAL = 60                 # /every minimum: 1 minute
MAX_INTERVAL = 31 * 86400
MAX_AHEAD = 366 * 86400           # reminders/one-shots at most a year ahead
LATE_GRACE = 3600                 # missed safe one-shot jobs still run if <= 1 h late
STARTUP_DELAY = 5                 # seconds after start before late items are delivered


def local_tz() -> tzinfo:
    try:
        from tzlocal import get_localzone
        return get_localzone()
    except Exception:  # pragma: no cover - tzlocal ships with apscheduler
        return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def now_local() -> datetime:
    return datetime.now(local_tz())


# ---------------------------------------------------------------- time parsing
_UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
          "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
          "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
          "d": 86400, "day": 86400, "days": 86400}
_DUR_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")
_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


def parse_duration(text: str) -> float:
    """'10m', '1h30m', '2 hours', '90s', '1.5h' -> seconds. Bare number = minutes."""
    t = text.strip().lower().replace(" ", "")
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return float(t) * 60
    pos, total = 0, 0.0
    for m in _DUR_PART.finditer(t):
        if m.start() != pos or m.group(2) not in _UNITS:
            raise AutomationError(f"Bad duration {text!r} (use e.g. 30s, 10m, 2h, 1h30m, 1d).")
        total += float(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    if pos != len(t) or total <= 0:
        raise AutomationError(f"Bad duration {text!r} (use e.g. 30s, 10m, 2h, 1h30m, 1d).")
    return total


def _is_duration(tok: str) -> bool:
    try:
        parse_duration(tok)
        return not re.fullmatch(r"\d+(\.\d+)?", tok)  # bare numbers only count when alone
    except AutomationError:
        return False


def take_duration(text: str) -> tuple[float, str]:
    """Consume a leading duration ('1h 30m rest' -> (5400, 'rest')). Bare '30' = 30 minutes."""
    toks = text.split()
    i, total = 0, 0.0
    while i < len(toks):
        tok = toks[i].lower()
        nxt = toks[i + 1].lower() if i + 1 < len(toks) else ""
        if re.fullmatch(r"\d+(\.\d+)?", tok) and nxt in _UNITS:      # "2 hours"
            total += float(tok) * _UNITS[nxt]
            i += 2
        elif _is_duration(tok):
            total += parse_duration(tok)
            i += 1
        elif i == 0 and re.fullmatch(r"\d+(\.\d+)?", tok):           # bare minutes
            total += float(tok) * 60
            i += 1
            break
        else:
            break
    if i == 0 or total <= 0:
        raise AutomationError(f"Expected a duration at {text.strip()[:30]!r} (e.g. 10m, 2h, 1h30m).")
    return total, " ".join(toks[i:])


def parse_clock(tok: str) -> tuple[int, int] | None:
    """'22:00', '9:30', '9pm', '9:30am' -> (h, m); None if not a time of day."""
    m = _TIME_RE.match(tok.strip().lower())
    if not m or (m.group(2) is None and m.group(3) is None):
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap == "pm" else 0)
    if h > 23 or mi > 59:
        return None
    return h, mi


def take_clock(text: str) -> tuple[tuple[int, int], str]:
    toks = text.split()
    if toks and len(toks) > 1 and toks[1].lower() in ("am", "pm"):   # "9 pm"
        toks = [toks[0] + toks[1]] + toks[2:]
    hm = parse_clock(toks[0]) if toks else None
    if hm is None:
        raise AutomationError(f"Expected a time like 22:00 or 9pm, got {text.strip()[:20]!r}.")
    return hm, " ".join(toks[1:])


def parse_when(text: str, now: datetime | None = None) -> tuple[datetime, str]:
    """Parse a leading point in time; returns (aware datetime, remaining text).
    Accepts: '10m', 'in 2h', '1h30m', '22:00', '9pm', 'today 18:00', 'tomorrow 9:00', 'tomorrow'."""
    now = now or now_local()
    text = text.strip()
    toks = text.split()
    if not toks:
        raise AutomationError("Missing time (e.g. 10m, 2h, 22:00, tomorrow 9:00).")
    first = toks[0].lower()
    if first == "in":
        secs, rest = take_duration(" ".join(toks[1:]))
        return now + timedelta(seconds=secs), rest
    if first in ("tomorrow", "today", "tmr", "tmrw"):
        day = now + timedelta(days=0 if first == "today" else 1)
        rest_txt = " ".join(toks[1:])
        rest_toks = rest_txt.split()
        if rest_toks and rest_toks[0].lower() == "at":
            rest_txt = " ".join(rest_toks[1:])
        try:
            (h, m), rest = take_clock(rest_txt)
        except AutomationError:
            if first == "today":
                raise
            (h, m), rest = (9, 0), rest_txt          # bare "tomorrow" = 09:00
        when = day.replace(hour=h, minute=m, second=0, microsecond=0)
        if when <= now:
            raise AutomationError("That time has already passed today.")
        return when, rest
    if first == "at":
        toks = toks[1:]
        text = " ".join(toks)
    if toks and (parse_clock(toks[0]) or (len(toks) > 1 and toks[1].lower() in ("am", "pm"))):
        (h, m), rest = take_clock(text)
        when = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if when <= now:
            when += timedelta(days=1)
        return when, rest
    secs, rest = take_duration(text)
    return now + timedelta(seconds=secs), rest


# ---------------------------------------------------------------- plans (parse only; nothing saved)
def plan_reminder(text: str, now: datetime | None = None) -> Plan:
    when, msg = parse_when(text, now)
    msg = msg.strip()
    if not msg:
        raise AutomationError("What should I remind you about? e.g. /remind 10m stretch")
    _check_ahead(when, now)
    return Plan(kind="remind", spec={"at": when.timestamp()},
                steps=[{"action": "notify", "args": {"text": f"⏰ Reminder: {msg}"}}],
                label=f"reminder at {when:%a %d %b %H:%M}: {msg}")


def plan_every(text: str) -> Plan:
    secs, rest = take_duration(text)
    if secs < MIN_INTERVAL:
        raise AutomationError("Minimum interval is 1 minute.")
    if secs > MAX_INTERVAL:
        raise AutomationError("Maximum interval is 31 days.")
    steps = parse_steps(rest)
    return Plan(kind="every", spec={"seconds": secs}, steps=steps,
                label=f"job every {fmt_duration(secs)}: {describe_steps(steps)}")


def plan_at(text: str, now: datetime | None = None, *, daily: bool | None = None) -> Plan:
    """'22:00 shutdown', '22:00 daily screenshot', 'tomorrow 9:00 notify hi'."""
    toks = text.split()
    if daily is None:
        daily = False
        for kw in ("daily", "everyday", "every_day"):
            if kw in [t.lower() for t in toks[:3]]:
                daily = True
                toks = [t for i, t in enumerate(toks) if not (i < 3 and t.lower() == kw)]
        text = " ".join(toks)
    if daily:
        (h, m), rest = take_clock(text.removeprefix("at ").strip())
        steps = parse_steps(rest)
        return Plan(kind="daily", spec={"time": f"{h:02d}:{m:02d}"}, steps=steps,
                    label=f"daily job at {h:02d}:{m:02d}: {describe_steps(steps)}")
    when, rest = parse_when(text, now)
    _check_ahead(when, now)
    steps = parse_steps(rest)
    return Plan(kind="once", spec={"at": when.timestamp()}, steps=steps,
                label=f"one-shot job at {when:%a %d %b %H:%M}: {describe_steps(steps)}")


def plan_in(text: str, now: datetime | None = None) -> Plan:
    """One-shot after a delay: '10m screenshot'."""
    secs, rest = take_duration(text)
    when = (now or now_local()) + timedelta(seconds=secs)
    _check_ahead(when, now)
    steps = parse_steps(rest)
    return Plan(kind="once", spec={"at": when.timestamp()}, steps=steps,
                label=f"one-shot job at {when:%a %d %b %H:%M}: {describe_steps(steps)}")


def _check_ahead(when: datetime, now: datetime | None) -> None:
    if when.timestamp() - (now or now_local()).timestamp() > MAX_AHEAD:
        raise AutomationError("That's more than a year ahead.")


# ---------------------------------------------------------------- JobQueue wiring
def job_name(task_id: int) -> str:
    return f"{JOB_PREFIX}{task_id}"


async def _job_callback(context: Any) -> None:
    await fire_task(context.bot, int(context.job.data))


async def _late_callback(context: Any) -> None:
    await fire_task(context.bot, int(context.job.data), late=True)


async def _notice_callback(context: Any) -> None:
    chat_id, text = context.job.data
    try:
        await context.bot.send_message(chat_id, text)
    except Exception:
        log.exception("could not send automation notice")


def _header(task: Task, late: bool) -> str:
    icon = {"once": "⏰", "every": "🔁", "daily": "📅", "remind": "⏰"}.get(task.kind, "⚙️")
    return f"{icon} {'(late) ' if late else ''}Automation #{task.id}: {task.label}"


async def fire_task(bot: Any, task_id: int, *, late: bool = False) -> None:
    """Run a stored task now (called by the JobQueue). Never raises."""
    try:
        task: Task | None = await db("get_task", task_id)
        if task is None:                       # cancelled meanwhile
            unschedule(task_id)
            return
        if task.kind in ("once", "remind"):
            await db("delete_task", task.id)   # one-shot: remove first so a crash can't re-fire it
        else:
            await db("set_last_fired", task.id, time.time())
        steps = task.steps
        if late and task.kind == "remind":
            steps = [{"action": "notify", "args": {"text": "(late) " + s["args"].get("text", "")}}
                     if s["action"] == "notify" else s for s in steps]
        await run_steps(bot, task.chat_id, steps, header=_header(task, late),
                        approved_dangerous=task.dangerous)
    except Exception:
        log.exception("automation task %s failed", task_id)


def schedule_task(app: Any, task: Task, *, restoring: bool = False) -> None:
    """Put a stored (non-trigger) task on the JobQueue."""
    jq = app.job_queue
    name = job_name(task.id)
    if task.kind == "every":
        secs = float(task.spec["seconds"])
        jq.run_repeating(_job_callback, interval=secs, first=secs, data=task.id, name=name)
    elif task.kind == "daily":
        h, m = (int(x) for x in task.spec["time"].split(":"))
        jq.run_daily(_job_callback, time=dtime(h, m, tzinfo=local_tz()), data=task.id, name=name)
    elif task.kind in ("once", "remind"):
        at = float(task.spec["at"])
        delay = at - time.time()
        if delay > 0:
            jq.run_once(_job_callback, when=datetime.fromtimestamp(at, local_tz()), data=task.id, name=name)
        elif not restoring:
            jq.run_once(_job_callback, when=0, data=task.id, name=name)
        else:
            _schedule_missed(app, task, -delay)
    else:
        raise AutomationError(f"cannot schedule task kind {task.kind!r}")


def _schedule_missed(app: Any, task: Task, late_by: float) -> None:
    """Startup policy for one-shots whose time passed while the bot was offline:
    reminders are delivered late; safe jobs run if <= 1 h late; dangerous jobs are NEVER run late."""
    jq = app.job_queue
    if task.kind == "remind" or (not task.dangerous and late_by <= LATE_GRACE):
        jq.run_once(_late_callback, when=STARTUP_DELAY, data=task.id, name=job_name(task.id))
        return
    get_store().delete_task(task.id)
    why = "dangerous jobs are never run late" if task.dangerous else f"more than {fmt_duration(LATE_GRACE)} late"
    jq.run_once(_notice_callback, when=STARTUP_DELAY,
                data=(task.chat_id, f"⏭ Skipped missed automation #{task.id} ({task.label}) - "
                                    f"the PC/bot was offline {fmt_duration(late_by)} past its time; {why}."),
                name=f"{JOB_PREFIX}notice:{task.id}")


def unschedule(task_id: int) -> int:
    """Remove a task's JobQueue job(s). Returns how many were removed."""
    try:
        app = get_app()
    except AutomationError:
        return 0
    jobs = app.job_queue.get_jobs_by_name(job_name(task_id))
    for j in jobs:
        j.schedule_removal()
    return len(jobs)


def restore(app: Any) -> int:
    """Re-create JobQueue jobs for every persisted task (called from modules.automation.register)."""
    n = 0
    for task in get_store().list_tasks():
        if task.kind == "trigger":
            continue
        try:
            schedule_task(app, task, restoring=True)
            n += 1
        except Exception:
            log.exception("could not restore automation #%s", task.id)
    log.info("restored %d scheduled automation(s)", n)
    return n


# ---------------------------------------------------------------- create / cancel / list
async def commit(plan: Plan, chat_id: int) -> Task:
    """Save a plan and activate it. The CALLER must have confirmed plan.summary() if plan.dangerous."""
    app = get_app() if plan.kind != "trigger" else None
    task: Task = await db("add_task", plan, chat_id)
    if app is not None:
        try:
            schedule_task(app, task)
        except Exception:
            await db("delete_task", task.id)
            raise
    log.info("automation #%s created: %s (dangerous=%s)", task.id, task.label, task.dangerous)
    return task


async def cancel(task_id: int) -> bool:
    existed = await db("delete_task", task_id)
    unschedule(task_id)
    try:
        from . import triggers
        triggers.forget(task_id)
    except Exception:
        pass
    return bool(existed)


def next_run(task: Task) -> datetime | None:
    try:
        jobs = get_app().job_queue.get_jobs_by_name(job_name(task.id))
    except AutomationError:
        return None
    for j in jobs:
        nt = getattr(j, "next_t", None)
        if nt is not None:
            return nt
    return None


def describe_task(task: Task) -> str:
    warn = " ⚠️" if task.dangerous else ""
    if task.kind == "trigger":
        line = f"#{task.id} 🔔 {task.label}"
    else:
        nr = next_run(task)
        when = f" (next {nr.astimezone(local_tz()):%a %H:%M})" if nr else ""
        icon = {"once": "⏰", "remind": "⏰", "every": "🔁", "daily": "📅"}.get(task.kind, "⚙️")
        line = f"#{task.id} {icon} {task.label}{when}"
    return line + warn


async def list_text() -> str:
    tasks: list[Task] = await db("list_tasks")
    macros = await db("list_macros")
    lines = [describe_task(t) for t in tasks] or ["No reminders, jobs or triggers."]
    if macros:
        lines.append("")
        lines.append("Macros: " + ", ".join(f"{n} ({len(s)} step{'s' if len(s) != 1 else ''})" for n, s in macros))
    lines.append("")
    lines.append("Cancel with /cancel <id>.  ⚠️ = runs approved dangerous steps unattended.")
    return "\n".join(lines)
