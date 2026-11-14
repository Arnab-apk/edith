from bot import cli


def test_env_roundtrip_keeps_comments(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "restrict_to_user", lambda p: None)
    f = tmp_path / ".env"
    f.write_text("# hello\nBOT_TOKEN=old\nOTHER=x\n", encoding="utf-8")
    cli.write_env({"BOT_TOKEN": "new", "LLM_MODEL": "qwen3:8b"}, f)
    text = f.read_text(encoding="utf-8")
    assert text.startswith("# hello\n") and "OTHER=x" in text
    assert cli.read_env(f) == {"BOT_TOKEN": "new", "OTHER": "x", "LLM_MODEL": "qwen3:8b"}


def test_write_env_rejects_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "restrict_to_user", lambda p: None)
    import pytest
    with pytest.raises(ValueError):
        cli.write_env({"BOT_TOKEN": "a\nALLOWED_USER_IDS=999"}, tmp_path / ".env")
    with pytest.raises(ValueError):
        cli.write_env({"bad key": "x"}, tmp_path / ".env")


def test_mask_and_token_format():
    assert cli.mask("BOT_TOKEN", "123456:ABCDEFGHIJKLMNOP") == "1234…NOP"
    assert cli.mask("LLM_MODEL", "qwen3:8b") == "qwen3:8b"
    assert cli.check_token("not-a-token") is None  # rejected locally, no network


def test_parser():
    ns = cli.build_parser().parse_args(["config", "set", "LLM_MODEL", "x"])
    assert (ns.action, ns.key, ns.value) == ("set", "LLM_MODEL", "x")
    assert cli.build_parser().parse_args(["autostart", "status"]).state == "status"


def test_single_instance_lock(tmp_path, monkeypatch):
    lock = tmp_path / "edith.lock"
    assert cli.acquire_single_instance(lock) is True
    first = cli._LOCK_HANDLE
    assert cli.acquire_single_instance(lock) is False      # second bot is refused
    first.close()
    monkeypatch.setattr(cli, "_LOCK_HANDLE", None)
    assert cli.acquire_single_instance(lock) is True       # free again after the first exits
    cli._LOCK_HANDLE.close()


def test_is_local_ollama():
    assert cli.is_local_ollama("http://localhost:11434/v1")
    assert cli.is_local_ollama("http://127.0.0.1:11434")
    assert not cli.is_local_ollama("https://generativelanguage.googleapis.com/v1beta/openai/")
    assert not cli.is_local_ollama("http://localhost:11434.evil.com/v1")


def test_probe_llm_reports_hang_and_never_leaks_key(monkeypatch):
    import httpx

    env = {"LLM_BASE_URL": "http://x/v1", "LLM_MODEL": "m", "LLM_API_KEY": "SECRETKEY"}

    def hang(*a, **k):
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(cli.httpx, "post", hang)
    ok, detail = cli.probe_llm(env, timeout=1)
    assert not ok and "no answer" in detail and "SECRETKEY" not in detail

    class R:
        status_code = 401
        text = "bad key SECRETKEY"
    monkeypatch.setattr(cli.httpx, "post", lambda *a, **k: R())
    ok, detail = cli.probe_llm(env)
    assert not ok and "401" in detail and "SECRETKEY" not in detail
