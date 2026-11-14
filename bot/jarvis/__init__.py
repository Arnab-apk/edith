"""Edith: voice (STT/TTS), brain (LLM tool loop over the action registry), memory.

Nothing heavy is imported here: faster-whisper / LLM clients load lazily on first use.
"""
