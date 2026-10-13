"""Per-site portal passwords live in the macOS Keychain, never in files, logs or chat."""
import re
import secrets
import string

import keyring

SERVICE = "job-agent"
_SYMBOLS = "!@#$%^&*"


def _account(domain: str, email: str) -> str:
    return f"{domain}|{email}"


def get(domain: str, email: str) -> str | None:
    return keyring.get_password(SERVICE, _account(domain, email))


def save(domain: str, email: str, password: str) -> None:
    keyring.set_password(SERVICE, _account(domain, email), password)


def delete(domain: str, email: str) -> None:
    keyring.delete_password(SERVICE, _account(domain, email))


def get_state(domain: str, email: str) -> str | None:
    """'created' once a site accepted the sign-up; only then is signing in worth trying."""
    return keyring.get_password(SERVICE + "-status", _account(domain, email))


def set_state(domain: str, email: str, state: str) -> None:
    keyring.set_password(SERVICE + "-status", _account(domain, email), state)


def rules_from_text(text: str) -> dict:
    """Read stated password rules (length, symbols) from the page text."""
    low = text.lower()
    m = re.search(r"(?:at least|minimum(?: of)?|min\.?)\s*(\d{1,2})\s*characters", low)
    mx = re.search(r"(?:no more than|at most|maximum(?: of)?|max\.?)\s*(\d{1,3})\s*characters", low)
    return {
        "min_len": int(m.group(1)) if m else 8,
        "max_len": int(mx.group(1)) if mx else 64,
        "symbols": not re.search(r"no special|without special|alphanumeric only", low),
    }


def generate(rules: dict | None = None) -> str:
    """Random password with upper, lower, digit and (unless disallowed) a symbol."""
    rules = rules or {}
    length = min(max(16, rules.get("min_len", 8)), rules.get("max_len", 64))
    pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits]
    if rules.get("symbols", True):
        pools.append(_SYMBOLS)
    chars = [secrets.choice(p) for p in pools]
    alphabet = "".join(pools)
    chars += [secrets.choice(alphabet) for _ in range(max(0, length - len(chars)))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)
