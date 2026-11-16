"""Round 2 automation: weekly schedules + file/network/usb/lock triggers. Fully faked: temp DB, fake
JobQueue/Bot, monkeypatched sensors. Nothing real is scheduled, probed or run."""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

import bot.actions.automation  # noqa: F401  (registers actions)
from bot import automation as auto
from bot.actions import registry as r
from bot.automation import scheduler, triggers

CALLS: list[tuple[str, dict]] = []


@r.action("t2_echo", "Echo text (test).", category="test")
async def t2_echo(text: str) -> r.ActionResult:
    CALLS.append(("t2_echo", {"text": text}))
    return r.ActionResult(text=f"echo {text}")


@r.action("t2_boom", "Dangerous thing (test).", dangerous=True, category="test")
async def t2_boom(target: str = "pc") -> r.ActionResult:
    CALLS.append(("t2_boom", {"target": target}))
    return r.ActionResult(text="boom")


class FakeJob:
    def __init__(self, callback, name, data, **info):
        self.callback, self.name, self.data, self.info, self.removed, self.next_t = callback, name, data, info, False, None

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    def __init__(self):
        self.jobs: list[FakeJob] = []

    def _add(self, cb, name, data, **info):
        self.jobs.append(FakeJob(cb, name, data, **info))
        return self.jobs[-1]

    def run_once(self, callback, when, data=None, name=None, **kw):
        return self._add(callback, name, data, kind="once", when=when)

    def run_repeating(self, callback, interval, first=None, last=None, data=None, name=None, **kw):
        return self._add(callback, name, data, kind="repeating", interval=interval)

    def run_daily(self, callback, time, days=(0, 1, 2, 3, 4, 5, 6), data=None, name=None, **kw):
        return self._add(callback, name, data, kind="daily", time=time, days=days)

    def get_jobs_by_name(self, name):
        return tuple(j for j in self.jobs if j.name == name and not j.removed)


class FakeBot:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)

    async def send_document(self, chat_id, doc, **kw):
        self.sent.append("<doc>")


class FakeApp:
    def __init__(self):
        self.job_queue, self.bot = FakeJobQueue(), FakeBot()


@pytest.fixture(autouse=True)
def env(tmp_path):
    auto.set_store(auto.Store(tmp_path / "db" / "automation.db"))
    app = FakeApp()
    auto.set_app(app)
    triggers._STATE.clear()
    CALLS.clear()
    yield app
    auto.set_store(None)
    auto.set_app(None)


@pytest.fixture(scope="module", autouse=True)
def _drop_test_actions():
    yield
    for n in ("t2_echo", "t2_boom"):
        r.REGISTRY.pop(n, None)


# ================================================================ weekly schedules
@pytest.mark.parametrize("text,days,hm,rest", [
    ("every weekday at 9:00 t2_echo hi", [0, 1, 2, 3, 4], (9, 0), "t2_echo hi"),
    ("weekdays 9:00 x", [0, 1, 2, 3, 4], (9, 0), "x"),
    ("weekends at 10:00 x", [5, 6], (10, 0), "x"),
    ("every monday at 8:00 x", [0], (8, 0), "x"),
    ("every mon,wed,fri at 18:30 x", [0, 2, 4], (18, 30), "x"),
    ("on Mondays and Fridays 7pm x", [0, 4], (19, 0), "x"),
    ("tue, thu 6:15am x", [1, 3], (6, 15), "x"),
    ("every day at 07:00 x", list(range(7)), (7, 0), "x"),
])
def test_parse_weekly(text, days, hm, rest):
    assert scheduler.parse_weekly(text) == (days, hm, rest)


def test_parse_weekly_non_days_and_errors():
    assert scheduler.parse_weekly("30m screenshot") is None
    assert scheduler.parse_weekly("22:00 shutdown") is None
    assert scheduler.parse_weekly("every 2h x") is None
    with pytest.raises(auto.AutomationError, match="time of day"):
        scheduler.parse_weekly("every monday t2_echo hi")


def test_ptb_day_numbering():
    # PTB 20+: 0 = Sunday ... 6 = Saturday; our spec: 0 = Monday ... 6 = Sunday
    assert scheduler.ptb_days([0, 1, 2, 3, 4]) == (1, 2, 3, 4, 5)
    assert scheduler.ptb_days([5, 6]) == (0, 6)
    assert scheduler.ptb_days([6]) == (0,)


