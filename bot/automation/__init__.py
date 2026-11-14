"""Automations: reminders, scheduled jobs, triggers and macros.

Layout
  bot/automation/__init__.py   SQLite store, step parsing/running, runtime (app) reference
  bot/automation/scheduler.py  /remind /every /at -> PTB JobQueue jobs (persisted, restored at startup)
  bot/automation/triggers.py   /when conditions polled every AUTOMATION_POLL_SECONDS
  bot/automation/macros.py     named step lists (/macro save|run|list|del)

A *step* is {"action": <registered action name>, "args": {...}}. Steps are validated against
bot.actions.registry when created. SECURITY:
  * creating a job/trigger/macro whose steps include a dangerous action must be confirmed by the
    owner at creation time (modules use bot.confirm.ask_confirm; Edith confirms because the
    creating actions are registered dangerous=True). The approval is stored with the task.
  * scheduled/triggered tasks approved at creation run unattended, but the owner is notified
    before every dangerous step runs. A dangerous step in a task that was NOT approved is skipped.
  * macro runs with dangerous steps confirm again at run time (see macros.run_macro).
  * automation-management actions cannot be used as steps (no recursion / approval laundering),
    except `notify`.
This package does not import `telegram` at module level.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..actions.registry import REGISTRY, ActionResult, describe_call, run_action, validate_args

log = logging.getLogger("automation")

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = Path(os.getenv("AUTOMATION_DB", str(ROOT / "data" / "automation.db")))
MAX_TASKS = 100
MAX_STEPS = 20
STEP_ALLOWED_AUTOMATION_ACTIONS = {"notify"}
JOB_PREFIX = "auto:"


class AutomationError(ValueError):
    """User-facing error (bad syntax, unknown action, limits...)."""


# ---------------------------------------------------------------- models
@dataclass
class Task:
    id: int
    kind: str                 # remind | once | every | daily | trigger
    spec: dict[str, Any]
    steps: list[dict[str, Any]]
    label: str
    chat_id: int
    dangerous: bool           # owner approved dangerous steps at creation
    created: float
    last_fired: float | None = None


@dataclass
class Plan:
    """A parsed-but-not-yet-saved task. Confirm `plan.summary()` first if `plan.dangerous`."""
    kind: str
    spec: dict[str, Any]
    steps: list[dict[str, Any]]
    label: str
    notes: list[str] = field(default_factory=list)

    @property
    def dangerous(self) -> bool:
        return steps_dangerous(self.steps)

    def summary(self) -> str:
        lines = [f"Create {self.label}"]
        lines += [f"  {i}. {describe_step(s)}{'  ⚠️ DANGEROUS' if step_is_dangerous(s) else ''}"
                  for i, s in enumerate(self.steps, 1)]
        if self.dangerous and self.kind != "macro":
            lines.append("Dangerous steps will run UNATTENDED (you get a notice each time).")
        return "\n".join(lines)


# ---------------------------------------------------------------- SQLite store
class Store:
    """Tiny SQLite persistence. Each call opens its own connection (thread-safe with run_blocking)."""

    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS tasks(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL, spec TEXT NOT NULL, steps TEXT NOT NULL,
                    label TEXT NOT NULL, chat_id INTEGER NOT NULL,
                    dangerous INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, last_fired REAL);
                CREATE TABLE IF NOT EXISTS macros(
                    name TEXT PRIMARY KEY, steps TEXT NOT NULL,
                    dangerous INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
            """)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _exec(self, sql: str, args: tuple = ()) -> tuple[list[sqlite3.Row], int | None]:
        with self._lock:
            c = self._conn()
            try:
                with c:
                    cur = c.execute(sql, args)
                    return cur.fetchall(), cur.lastrowid
            finally:
                c.close()

    def _run(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self._exec(sql, args)[0]

    @staticmethod
    def _task(r: sqlite3.Row) -> Task:
        return Task(id=r["id"], kind=r["kind"], spec=json.loads(r["spec"]), steps=json.loads(r["steps"]),
                    label=r["label"], chat_id=r["chat_id"], dangerous=bool(r["dangerous"]),
                    created=r["created"], last_fired=r["last_fired"])

    # tasks
    def add_task(self, plan: Plan, chat_id: int) -> Task:
        if self.count_tasks() >= MAX_TASKS:
            raise AutomationError(f"Too many automations (max {MAX_TASKS}). /cancel some first.")
        now = time.time()
        _, new_id = self._exec(
            "INSERT INTO tasks(kind, spec, steps, label, chat_id, dangerous, created) VALUES(?,?,?,?,?,?,?)",
            (plan.kind, json.dumps(plan.spec), json.dumps(plan.steps), plan.label, int(chat_id),
             int(plan.dangerous), now))
        return self.get_task(new_id)  # type: ignore[arg-type,return-value]

    def get_task(self, task_id: int) -> Task | None:
        rows = self._run("SELECT * FROM tasks WHERE id=?", (int(task_id),))
        return self._task(rows[0]) if rows else None

    def list_tasks(self, kind: str | None = None) -> list[Task]:
        if kind:
            rows = self._run("SELECT * FROM tasks WHERE kind=? ORDER BY id", (kind,))
        else:
            rows = self._run("SELECT * FROM tasks ORDER BY id")
        return [self._task(r) for r in rows]

    def count_tasks(self) -> int:
        return self._run("SELECT COUNT(*) AS n FROM tasks")[0]["n"]

    def delete_task(self, task_id: int) -> bool:
        existed = self.get_task(task_id) is not None
        self._run("DELETE FROM tasks WHERE id=?", (int(task_id),))
        return existed

    def set_last_fired(self, task_id: int, ts: float) -> None:
        self._run("UPDATE tasks SET last_fired=? WHERE id=?", (ts, int(task_id)))

    # macros
    def save_macro(self, name: str, steps: list[dict[str, Any]], dangerous: bool) -> None:
        self._run("INSERT OR REPLACE INTO macros(name, steps, dangerous, created) VALUES(?,?,?,?)",
                  (name, json.dumps(steps), int(dangerous), time.time()))

    def get_macro(self, name: str) -> list[dict[str, Any]] | None:
        rows = self._run("SELECT steps FROM macros WHERE name=?", (name,))
        return json.loads(rows[0]["steps"]) if rows else None

    def list_macros(self) -> list[tuple[str, list[dict[str, Any]]]]:
        return [(r["name"], json.loads(r["steps"])) for r in self._run("SELECT * FROM macros ORDER BY name")]

    def delete_macro(self, name: str) -> bool:
        existed = self.get_macro(name) is not None
        self._run("DELETE FROM macros WHERE name=?", (name,))
        return existed


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store(DB_PATH)
    return _store


def set_store(store: Store | None) -> None:
    """Swap the store (tests use a temp DB)."""
    global _store
    _store = store


async def db(method_name: str, *args: Any) -> Any:
    """Run a Store method off the event loop."""
    from ..utils import run_blocking
    return await run_blocking(getattr(get_store(), method_name), *args)


# ---------------------------------------------------------------- runtime (Application reference)
_APP: Any = None


def set_app(app: Any) -> None:
    global _APP
    _APP = app


def get_app() -> Any:
    if _APP is None or getattr(_APP, "job_queue", None) is None:
        raise AutomationError("Automation engine is not running (no Application/JobQueue registered).")
    return _APP


def owner_chat_id() -> int:
    """Chat used for automations created without a chat (e.g. by Edith): the first allowed user."""
    from ..config import settings
    return sorted(settings.allowed_user_ids)[0]


# ---------------------------------------------------------------- steps: parsing / describing
_TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def parse_call(text: str) -> dict[str, Any]:
    """Parse 'action arg1 arg2 key=value' into a validated step dict.

    key=value sets a named parameter. Remaining tokens fill the REQUIRED parameters in order
    (or, if the action has no unfilled required parameters, the optional ones). The last
    parameter filled this way swallows the rest of the text when it is a string (raw, quotes
    kept - handy for shell commands) or a list.
    """
    text = text.strip()
    if not text:
        raise AutomationError("Empty step.")
    tokens = [(m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]
    name = tokens[0][0].lstrip("/").lower()
    act = REGISTRY.get(name)
    if act is None:
        raise AutomationError(f"Unknown action {name!r}. Known: {', '.join(sorted(REGISTRY)) or '(none)'}")
    if act.category == "automation" and name not in STEP_ALLOWED_AUTOMATION_ACTIONS:
        raise AutomationError(f"{name!r} manages automations and cannot be used as a step.")
    props: dict[str, Any] = act.input_schema["properties"]
    kwargs: dict[str, Any] = {}
    positional: list[tuple[str, int, int]] = []
    last_kw: tuple[str, int] | None = None   # (param, offset of its value) for a trailing string kwarg
    for tok, start, end in tokens[1:]:
        key, sep, val = tok.partition("=")
        if sep and key in props and key not in kwargs and not positional:
            kwargs[key] = _unquote(val)
            sch = props[key]
            last_kw = (key, start + len(key) + 1) if sch.get("type") == "string" and "enum" not in sch else None
        else:
            positional.append((tok, start, end))
    if positional:
        free = [p for p in act.input_schema["required"] if p not in kwargs]
        if not free:
            free = [p for p in props if p not in kwargs]
        if not free and last_kw is not None:      # "notify text=hello world"
            kwargs[last_kw[0]] = _unquote(text[last_kw[1]:].strip())
            positional, free = [], []
        elif not free:
            raise AutomationError(f"{name} takes no (more) arguments.")
        for i, pname in enumerate(free):
            if not positional:
                break
            sch = props[pname]
            last = i == len(free) - 1
            if last and sch.get("type") == "string" and "enum" not in sch:
                kwargs[pname] = text[positional[0][1]:].strip()
                if len(positional) == 1:
                    kwargs[pname] = _unquote(kwargs[pname])
                positional = []
            elif last and sch.get("type") == "array":
                kwargs[pname] = [_unquote(t) for t, _, _ in positional]
                positional = []
            else:
                kwargs[pname] = _unquote(positional.pop(0)[0])
        if positional:
            raise AutomationError(f"{name}: too many arguments ({' '.join(t for t, _, _ in positional)}).")
    try:
        clean = validate_args(act, kwargs)
    except ValueError as e:
        raise AutomationError(str(e)) from None
    return {"action": name, "args": clean}


def parse_steps(text: str) -> list[dict[str, Any]]:
    """'a x; b y=1' -> [step, step]. Steps are separated by ';'."""
    parts = [p.strip() for p in text.split(";") if p.strip()]
    if not parts:
        raise AutomationError("No steps given.")
    if len(parts) > MAX_STEPS:
        raise AutomationError(f"Too many steps (max {MAX_STEPS}).")
    return [parse_call(p) for p in parts]


def step_is_dangerous(step: dict[str, Any]) -> bool:
    act = REGISTRY.get(step["action"])
    # unknown action at check time -> treat as dangerous (fail safe)
    return True if act is None else act.dangerous


def steps_dangerous(steps: list[dict[str, Any]]) -> bool:
    return any(step_is_dangerous(s) for s in steps)


def describe_step(step: dict[str, Any]) -> str:
    return describe_call(step["action"], step.get("args", {}))


def describe_steps(steps: list[dict[str, Any]]) -> str:
    return "; ".join(describe_step(s) for s in steps)


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s <= 0:
        return "0s"
    out = []
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if s >= n:
            out.append(f"{s // n}{unit}")
            s %= n
    return "".join(out)


# ---------------------------------------------------------------- steps: running
async def _safe_send(bot: Any, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except Exception:  # never let a notification failure kill a job
        log.exception("automation: could not send message")


async def run_steps(bot: Any, chat_id: int, steps: list[dict[str, Any]], *, header: str = "",
                    approved_dangerous: bool) -> list[ActionResult]:
    """Run steps in order, sending each result to chat_id. Never raises.

    - If every step is `notify`, no header is sent; an empty notify text is replaced by the header.
    - Dangerous steps run only when approved_dangerous; the owner is told before each one runs.
    """
    from ..utils import send_result

    all_notify = all(s["action"] == "notify" for s in steps)
    if header and not all_notify:
        await _safe_send(bot, chat_id, header)
    results: list[ActionResult] = []
    for step in steps:
        name, args = step["action"], dict(step.get("args") or {})
        act = REGISTRY.get(name)
        if act is None:
            res = ActionResult(text=f"Step skipped: action {name!r} is not available.", ok=False)
        elif act.dangerous and not approved_dangerous:
            res = ActionResult(text=f"Step skipped: {describe_step(step)} is dangerous and was not approved.",
                               ok=False)
        else:
            if name == "notify" and not args.get("text"):
                args["text"] = header or "🔔 Notification"
            if act.dangerous:
                await _safe_send(bot, chat_id, f"⚠️ Running approved dangerous step: {describe_call(name, args)}")
            res = await run_action(name, **args)
        results.append(res)
        try:
            await send_result(bot, chat_id, res)
        except Exception:
            log.exception("automation: could not send result of %s", name)
    return results
