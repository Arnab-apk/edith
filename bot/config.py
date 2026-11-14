"""Loads settings from .env in the project root."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if not os.getenv("EDITH_SKIP_DOTENV"):  # the test suite must never read the real .env
    load_dotenv(ROOT / ".env", override=True)  # .env wins over stray system variables


@dataclass(frozen=True)
class Settings:
    bot_token: str
    allowed_user_ids: frozenset[int]
    shell_timeout: int
    upload_dir: Path
    log_dir: Path


def _load() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    raw_ids = os.getenv("ALLOWED_USER_IDS", "")
    ids = frozenset(int(x) for x in raw_ids.replace(" ", "").split(",") if x.isdigit())
    if not token or ":" not in token:
        sys.exit("BOT_TOKEN missing/invalid in .env (copy .env.example to .env)")
    if not ids:
        sys.exit("ALLOWED_USER_IDS is empty in .env - refusing to start an open bot")
    upload = Path(os.getenv("UPLOAD_DIR", str(ROOT / "uploads"))).expanduser()
    upload.mkdir(parents=True, exist_ok=True)
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    return Settings(
        bot_token=token,
        allowed_user_ids=ids,
        shell_timeout=int(os.getenv("SHELL_TIMEOUT", "60")),
        upload_dir=upload,
        log_dir=log_dir,
    )


settings = _load()