async def test_weekly_commit_schedule_restore_and_list(env):
    plan = scheduler.plan_every("every mon,wed,fri at 18:30 t2_echo gym")   # /every path
    assert plan.kind == "weekly" and plan.spec == {"time": "18:30", "days": [0, 2, 4]}
    assert plan.label == "job Mon, Wed, Fri at 18:30: t2_echo(text='gym')"
    task = await scheduler.commit(plan, 5)
    job = env.job_queue.get_jobs_by_name(f"auto:{task.id}")[0]
    assert job.info["kind"] == "daily" and job.info["days"] == (1, 3, 5)
    assert (job.info["time"].hour, job.info["time"].minute) == (18, 30) and job.info["time"].tzinfo
    await job.callback(SimpleNamespace(bot=env.bot, job=job))
    assert CALLS == [("t2_echo", {"text": "gym"})]
    assert env.bot.sent[0].startswith("📅 Automation #")
    assert auto.get_store().get_task(task.id) is not None          # repeating: kept
    fresh = FakeApp()
    auto.set_app(fresh)
    assert scheduler.restore(fresh) == 1
    assert fresh.job_queue.get_jobs_by_name(f"auto:{task.id}")[0].info["days"] == (1, 3, 5)
    assert "📅 job Mon, Wed, Fri at 18:30" in await scheduler.list_text()


def test_weekly_via_at_and_daily_collapse():
    assert scheduler.plan_at("weekends 10:00 t2_echo brunch").label.startswith("job weekends at 10:00")
    assert scheduler.plan_at("mon 8:00 t2_echo standup").spec == {"time": "08:00", "days": [0]}
    every_day = scheduler.plan_weekly("every day at 7:00 t2_echo up")
    assert every_day.kind == "daily" and every_day.spec == {"time": "07:00"}
    assert scheduler.plan_at("22:00 daily t2_echo x").kind == "daily"            # old form still works
    assert scheduler.plan_every("30m t2_echo x").kind == "every"


async def test_schedule_action_weekly(env):
    sch = r.REGISTRY["schedule_action"].input_schema["properties"]["kind"]
    assert "weekly" in sch["enum"]
    res = await r.run_action("schedule_action", kind="weekly", when="weekdays 9:00", steps="t2_echo hi")
    assert res.ok, res.text
    assert auto.get_store().get_task(res.data["id"]).spec["days"] == [0, 1, 2, 3, 4]
    # a small model may pick another kind for a weekday phrase - still understood
    res = await r.run_action("schedule_action", kind="every", when="every monday at 8:00", steps="t2_echo x")
    assert res.ok and auto.get_store().get_task(res.data["id"]).kind == "weekly"
    res = await r.run_action("schedule_action", kind="weekly", when="9:00", steps="t2_echo x")
    assert not res.ok and "days + time" in res.text
    res = await r.run_action("schedule_action", kind="weekly", when="mon 8:00 t2_boom", steps="t2_echo x")
    assert not res.ok                                             # no steps smuggled into `when`
    res = await r.run_action("schedule_action", kind="weekly", when="sat 10:00", steps="t2_boom")
    assert res.ok and auto.get_store().get_task(res.data["id"]).dangerous


# ================================================================ condition parsing
def test_parse_new_conditions(tmp_path, monkeypatch):
    (tmp_path / "Downloads").mkdir()
    monkeypatch.setattr(triggers, "_home", lambda: tmp_path)
    dl = str((tmp_path / "Downloads").resolve())
    spec, rest = triggers.parse_condition("file in Downloads")
    assert spec == {"metric": "file", "folder": dl, "pattern": "*", "for": 0, "cooldown": 0} and rest == ""
    spec, rest = triggers.parse_condition("file *.pdf in downloads notify got {file}")
    assert spec["pattern"] == "*.pdf" and rest == "notify got {file}"
    assert triggers.parse_condition("new files pdf in Downloads")[0]["pattern"] == "*.pdf"
    assert triggers.parse_condition("file .zip in Downloads")[0]["pattern"] == "*.zip"
    spaced = tmp_path / "My Stuff"
    spaced.mkdir()
    spec, rest = triggers.parse_condition(f'file report.docx in "{spaced}" then t2_echo hi')
    assert spec["folder"] == str(spaced.resolve()) and spec["pattern"] == "report.docx" and rest == "t2_echo hi"
    assert triggers.parse_condition("network lost")[0] == {"metric": "net", "event": "lost", "for": 0, "cooldown": 0}
    assert triggers.parse_condition("internet back cooldown 5m")[0]["cooldown"] == 300
    assert triggers.parse_condition("network down")[0]["event"] == "lost"
    assert triggers.parse_condition("usb plugged")[0] == {"metric": "usb", "event": "plugged", "for": 0, "cooldown": 0}
    assert triggers.parse_condition("usb drive removed notify")[0]["event"] == "removed"
    assert triggers.parse_condition("pc unlocked")[0] == {"metric": "lock", "event": "unlocked", "for": 0,
                                                          "cooldown": 0}
    assert triggers.parse_condition("screen locks")[0]["event"] == "locked"
    assert triggers.parse_condition("ram>90")[0]["metric"] == "ram"            # old forms unaffected


