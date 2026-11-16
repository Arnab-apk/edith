import json
import threading

import pytest

from bot.actions import notes
from bot.actions import registry as r


@pytest.fixture(autouse=True)
def notes_file(tmp_path, monkeypatch):
    p = tmp_path / "sub" / "notes.json"
    monkeypatch.setenv("EDITH_NOTES_FILE", str(p))
    return p


async def test_remember_recall_forget(notes_file):
    res = await notes.remember("My office wifi is   CorpNet-5G\n(password on the router)")
    assert res.ok and res.data["id"] == 1
    assert json.loads(notes_file.read_text(encoding="utf-8"))["notes"][0]["text"] == \
        "My office wifi is CorpNet-5G (password on the router)"   # whitespace/newlines flattened
    assert (await notes.remember("I like lofi music")).data["id"] == 2
    res = await notes.recall()
    assert res.ok and res.text.startswith("2 note(s)") and res.data["ids"] == [2, 1]
    res = await notes.recall("what's my WIFI?")
    assert res.data["ids"] == [1] and "CorpNet" in res.text
    assert (await notes.recall("pizza")).text.startswith("No notes matching")
    assert (await notes.forget_note(1)).ok
    assert not (await notes.forget_note(1)).ok
    assert (await notes.recall()).data["ids"] == [2]
    assert (await notes.remember("new one")).data["id"] == 3   # ids never reused


async def test_duplicates_and_limits(monkeypatch):
    assert (await notes.remember("I like lofi")).data["id"] == 1
    dup = await notes.remember("  i like LOFI ")
    assert dup.ok and "Already" in dup.text and dup.data["id"] == 1
    assert len(notes.list_notes()) == 1
    assert not (await notes.remember("   ")).ok
    too_long = await notes.remember("x" * (notes.MAX_NOTE_CHARS + 1))
    assert not too_long.ok and "too long" in too_long.text
    assert (await notes.remember("y" * notes.MAX_NOTE_CHARS)).ok
    monkeypatch.setattr(notes, "MAX_NOTES", 3)
    assert (await notes.remember("third")).ok
    full = await notes.remember("fourth")
    assert not full.ok and "full" in full.text


def test_recall_ranking():
    for t in ("office wifi is A", "home wifi is B", "office printer on floor 2"):
        notes.add_note(t)
    ids = [n["id"] for n in notes.search_notes("office wifi")]
    assert ids[0] == 1 and set(ids) == {1, 2, 3}


def test_prompt_block():
    assert notes.prompt_block() == ""
    for i in range(1, 51):
        notes.add_note(f"fact number {i} " + "z" * 40)
    block = notes.prompt_block(max_chars=400)
    assert block.startswith(notes.PROMPT_HEADER + "\n- #50 ")   # newest first
    assert len(block) <= 400
    assert "#1 " not in block


def test_corrupt_file_and_atomic_write(notes_file):
    notes_file.parent.mkdir(parents=True)
    notes_file.write_text("{not json", encoding="utf-8")
    assert notes.list_notes() == []
    assert notes.prompt_block() == ""
    assert notes.add_note("fresh") == ("added", 1)
    names = sorted(p.name for p in notes_file.parent.iterdir())  # no temp files; bad file kept aside
    assert names[0] == "notes.json" and len(names) == 2 and names[1].startswith("notes.json.bad-")
    assert notes_file.with_name(names[1]).read_text(encoding="utf-8") == "{not json"


def test_thread_safety():
    threads = [threading.Thread(target=notes.add_note, args=(f"note {i}",)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = notes.list_notes()
    assert len(got) == 30 and len({n["id"] for n in got}) == 30


async def test_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr(notes, "_save", boom)
    res = await notes.remember("x")
    assert not res.ok and "disk gone" in res.text


def test_registered_schemas():
    for name, req in (("remember", ["note"]), ("recall", []), ("forget_note", ["note_id"])):
        act = r.REGISTRY[name]
        assert act.dangerous is False and act.category == "memory"
        assert act.input_schema["required"] == req
        json.dumps(act.tool_schema())
