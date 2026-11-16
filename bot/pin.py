"""Optional PIN for the riskiest actions (protects the PC if someone gets into the owner's Telegram).

Enable by setting EDITH_PIN_HASH (create it with make_pin_hash / the `control pin set` CLI):
    pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>
Actions that need the PIN: policy.ALWAYS_CONFIRM + power_shutdown/restart/logoff + EDITH_PIN_ACTIONS.

Fail closed: if EDITH_PIN_HASH is set but malformed, the PIN stays required and no PIN matches
(the owner fixes it from the PC with `control pin set|off`). Never log or store the PIN itself.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re

log = logging.getLogger("pin")

ALGO = "pbkdf2_sha256"
ITERATIONS = 200_000
MIN_ITERATIONS, MAX_ITERATIONS = 1_000, 5_000_000
PIN_RE = re.compile(r"^[0-9]{4,8}$")  # ASCII only: \d would also accept other scripts' digits
EXTRA_PIN_ACTIONS = frozenset({"power_shutdown", "power_restart", "power_logoff"})


def make_pin_hash(pin: str, *, iterations: int = ITERATIONS, salt: bytes | None = None) -> str:
    """Hash a 4-8 digit PIN with PBKDF2-SHA256 and a random 16-byte salt."""
    if not isinstance(pin, str) or not PIN_RE.match(pin):
        raise ValueError("PIN must be 4-8 digits")
    if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        raise ValueError("bad iteration count")
    salt = salt if salt is not None else os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations)
    return f"{ALGO}${iterations}${salt.hex()}${dk.hex()}"


def _raw() -> str:
    return os.getenv("EDITH_PIN_HASH", "").strip()


def _parse(value: str) -> tuple[int, bytes, bytes] | None:
    try:
        algo, it, salt_hex, hash_hex = value.split("$")
        iterations = int(it)
        salt, digest = bytes.fromhex(salt_hex), bytes.fromhex(hash_hex)
    except ValueError:
        return None
    if algo != ALGO or not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS or not salt or len(digest) != 32:
        return None
    return iterations, salt, digest


def pin_enabled() -> bool:
    """True whenever EDITH_PIN_HASH is set (even if malformed: fail closed)."""
    return bool(_raw())


def pin_status() -> str:
    """One line for doctor/status output."""
    raw = _raw()
    if not raw:
        return "PIN off"
    return "PIN on" if _parse(raw) else "PIN hash INVALID (PIN actions cannot run until it is fixed)"


def check_pin(pin: str) -> bool:
    """Constant-time check of a PIN against EDITH_PIN_HASH. Slow on purpose (PBKDF2): call off the loop."""
    parsed = _parse(_raw())
    if parsed is None:
        if pin_enabled():
            log.error("EDITH_PIN_HASH is malformed - every PIN is rejected")
        return False
    if not isinstance(pin, str) or not PIN_RE.match(pin):
        return False
    iterations, salt, digest = parsed
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations), digest)


def pin_actions() -> frozenset[str]:
    from .actions.policy import ALWAYS_CONFIRM
    extra = {x.strip().lower() for x in os.getenv("EDITH_PIN_ACTIONS", "").split(",") if x.strip()}
    return frozenset(ALWAYS_CONFIRM | EXTRA_PIN_ACTIONS | extra)


def pin_required(action_name: str | None) -> bool:
    """True when a PIN is configured and this action must be unlocked with it."""
    return bool(action_name) and pin_enabled() and action_name in pin_actions()
