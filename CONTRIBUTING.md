# Contributing to Edith

Thanks for helping! Edith controls a real PC, so a few rules are non-negotiable.

## Hard rules
- Every Telegram handler wrapped in `bot.security.owner_only` (allowlist in .env; strangers get silence + audit log).
- Destructive actions (shell, delete, kill, power, move, send-keys) need an inline **Confirm** button — also when Edith calls them.
- No stealth, hiding from Task Manager, keylogging, AV evasion, self-spreading. Autostart only via a visible Task Scheduler entry the owner installs.
- Blocking calls go through `bot.utils.run_blocking`. Long text goes through `bot.utils.reply_text`.

## How the code fits together
- `bot/actions/registry.py`: `@action(name, desc, dangerous=, category=, params={arg: desc})` on `async def` with typed args
  (str/int/float/bool/Literal/list[str]/Optional). Returns `ActionResult(text, files=[ActionFile(...)], ok, data)`.
  Actions are pure (no `telegram` import). `run_action(name, **kw)` validates + never raises. `load_all()` imports `ACTION_MODULES`.
- `bot/confirm.py`: `ask_confirm(bot, chat_id, summary) -> bool` (awaits Confirm/Cancel buttons, 120 s timeout = Cancel),
  `confirm_and_run(update, context, name, **kw)` (confirms if dangerous, runs, sends result). Callback prefix `confirm:` is reserved.
- `bot/utils.py`: `send_result(bot, chat_id, ActionResult)` sends text + files (works without an Update).
- `main.py` builds the app with `concurrent_updates(True)`, calls `load_all()`, registers confirm + every module in `MODULE_NAMES`.
- Modules: `COMMANDS` + `register(app)`; every handler `@owner_only`; callback data prefixed `<module>:`.
- Tests: `tests/test_<slice>.py`, run `.venv\Scripts\python -m pytest -q tests/test_<slice>.py`. `tests/conftest.py` fakes BOT_TOKEN.
  Unit tests must not shut down/lock/kill/type anything real: monkeypatch the OS calls.
- Brain is provider-pluggable: `LLM_PROVIDER=anthropic|openai_compat` (+ LLM_BASE_URL/LLM_API_KEY/LLM_MODEL) so Ollama/Gemini/Groq work free.

## Workflow
1. `setup.bat -SkipDeps` (or `uv venv --python 3.12 .venv` + `uv pip sync requirements.txt`).
2. Add or change an action in `bot/actions/`, then expose it in `bot/modules/` if it needs a slash command.
3. Add tests in `tests/` that monkeypatch every OS call, then run `.venv\Scripts\python -m pytest -q`.
4. For anything touching security (auth, confirm, shell, files, the AI tool loop), explain the threat model in the PR.

Report security issues privately via GitHub's "Report a vulnerability" (Security tab) rather than a public issue.