def test_parse_new_condition_errors(tmp_path):
    with pytest.raises(auto.AutomationError, match="Folder not found"):
        triggers.parse_condition(f"file in {tmp_path / 'nope'}")
    with pytest.raises(auto.AutomationError, match="full folder path"):
        triggers.parse_condition("file in relative_dir")
    with pytest.raises(auto.AutomationError, match="not supported"):
        triggers.parse_condition("usb plugged for 2m")
    with pytest.raises(auto.AutomationError) as e:
        triggers.parse_condition("bluetooth on")
    assert "usb plugged" in str(e.value) and "file *.pdf in Downloads" in str(e.value)


def test_describe_new_conditions(tmp_path):
    spec, _ = triggers.parse_condition(f"file *.pdf in {tmp_path}")
    assert triggers.describe_condition(spec) == f"new file *.pdf in {tmp_path.resolve()}"
    assert triggers.describe_condition(triggers.parse_condition("network lost")[0]) == "network lost"
    assert triggers.describe_condition(triggers.parse_condition("usb removed")[0]) == "usb removed"
    assert triggers.describe_condition(triggers.parse_condition("pc locked cooldown 2m")[0]) == \
        "pc locked (cooldown 2m)"


def test_placeholder_rules(tmp_path):
    with pytest.raises(auto.AutomationError, match="only works in file"):
        triggers.plan_trigger("usb plugged notify {file}")
    with pytest.raises(auto.AutomationError, match="dangerous step"):
        triggers.plan_trigger(f"file in {tmp_path} t2_boom {{file}}")
    plan = triggers.plan_trigger("usb plugged notify text=Drive {drive} is in")
    assert plan.steps == [{"action": "notify", "args": {"text": "Drive {drive} is in"}}]
    steps = [{"action": "notify", "args": {"text": "got {file}"}}, {"action": "t2_boom", "args": {"target": "{file}"}}]
    out = triggers.substitute(steps, {"file": "C:\\x.pdf"})
    assert out[0]["args"]["text"] == "got C:\\x.pdf"
    assert out[1]["args"]["target"] == "{file}"                   # dangerous steps never get filled


# ================================================================ samplers (OS calls faked)
def test_list_folder_skips_partials(tmp_path):
    for n in ("a.pdf", "b.crdownload", "c.part", "desktop.ini", "~$doc.docx", "d.TMP"):
        (tmp_path / n).write_text("x")
    (tmp_path / "sub").mkdir()
    assert set(triggers._list_folder(str(tmp_path))) == {"a.pdf"}
    assert triggers._list_folder(str(tmp_path / "gone")) is None


def test_internet_up(monkeypatch):
    tried = []

    class Conn:
        def close(self):
            pass

    def fake_connect(addr, timeout):
        tried.append((addr, timeout))
        if addr[0] == "1.1.1.1":
            raise OSError("blocked")
        return Conn()

    monkeypatch.setattr(triggers.socket, "create_connection", fake_connect)
    assert triggers._internet_up() is True and tried == [(("1.1.1.1", 443), 2), (("8.8.8.8", 53), 2)]
    monkeypatch.setattr(triggers.socket, "create_connection",
                        lambda addr, timeout: (_ for _ in ()).throw(OSError("offline")))
    assert triggers._internet_up() is False


def test_removable_drives(monkeypatch):
    import psutil
    parts = [SimpleNamespace(mountpoint="C:\\", opts="rw,fixed"), SimpleNamespace(mountpoint="E:\\", opts="rw,removable"),
             SimpleNamespace(mountpoint="F:\\", opts="rw")]
    monkeypatch.setattr(psutil, "disk_partitions", lambda all=False: parts)
    monkeypatch.setattr(triggers, "_drive_type", lambda root: 2 if root == "F:\\" else 3)
    assert triggers._removable_drives() == {"E:\\", "F:\\"}


