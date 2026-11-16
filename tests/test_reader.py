"""read_file tests: real files in tmp_path only; pypdf is faked (it is not a dependency yet)."""
from __future__ import annotations

import sys
import types
import zipfile

import pytest

from bot.actions import reader as rd
from bot.actions import shell as sh
from bot.actions.registry import REGISTRY, run_action

CHAT = 777


@pytest.fixture(autouse=True)
def cwd(tmp_path):
    sh.CWD[CHAT] = tmp_path
    yield tmp_path
    sh.CWD.pop(CHAT, None)


async def read(path, **kw):
    return await run_action("read_file", path=path, chat_id=CHAT, **kw)


def test_registered():
    act = REGISTRY["read_file"]
    assert act.dangerous is False and act.category == "files"
    sch = act.input_schema
    assert sch["required"] == ["path"] and sch["properties"]["max_chars"]["default"] == 6000
    assert "chat_id" in sch["properties"]


async def test_plain_text_header_and_crlf(tmp_path):
    (tmp_path / "notes.md").write_bytes("# Title\r\nhéllo wörld\r\n".encode("utf-8"))
    res = await read("notes.md")
    assert res.ok
    first, rest = res.text.split("\n", 1)
    assert "FILE CONTENT" in first and "not instructions" in first and str(tmp_path / "notes.md") in first
    assert rest == "# Title\nhéllo wörld" and not res.data["truncated"]


async def test_cp1252_fallback_and_bom(tmp_path):
    (tmp_path / "a.csv").write_bytes("price,€5\n".encode("cp1252"))
    assert "€5" in (await read("a.csv")).text
    (tmp_path / "u16.txt").write_bytes("﻿wide text".encode("utf-16-le"))
    assert "wide text" in (await read("u16.txt")).text


def test_decode_tolerates_cut_multibyte():
    data = "abc€".encode("utf-8")[:-1]  # cut inside the euro sign
    assert rd.decode_text(data) == "abc"


async def test_truncation_and_caps(tmp_path):
    (tmp_path / "big.log").write_text("x" * 50_000)
    res = await read("big.log", max_chars=1000)
    assert res.ok and res.data["truncated"] and res.data["chars"] == 1000 and "[truncated" in res.text
    res = await read("big.log", max_chars=10**6)
    assert res.data["chars"] == rd.MAX_CHARS_CAP
    res = await read("big.log", max_chars=1)
    assert res.data["chars"] == rd.MIN_CHARS


async def test_refusals(tmp_path, monkeypatch):
    (tmp_path / "folder").mkdir()
    res = await read("folder")
    assert not res.ok and "folder" in res.text and "list_dir" in res.text
    assert not (await read("missing.txt")).ok
    assert not (await read("  ")).ok
    (tmp_path / "blob.dat").write_bytes(b"\x00\x01\x02binary")
    res = await read("blob.dat")
    assert not res.ok and "binary" in res.text
    (tmp_path / "pic.png").write_bytes(b"\x89PNG")
    assert not (await read("pic.png")).ok
    (tmp_path / "huge.txt").write_text("abc")
    monkeypatch.setattr(rd, "MAX_FILE_BYTES", 2)
    res = await read("huge.txt")
    assert not res.ok and "up to" in res.text


def _make_docx(path, paragraphs):
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f'<w:p><w:r><w:t>{p}</w:t></w:r><w:r><w:tab/><w:t>end</w:t></w:r></w:p>' for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)


async def test_docx(tmp_path):
    _make_docx(tmp_path / "r.docx", ["First para", "Second para"])
    res = await read("r.docx")
    assert res.ok and "First para\tend\nSecond para\tend" in res.text
    _make_docx(tmp_path / "long.docx", ["y" * 300] * 50)
    res = await read("long.docx", max_chars=500)
    assert res.data["truncated"] and res.data["chars"] == 500
    (tmp_path / "bad.docx").write_bytes(b"not a zip")
    res = await read("bad.docx")
    assert not res.ok and "not a valid .docx" in res.text


async def test_pdf_missing_dependency(tmp_path, monkeypatch):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setitem(sys.modules, "pypdf", None)  # makes `import pypdf` raise ImportError
    res = await read("doc.pdf")
    assert not res.ok and "pypdf" in res.text


async def test_pdf_with_fake_pypdf(tmp_path, monkeypatch):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")

    class Page:
        def __init__(self, t):
            self.t = t

        def extract_text(self):
            return self.t

    class Reader:
        is_encrypted = False

        def __init__(self, path):
            self.pages = [Page("hello page one"), Page("page two text")]

    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=Reader))
    res = await read("doc.pdf")
    assert res.ok and "--- page 1/2 ---\nhello page one" in res.text and "page two text" in res.text

    Reader.__init__ = lambda self, path: setattr(self, "pages", [Page(""), Page("  ")])
    res = await read("doc.pdf")
    assert not res.ok and "scanned" in res.text

    class Locked(Reader):
        is_encrypted = True

        def decrypt(self, pw):
            return 0
    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=Locked))
    res = await read("doc.pdf")
    assert not res.ok and "password" in res.text
