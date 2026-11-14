"""Test env: fake credentials so bot.config loads without a real .env (real env vars win)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123456:TEST-token")
os.environ.setdefault("ALLOWED_USER_IDS", "111111111")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