def test_sample_dispatches_new_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(triggers, "_internet_up", lambda: True)
    monkeypatch.setattr(triggers, "_removable_drives", lambda: {"E:\\"})
    monkeypatch.setattr(triggers, "_is_locked", lambda: False)
    (tmp_path / "x.txt").write_text("1")
    snap = triggers.sample({"net", "usb", "lock", f"file:{tmp_path}"})
    assert snap["net"] is True and snap["usb"] == {"E:\\"} and snap["lock"] is False
    assert set(snap[f"file:{tmp_path}"]) == {"x.txt"}


# ================================================================ evaluation
def test_eval_file_baseline_new_partial_and_cap(tmp_path):
    spec = {"metric": "file", "folder": str(tmp_path), "pattern": "*.pdf", "for": 0, "cooldown": 0}
    key = triggers.sample_key(spec)
    st = triggers.TriggerState()
    assert triggers.evaluate(spec, st, {key: {"old.pdf": 1.0}}, 0) is None          # baseline
    assert triggers.evaluate(spec, st, {key: {"old.pdf": 1.0, "n.txt": 2.0}}, 10) is None  # pattern miss
    reading = triggers.evaluate(spec, st, {key: {"old.pdf": 1.0, "n.txt": 2.0, "new.pdf": 3.0}}, 20)
    assert reading == "new file new.pdf"
    assert st.extra["vars"] == [{"file": os.path.join(str(tmp_path), "new.pdf")}]
    many = {f"f{i}.pdf": 5.0 for i in range(5)}
    reading = triggers.evaluate(spec, st, {key: many}, 30)
    assert reading.startswith("5 new files: f0.pdf, f1.pdf, f2.pdf") and "(+2 more)" in reading
    assert len(st.extra["vars"]) == triggers.MAX_EVENTS_PER_POLL
    assert triggers.evaluate(spec, st, {key: many}, 40) is None                      # already known
    assert triggers.evaluate(spec, st, {key: None}, 50) is None                      # folder gone


def test_eval_file_after_restart_uses_reference_time(tmp_path):
    spec = {"metric": "file", "folder": str(tmp_path), "pattern": "*", "for": 0, "cooldown": 0}
    st = triggers.TriggerState(extra={"ref": 100.0})
    reading = triggers.evaluate(spec, st, {triggers.sample_key(spec): {"before.txt": 50.0, "during.txt": 150.0}}, 200)
    assert reading == "new file during.txt"


def test_eval_network_lost_and_back():
    lost = {"metric": "net", "event": "lost", "for": 0, "cooldown": 0}
    back = dict(lost, event="back")
    st_l, st_b = triggers.TriggerState(), triggers.TriggerState()
    # a single failed probe is a blip, not an outage
    for st, spec in ((st_l, lost), (st_b, back)):
        assert triggers.evaluate(spec, st, {"net": True}, 0) is None
        assert triggers.evaluate(spec, st, {"net": False}, 30) is None
        assert triggers.evaluate(spec, st, {"net": True}, 60) is None
    # real outage: two failed polls, then the connection returns
    for t, up in ((100, False), (130, False), (160, False)):
        assert triggers.evaluate(lost, st_l, {"net": up}, t) is None              # can't notify while offline
        assert triggers.evaluate(back, st_b, {"net": up}, t) is None
    r_lost = triggers.evaluate(lost, st_l, {"net": True}, 400)
    r_back = triggers.evaluate(back, st_b, {"net": True}, 400)
    assert r_lost.startswith("internet was lost at ") and "5m" in r_lost and "back now" in r_lost
    assert r_back == "internet is back after about 5m offline"
    assert triggers.evaluate(lost, st_l, {"net": True}, 430) is None
    assert triggers.evaluate(lost, st_l, {"net": None}, 460) is None


def test_eval_usb():
    plug = {"metric": "usb", "event": "plugged", "for": 0, "cooldown": 0}
    rem = dict(plug, event="removed")
    sp, sr = triggers.TriggerState(), triggers.TriggerState()
    for spec, st in ((plug, sp), (rem, sr)):
        assert triggers.evaluate(spec, st, {"usb": {"E:\\"}}, 0) is None           # baseline: E: already in
    assert triggers.evaluate(plug, sp, {"usb": {"E:\\", "F:\\"}}, 10) == "USB drive F:\\ plugged in"
    assert sp.extra["vars"] == [{"drive": "F:\\"}]
    assert triggers.evaluate(rem, sr, {"usb": {"E:\\", "F:\\"}}, 10) is None
    assert triggers.evaluate(rem, sr, {"usb": {"F:\\"}}, 20) == "USB drive E:\\ removed"
    assert triggers.evaluate(plug, sp, {"usb": {"F:\\"}}, 20) is None


