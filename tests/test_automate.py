"""Tests for bot/automation/* + actions/automation.py + modules/automation.py.
Uses a temp SQLite DB, a fake JobQueue and a fake Bot - nothing real is scheduled or run."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import bot.actions.automation  # noqa: F401  (registers actions)
from bot import automation as auto
from bot.actions import registry as r
from bot.automation import macros, scheduler, triggers

CALLS: list[tuple[str, dict]] = []


# ---------------------------------------------------------------- test actions
@r.action("t_echo", "Echo text (test).", category="test")
async def t_echo(text: str) -> r.ActionResult:
    CALLS.append(("t_echo", {"text": text}))
    return r.ActionResult(text=f"echo {text}")


@r.action("t_vol", "Set volume (test).", category="test")
async def t_vol(level: int, mute: bool = False) -> r.ActionResult:
    CALLS.append(("t_vol", {"level": level, "mute": mute}))
    return r.ActionResult(text=f"vol {level}")


@r.action("t_boom", "Dangerous thing (test).", dangerous=True, category="test")
async def t_boom(target: str = "pc") -> r.ActionResult:
    CALLS.append(("t_boom", {"target": target}))
    return r.ActionResult(text="boom done")


# ---------------------------------------------------------------- fakes
class FakeJob:
    def __init__(self, callback, name, data, **info):
        self.callback, self.name, self.data, self.info = callback, name, data, info
        self.removed = False
        self.next_t = None

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    def __init__(self):
        self.jobs: list[FakeJob] = []

    def _add(self, callback, name, data, **info):
        j = FakeJob(callback, name, data, **info)
        self.jobs.append(j)
        return j

    def run_once(self, callback, when, data=None, name=None, **kw):
        return self._add(callback, name, data, kind="once", when=when)

    def run_repeating(self, callback, interval, first=None, last=None, data=None, name=None, **kw):
        return self._add(callback, name, data, kind="repeating", interval=interval, first=first)

    def run_daily(self, callback, time, days=(0, 1, 2, 3, 4, 5, 6), data=None, name=None, **kw):
        return self._add(callback, name, data, kind="daily", time=time)

    def get_jobs_by_name(self, name):
        return tuple(j for j in self.jobs if j.name == name and not j.removed)


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return SimpleNamespace(edit_text=self._noop)

    async def send_document(self, chat_id, doc, **kw):
        self.sent.append((chat_id, "<document>"))

    async def _noop(self, *a, **k):
        return None

    def texts(self) -> list[str]:
        return [t for _, t in self.sent]


class FakeApp:
    def __init__(self):
        self.job_queue = FakeJobQueue()
        self.bot = FakeBot()
        self.handlers = []

    def add_handler(self, h, *a, **k):
        self.handlers.append(h)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    auto.set_store(auto.Store(tmp_path / "automation.db"))
    app = FakeApp()
    auto.set_app(app)
    triggers._STATE.clear()
    CALLS.clear()
    yield app
    auto.set_store(None)
    auto.set_app(None)


def ctx(app, job):
    return SimpleNamespace(bot=app.bot, job=job)


TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 14, 15, 30, tzinfo=TZ)


# ---------------------------------------------------------------- time parsing
def test_parse_duration():
    assert scheduler.parse_duration("10m") == 600
    assert scheduler.parse_duration("1h30m") == 5400
    assert scheduler.parse_duration("90s") == 90
    assert scheduler.parse_duration("2") == 120
    assert scheduler.take_duration("2 hours do it") == (7200, "do it")
    assert scheduler.take_duration("1h 30m screenshot") == (5400, "screenshot")
    with pytest.raises(auto.AutomationError):
        scheduler.parse_duration("soon")


@pytest.mark.parametrize("text,expected,rest", [
    ("10m stretch", NOW + timedelta(minutes=10), "stretch"),
    ("in 2h call", NOW + timedelta(hours=2), "call"),
    ("22:00 sleep", NOW.replace(hour=22, minute=0), "sleep"),
    ("9:00 standup", (NOW + timedelta(days=1)).replace(hour=9, minute=0), "standup"),
    ("9pm tea", NOW.replace(hour=21, minute=0), "tea"),
    ("9 pm tea", NOW.replace(hour=21, minute=0), "tea"),
    ("tomorrow 9:00 call mom", (NOW + timedelta(days=1)).replace(hour=9, minute=0), "call mom"),
    ("tomorrow call mom", (NOW + timedelta(days=1)).replace(hour=9, minute=0), "call mom"),
    ("at 16:00 x", NOW.replace(hour=16, minute=0), "x"),
])
def test_parse_when(text, expected, rest):
    when, left = scheduler.parse_when(text, NOW)
    assert when == expected
    assert left == rest


def test_parse_when_errors():
    with pytest.raises(auto.AutomationError):
        scheduler.parse_when("whenever", NOW)
    with pytest.raises(auto.AutomationError):
        scheduler.parse_when("today 9:00 x", NOW)  # already passed


# ---------------------------------------------------------------- step parsing
def test_parse_call_positional_and_kwargs():
    assert auto.parse_call("t_echo hello   world") == {"action": "t_echo", "args": {"text": "hello   world"}}
    assert auto.parse_call('t_echo "quoted"') == {"action": "t_echo", "args": {"text": "quoted"}}
    assert auto.parse_call("t_vol 30") == {"action": "t_vol", "args": {"level": 30}}
    assert auto.parse_call("t_vol level=40 mute=yes") == {"action": "t_vol", "args": {"level": 40, "mute": True}}
    assert auto.parse_call("/T_BOOM") == {"action": "t_boom", "args": {}}
    assert auto.parse_call("t_boom server") == {"action": "t_boom", "args": {"target": "server"}}
    assert auto.parse_call("notify") == {"action": "notify", "args": {}}
    assert auto.parse_call("notify text=hi there")["args"] == {"text": "hi there"}


def test_parse_call_errors():
    with pytest.raises(auto.AutomationError, match="Unknown action"):
        auto.parse_call("nope 1")
    with pytest.raises(auto.AutomationError):
        auto.parse_call("t_vol loud")
    with pytest.raises(auto.AutomationError, match="cannot be used as a step"):
        auto.parse_call("run_macro x")
    with pytest.raises(auto.AutomationError, match="too many"):
        auto.parse_call("t_vol 3 4 5")
    with pytest.raises(auto.AutomationError):
        auto.parse_steps(" ; ")


def test_steps_and_danger():
    steps = auto.parse_steps("t_echo hi; t_boom")
    assert [s["action"] for s in steps] == ["t_echo", "t_boom"]
    assert auto.steps_dangerous(steps)
    assert not auto.steps_dangerous(steps[:1])
    assert auto.step_is_dangerous({"action": "vanished", "args": {}})  # unknown = fail safe


# ---------------------------------------------------------------- scheduler
async def test_reminder_commit_and_fire(env):
    plan = scheduler.plan_reminder("10m drink water")
    assert plan.kind == "remind" and not plan.dangerous
    task = await scheduler.commit(plan, 42)
    job = env.job_queue.get_jobs_by_name(f"auto:{task.id}")[0]
    assert job.info["kind"] == "once" and job.data == task.id
    await job.callback(ctx(env, job))
    assert env.bot.texts() == ["⏰ Reminder: drink water"]
    assert env.bot.sent[0][0] == 42
    assert auto.get_store().get_task(task.id) is None  # one-shot removed


async def test_every_min_interval_and_fire(env):
    with pytest.raises(auto.AutomationError, match="Minimum"):
        scheduler.plan_every("30s t_echo hi")
    task = await scheduler.commit(scheduler.plan_every("5m t_echo hi; t_vol 10"), 7)
    job = env.job_queue.get_jobs_by_name(f"auto:{task.id}")[0]
    assert job.info["interval"] == 300
    await job.callback(ctx(env, job))
    assert CALLS == [("t_echo", {"text": "hi"}), ("t_vol", {"level": 10, "mute": False})]
    texts = env.bot.texts()
    assert texts[0].startswith("🔁 Automation #") and "echo hi" in texts and "vol 10" in texts
    stored = auto.get_store().get_task(task.id)
    assert stored is not None and stored.last_fired  # repeating job stays


async def test_at_daily_and_once(env):
    daily = scheduler.plan_at("08:30 daily t_echo morning")
    assert daily.kind == "daily" and daily.spec == {"time": "08:30"}
    t = await scheduler.commit(daily, 1)
    job = env.job_queue.get_jobs_by_name(f"auto:{t.id}")[0]
    assert job.info["kind"] == "daily" and (job.info["time"].hour, job.info["time"].minute) == (8, 30)
    assert job.info["time"].tzinfo is not None
    once = scheduler.plan_at("22:00 t_boom", NOW)
    assert once.kind == "once" and once.dangerous and "DANGEROUS" in once.summary()


async def test_dangerous_step_needs_approval(env):
    steps = auto.parse_steps("t_boom; t_echo ok")
    await auto.run_steps(env.bot, 1, steps, approved_dangerous=False)
    assert CALLS == [("t_echo", {"text": "ok"})]
    assert any("not approved" in t for t in env.bot.texts())
    env.bot.sent.clear()
    CALLS.clear()
    await auto.run_steps(env.bot, 1, steps, header="hdr", approved_dangerous=True)
    assert CALLS[0][0] == "t_boom"
    texts = env.bot.texts()
    assert texts[0] == "hdr" and texts[1].startswith("⚠️ Running approved dangerous step")


async def test_unapproved_task_skips_newly_dangerous(env):
    # task saved as safe; at run time the step is dangerous -> skipped (stored approval is respected)
    plan = scheduler.plan_every("5m t_echo x")
    plan.steps = [{"action": "t_boom", "args": {}}]
    store = auto.get_store()
    task = store.add_task(auto.Plan("every", {"seconds": 300}, [{"action": "t_echo", "args": {"text": "x"}}],
                                    "job"), 1)
    store._run("UPDATE tasks SET steps=? WHERE id=?", (json.dumps(plan.steps), task.id))
    await scheduler.fire_task(env.bot, task.id)
    assert CALLS == []


async def test_cancel(env):
    task = await scheduler.commit(scheduler.plan_every("1h t_echo hi"), 1)
    assert await scheduler.cancel(task.id)
    assert env.job_queue.get_jobs_by_name(f"auto:{task.id}") == ()
    assert not await scheduler.cancel(task.id)
    res = await r.run_action("cancel_job", job_id=999)
    assert not res.ok


async def test_restore_policies(env):
    store = auto.get_store()
    past = time.time() - 7200
    future = time.time() + 3600
    rem = store.add_task(auto.Plan("remind", {"at": past}, [{"action": "notify", "args": {"text": "⏰ Reminder: x"}}],
                                   "rem"), 5)
    dang = store.add_task(auto.Plan("once", {"at": past}, [{"action": "t_boom", "args": {}}], "boom"), 5)
    assert dang.dangerous
    safe_old = store.add_task(auto.Plan("once", {"at": past}, [{"action": "t_echo", "args": {"text": "a"}}], "s"), 5)
    fut = store.add_task(auto.Plan("once", {"at": future}, [{"action": "t_echo", "args": {"text": "b"}}], "f"), 5)
    trig = store.add_task(auto.Plan("trigger", {"metric": "ram", "op": ">", "value": 90, "for": 0,
                                                "cooldown": 900}, [{"action": "notify", "args": {}}], "t"), 5)
    fresh = FakeApp()
    auto.set_app(fresh)
    assert scheduler.restore(fresh) == 4
    jq = fresh.job_queue
    # late reminder delivered (marked late)
    rjob = jq.get_jobs_by_name(f"auto:{rem.id}")[0]
    await rjob.callback(ctx(fresh, rjob))
    assert fresh.bot.texts() == ["(late) ⏰ Reminder: x"]
    # dangerous + safe-but-too-late: dropped, owner notified, never run
    assert store.get_task(dang.id) is None and store.get_task(safe_old.id) is None
    notices = [j for j in jq.jobs if j.name and "notice" in j.name]
    assert len(notices) == 2
    for j in notices:
        await j.callback(ctx(fresh, j))
    assert sum("Skipped missed automation" in t for t in fresh.bot.texts()) == 2
    assert not any(c[0] == "t_boom" for c in CALLS)
    # future job scheduled normally; triggers are not JobQueue jobs
    assert jq.get_jobs_by_name(f"auto:{fut.id}")[0].info["kind"] == "once"
    assert jq.get_jobs_by_name(f"auto:{trig.id}") == ()


async def test_list_text(env):
    await scheduler.commit(scheduler.plan_every("1h t_echo hi"), 1)
    await scheduler.commit(triggers.plan_trigger("ram>95"), 1)
    txt = await scheduler.list_text()
    assert "#1 🔁" in txt and "#2 🔔 when ram>95%" in txt


# ---------------------------------------------------------------- triggers
@pytest.mark.parametrize("text,metric,extra,rest", [
    ("cpu>90 for 2m notify", "cpu", {"op": ">", "value": 90, "for": 120}, "notify"),
    ("ram>90", "ram", {"value": 90}, ""),
    ("battery<20 cooldown 30m then t_echo low", "battery", {"op": "<", "cooldown": 1800}, "t_echo low"),
    ("temp>85c for 1m cooldown 5m", "temp", {"for": 60, "cooldown": 300}, ""),
    ("idle>30m t_boom", "idle", {"value": 1800}, "t_boom"),
    ("process chrome.exe starts", "process", {"process": "chrome.exe", "event": "starts"}, ""),
    ('process "my app.exe" stops notify', "process", {"process": "my app.exe", "event": "stops"}, "notify"),
])
def test_parse_condition(text, metric, extra, rest):
    spec, left = triggers.parse_condition(text)
    assert spec["metric"] == metric
    for k, v in extra.items():
        assert spec[k] == v
    assert left == rest


def test_parse_condition_errors():
    for bad in ("gpu>5", "cpu>150", "idle<5m", "process x maybe", "whatever"):
        with pytest.raises(auto.AutomationError):
            triggers.parse_condition(bad)


def test_evaluate_for_window_and_cooldown():
    spec, _ = triggers.parse_condition("cpu>90 for 2m cooldown 10m")
    st = triggers.TriggerState()
    assert triggers.evaluate(spec, st, {"cpu": 95}, 0) is None      # starts holding
    assert triggers.evaluate(spec, st, {"cpu": 95}, 60) is None     # not long enough
    assert triggers.evaluate(spec, st, {"cpu": 50}, 90) is None     # dropped -> window resets
    assert triggers.evaluate(spec, st, {"cpu": 95}, 100) is None
    assert "cpu is 95%" in triggers.evaluate(spec, st, {"cpu": 95}, 220)
    for t in range(250, 820, 30):                                  # stays high: cooldown blocks spam
        assert triggers.evaluate(spec, st, {"cpu": 99}, t) is None
    assert triggers.evaluate(spec, st, {"cpu": 99}, 840) is not None  # 10 min later, held again >= 2m


def test_evaluate_battery_and_process():
    spec, _ = triggers.parse_condition("battery<20")
    st = triggers.TriggerState()
    assert triggers.evaluate(spec, st, {"battery": (10.0, True)}, 0) is None   # charging -> quiet
    assert triggers.evaluate(spec, st, {"battery": (10.0, False)}, 1) is not None
    assert triggers.evaluate(spec, st, {"battery": None}, 2) is None           # unreadable -> quiet
    spec, _ = triggers.parse_condition("process chrome starts")
    st = triggers.TriggerState()
    assert triggers.evaluate(spec, st, {"process": {"chrome.exe"}}, 0) is None  # baseline only
    assert triggers.evaluate(spec, st, {"process": set()}, 10) is None
    assert triggers.evaluate(spec, st, {"process": {"chrome.exe"}}, 100) == "chrome started"
    assert triggers.evaluate(spec, st, {"process": set()}, 110) is None
    assert triggers.evaluate(spec, st, {"process": {"chrome.exe"}}, 120) is None  # within 60 s cooldown


async def test_poll_once_fires_and_persists(env, monkeypatch):
    snap = {"ram": 97.0}
    monkeypatch.setattr(triggers, "sample", lambda metrics: {m: snap.get(m) for m in metrics})
    t1 = await scheduler.commit(triggers.plan_trigger("ram>90"), 9)
    t2 = await scheduler.commit(triggers.plan_trigger("ram>90 t_boom"), 9)  # created without approval? no:
    assert t2.dangerous  # commit records plan.dangerous; modules confirm before commit
    assert await triggers.poll_once(env.bot, now=1000.0) == 2
    texts = env.bot.texts()
    assert any(t.startswith(f"🔔 Trigger #{t1.id}: ram>90%") and "ram is 97%" in t for t in texts)
    assert ("t_boom", {"target": "pc"}) in CALLS
    assert auto.get_store().get_task(t1.id).last_fired == 1000.0
    assert await triggers.poll_once(env.bot, now=1060.0) == 0   # cooldown


def test_trigger_poll_job_started_once(env):
    triggers.start(env)
    triggers.start(env)
    assert len(env.job_queue.get_jobs_by_name(triggers.POLL_JOB)) == 1


def test_sample_handles_errors(monkeypatch):
    def broken():
        raise RuntimeError("no sensor")
    monkeypatch.setattr(triggers, "_temperature", broken)
    out = triggers.sample({"temp", "ram"})
    assert out["temp"] is None and isinstance(out["ram"], float)


# ---------------------------------------------------------------- macros
async def test_macro_save_run_and_confirm(env, monkeypatch):
    import bot.confirm as confirm
    answers: list[str] = []

    async def fake_confirm(bot, chat_id, summary, **kw):
        answers.append(summary)
        return answers_ok.pop(0)

    monkeypatch.setattr(confirm, "ask_confirm", fake_confirm)
    name, plan = macros.plan_macro("Work: t_echo start; t_vol 20")
    assert name == "work" and not plan.dangerous
    await macros.save(name, plan)
    answers_ok: list[bool] = []
    res = await macros.run_macro(env.bot, 3, "work")
    assert res.ok and res.data == {"ok": 2, "total": 2} and answers == []   # safe -> no confirm
    name, plan = macros.plan_macro("night: t_echo bye; t_boom")
    assert plan.dangerous
    await macros.save(name, plan)
    CALLS.clear()
    answers_ok = [False]
    res = await macros.run_macro(env.bot, 3, "night")
    assert not res.ok and CALLS == [] and "t_boom" in answers[-1]           # declined -> nothing ran
    answers_ok = [True]
    res = await macros.run_macro(env.bot, 3, "night")
    assert res.ok and [c[0] for c in CALLS] == ["t_echo", "t_boom"]
    assert "night" in await macros.list_text() and "⚠️" in await macros.list_text()
    assert await macros.delete("night") and not await macros.delete("night")
    with pytest.raises(auto.AutomationError):
        macros.check_name("bad name!")


# ---------------------------------------------------------------- actions (Edith layer)
EXPECTED = {"notify": False, "create_reminder": False, "schedule_action": True, "create_trigger": True,
            "list_jobs": False, "cancel_job": False, "save_macro": True, "run_macro": True,
            "list_macros": False, "delete_macro": False}


def test_actions_registered():
    for name, dangerous in EXPECTED.items():
        act = r.REGISTRY[name]
        assert act.dangerous is dangerous, name
        assert act.category == "automation"
        sch = act.tool_schema()
        json.dumps(sch)
        assert sch["input_schema"]["type"] == "object"


async def test_actions_create_and_list(env):
    owner = auto.owner_chat_id()
    res = await r.run_action("create_reminder", when="10m", text="stretch")
    assert res.ok, res.text
    task = auto.get_store().get_task(res.data["id"])
    assert task.chat_id == owner and task.kind == "remind"
    res = await r.run_action("create_reminder", when="10m blah", text="x")
    assert not res.ok
    res = await r.run_action("schedule_action", kind="every", when="30m", steps="t_echo hi")
    assert res.ok and auto.get_store().get_task(res.data["id"]).kind == "every"
    res = await r.run_action("schedule_action", kind="daily", when="07:15", steps="t_boom")
    assert res.ok and auto.get_store().get_task(res.data["id"]).dangerous
    res = await r.run_action("schedule_action", kind="every", when="10s", steps="t_echo hi")
    assert not res.ok and "Minimum" in res.text
    res = await r.run_action("schedule_action", kind="in", when="5m", steps="run_macro x")
    assert not res.ok
    res = await r.run_action("create_trigger", condition="ram>95 for 1m", steps="notify")
    assert res.ok
    res = await r.run_action("create_trigger", condition="ram>95 t_boom")
    assert not res.ok   # steps must go in `steps`, not smuggled in the condition
    listing = await r.run_action("list_jobs")
    assert "stretch" in listing.text and "ram>95%" in listing.text
    res = await r.run_action("save_macro", name="m1", steps="t_echo a")
    assert res.ok
    assert "m1" in (await r.run_action("list_macros")).text
    res = await r.run_action("run_macro", name="m1")
    assert res.ok and ("t_echo", {"text": "a"}) in CALLS
    assert (await r.run_action("delete_macro", name="m1")).ok
    assert (await r.run_action("notify", text="yo")).text == "yo"


async def test_actions_without_app():
    auto.set_app(None)
    res = await r.run_action("create_reminder", when="10m", text="x")
    assert not res.ok and "not running" in res.text


# ---------------------------------------------------------------- module (Telegram layer)
def _update(text: str, chat_id: int = 111111111):
    replies: list[str] = []

    async def reply_text(t, **kw):
        replies.append(t)

    async def reply_document(*a, **kw):
        replies.append("<doc>")

    msg = SimpleNamespace(text=text, reply_text=reply_text, reply_document=reply_document)
    upd = SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(id=chat_id, type="private"),
                          effective_user=SimpleNamespace(id=chat_id, username="o"), callback_query=None)
    return upd, replies


async def test_module_confirms_dangerous_creation(env, monkeypatch):
    import bot.modules.automation as mod
    from bot.config import settings
    owner = sorted(settings.allowed_user_ids)[0]
    prompts: list[str] = []
    verdict = [False]

    async def fake_confirm(bot, chat_id, summary, **kw):
        prompts.append(summary)
        return verdict[0]

    monkeypatch.setattr(mod, "ask_confirm", fake_confirm)
    context = SimpleNamespace(bot=env.bot, args=[])
    upd, replies = _update("/at 22:00 t_boom", owner)
    await mod.cmd_at(upd, context)
    assert prompts and "t_boom" in prompts[0] and "UNATTENDED" in prompts[0]
    assert replies == ["Not created."] and auto.get_store().list_tasks() == []
    verdict[0] = True
    upd, replies = _update("/at 22:00 t_boom", owner)
    await mod.cmd_at(upd, context)
    tasks = auto.get_store().list_tasks()
    assert len(tasks) == 1 and tasks[0].dangerous and replies[0].startswith("✅ #")
    # safe job: no confirmation prompt
    prompts.clear()
    upd, replies = _update("/every 10m t_echo hi", owner)
    await mod.cmd_every(upd, context)
    assert prompts == [] and replies[0].startswith("✅")
    # bad syntax -> usage, nothing created
    upd, replies = _update("/every 10s t_echo hi", owner)
    await mod.cmd_every(upd, context)
    assert "Minimum" in replies[0]
    # cancel
    upd, replies = _update(f"/cancel #{tasks[0].id}", owner)
    await mod.cmd_cancel(upd, context)
    assert "Cancelled" in replies[0]


async def test_module_ignores_strangers(env):
    import bot.modules.automation as mod
    upd, replies = _update("/remind 10m x", chat_id=999)
    await mod.cmd_remind(upd, SimpleNamespace(bot=env.bot, args=[]))
    assert replies == [] and auto.get_store().list_tasks() == []


def test_module_register(env):
    import bot.modules.automation as mod
    store = auto.get_store()
    store.add_task(auto.Plan("every", {"seconds": 600}, [{"action": "t_echo", "args": {"text": "x"}}], "j"), 1)
    app = FakeApp()
    mod.register(app)
    assert len(app.handlers) == 8
    assert app.job_queue.get_jobs_by_name("auto:1") and app.job_queue.get_jobs_by_name(triggers.POLL_JOB)
    assert {c for c, _ in mod.COMMANDS} == {"remind", "every", "at", "jobs", "cancel", "when", "macro"}


@pytest.fixture(scope="module", autouse=True)
def _drop_test_actions():
    yield
    for n in ("t_echo", "t_vol", "t_boom"):
        r.REGISTRY.pop(n, None)
