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