def test_eval_lock_debounced():
    spec = {"metric": "lock", "event": "locked", "for": 0, "cooldown": 0}
    st = triggers.TriggerState()
    assert triggers.evaluate(spec, st, {"lock": False}, 0) is None                  # baseline
    assert triggers.evaluate(spec, st, {"lock": True}, 30) is None                  # seen once (UAC blip?)
    assert triggers.evaluate(spec, st, {"lock": False}, 60) is None                 # it was a blip
    assert triggers.evaluate(spec, st, {"lock": True}, 90) is None
    assert triggers.evaluate(spec, st, {"lock": True}, 120) == "PC was locked"
    assert triggers.evaluate(spec, st, {"lock": True}, 150) is None
    unlock = dict(spec, event="unlocked")
    su = triggers.TriggerState(prev_running=True)
    assert triggers.evaluate(unlock, su, {"lock": False}, 0) is None
    assert triggers.evaluate(unlock, su, {"lock": False}, 30) == "PC was unlocked"
    assert triggers.evaluate(unlock, su, {"lock": None}, 60) is None


# ================================================================ end-to-end through poll_once
async def test_file_trigger_end_to_end(env, tmp_path, monkeypatch):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    (folder / "existing.pdf").write_text("x")
    plan = triggers.plan_trigger(f"file *.pdf in {folder} notify New PDF: {{file}}")
    task = await scheduler.commit(plan, 7)                     # baselines right away (on_created)
    assert "existing.pdf" in triggers._STATE[task.id].extra["known"]
    (folder / "report.pdf.crdownload").write_text("partial")
    assert await triggers.poll_once(env.bot) == 0              # still downloading
    os.replace(folder / "report.pdf.crdownload", folder / "report.pdf")
    assert await triggers.poll_once(env.bot) == 1
    path = str((folder / "report.pdf").resolve())
    assert env.bot.sent == [f"New PDF: {path}"]
    assert await triggers.poll_once(env.bot) == 0
    assert auto.get_store().get_task(task.id).last_fired


async def test_usb_trigger_runs_steps_per_drive(env, monkeypatch):
    drives = {"value": set()}
    monkeypatch.setattr(triggers, "_removable_drives", lambda: set(drives["value"]))
    task = await scheduler.commit(triggers.plan_trigger("usb plugged t2_echo drive {drive}"), 7)
    drives["value"] = {"E:\\", "F:\\"}
    assert await triggers.poll_once(env.bot) == 1
    assert CALLS == [("t2_echo", {"text": "drive E:\\"}), ("t2_echo", {"text": "drive F:\\"})]
    assert any(s.startswith(f"🔔 Trigger #{task.id}: usb plugged - USB drive E:\\, F:\\ plugged in")
               for s in env.bot.sent)


async def test_network_lost_notice_is_queued(env, monkeypatch):
    up = {"v": True}
    monkeypatch.setattr(triggers, "_internet_up", lambda: up["v"])
    task = await scheduler.commit(triggers.plan_trigger("network lost"), 7)
    t0 = time.time()
    assert await triggers.poll_once(env.bot, now=t0) == 0
    up["v"] = False
    assert await triggers.poll_once(env.bot, now=t0 + 30) == 0
    assert await triggers.poll_once(env.bot, now=t0 + 60) == 0
    assert env.bot.sent == []                                  # nothing sent while offline
    up["v"] = True
    assert await triggers.poll_once(env.bot, now=t0 + 120) == 1
    assert env.bot.sent[0].startswith(f"🔔 Trigger #{task.id}: network lost - internet was lost at")


async def test_create_trigger_action_new_kinds(env, monkeypatch, tmp_path):
    monkeypatch.setattr(triggers, "_is_locked", lambda: None)
    res = await r.run_action("create_trigger", condition="pc unlocked", steps="notify text=Welcome back")
    assert res.ok and "not readable" in res.text                # honest when lock state can't be read
    res = await r.run_action("create_trigger", condition=f"file in {tmp_path}", steps="notify {file}")
    assert res.ok, res.text
    res = await r.run_action("create_trigger", condition="usb plugged", steps="t2_boom {drive}")
    assert not res.ok and "dangerous step" in res.text
    listing = (await r.run_action("list_jobs")).text
    assert "when pc unlocked" in listing and "when new file * in" in listing
