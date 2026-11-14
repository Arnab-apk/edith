"""Per-chat Edith memory: rolling conversation history + small preferences.

History is provider-neutral plain text ({"role": "user"|"assistant", "content": str}) so it
works for both Anthropic and OpenAI-compatible providers. No thinking blocks or tool calls
are carried across turns, so trimming the oldest turns never invalidates anything.

History lives in RAM only (cleared on restart or /forget). Preferences (voice on/off,
persona) are persisted to a small JSON file so they survive restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ROOT

log = logging.getLogger("jarvis.memory")

MAX_TURNS = int(os.getenv("JARVIS_HISTORY_TURNS", "12"))  # user+assistant pairs kept
MAX_MSG_CHARS = 4000  # cap each stored message so history stays cheap
DEFAULT_STATE_PATH = Path(os.getenv("JARVIS_STATE_FILE", str(ROOT / "data" / "jarvis_state.json")))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


@dataclass
class ChatState:
    history: list[dict[str, str]] = field(default_factory=list)
    voice: bool = True
    persona: str | None = None  # None = default persona
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one Edith turn at a time per chat


class Memory:
    def __init__(self, *, max_turns: int = MAX_TURNS, state_path: Path | None = DEFAULT_STATE_PATH,
                 default_voice: bool | None = None) -> None:
        self.max_turns = max(1, max_turns)
        self.state_path = state_path
        self.default_voice = _env_bool("JARVIS_VOICE_DEFAULT", True) if default_voice is None else default_voice
        self._chats: dict[int, ChatState] = {}
        self._prefs: dict[str, dict] = self._load()

    # ------------------------------------------------------------ persistence (prefs only)
    def _load(self) -> dict[str, dict]:
        if not self.state_path or not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            log.warning("could not read %s - starting with default Edith prefs", self.state_path)
            return {}

    def _save(self) -> None:
        if not self.state_path:
            return
        prefs = {str(cid): {"voice": st.voice, "persona": st.persona} for cid, st in self._chats.items()}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(prefs, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            log.exception("could not save Edith prefs to %s", self.state_path)

    # ------------------------------------------------------------ access
    def get(self, chat_id: int) -> ChatState:
        st = self._chats.get(chat_id)
        if st is None:
            p = self._prefs.get(str(chat_id), {})
            st = ChatState(voice=bool(p.get("voice", self.default_voice)), persona=p.get("persona") or None)
            self._chats[chat_id] = st
        return st

    def history(self, chat_id: int) -> list[dict[str, str]]:
        return [dict(m) for m in self.get(chat_id).history]

    def add_turn(self, chat_id: int, user_text: str, assistant_text: str) -> None:
        st = self.get(chat_id)
        st.history.append({"role": "user", "content": _clip(user_text)})
        st.history.append({"role": "assistant", "content": _clip(assistant_text) or "(no reply)"})
        excess = len(st.history) - 2 * self.max_turns
        if excess > 0:
            del st.history[:excess]
        while st.history and st.history[0]["role"] != "user":  # always start with a user turn
            st.history.pop(0)

    def forget(self, chat_id: int) -> int:
        """Clear the chat's history. Returns how many messages were dropped."""
        st = self.get(chat_id)
        n = len(st.history)
        st.history.clear()
        return n

    def set_voice(self, chat_id: int, on: bool) -> None:
        self.get(chat_id).voice = on
        self._save()

    def set_persona(self, chat_id: int, persona: str | None) -> None:
        self.get(chat_id).persona = (persona or "").strip() or None
        self._save()


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= MAX_MSG_CHARS else text[:MAX_MSG_CHARS] + " …(truncated)"


memory = Memory()
