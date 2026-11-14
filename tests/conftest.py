"""Test env: fake credentials; the real .env is never read (EDITH_SKIP_DOTENV)."""
import os
import sys
from pathlib import Path

os.environ["EDITH_SKIP_DOTENV"] = "1"
os.environ["BOT_TOKEN"] = "123456:TEST-token"
os.environ["ALLOWED_USER_IDS"] = "111111111"
for _k in [k for k in os.environ if k.startswith(("LLM_", "CLAUDE_", "ANTHROPIC_", "WHISPER_", "TTS_", "EDITH_", "AUTOMATION_")) and k != "EDITH_SKIP_DOTENV"]:
    del os.environ[_k]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
