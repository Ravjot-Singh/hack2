"""Password hashing (stdlib scrypt) and encryption of secrets businesses give us (Fernet)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(base64.b64decode(digest)))
        return hmac.compare_digest(actual, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def _fernet() -> Fernet:
    # Any string works as the configured key: it is stretched to a valid 32-byte Fernet key.
    raw = hashlib.sha256(get_settings().token_encryption_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None  # key changed: treat as not configured


def _link_sig(tenant_id: int, customer_id: int) -> str:
    key = hashlib.sha256(("telegram-link:" + get_settings().session_secret).encode()).digest()
    mac = hmac.new(key, f"{tenant_id}:{customer_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode()[:22]


def link_token(tenant_id: int, customer_id: int) -> str:
    """Telegram /start parameter that links a chat to one customer (fits Telegram's 64-char [A-Za-z0-9_-] limit)."""
    return f"c{customer_id}_{_link_sig(tenant_id, customer_id)}"


def read_link_token(tenant_id: int, token: str) -> int | None:
    """The customer id a link token was made for, or None if it is forged or for another business."""
    head, _, sig = (token or "").partition("_")
    if not head.startswith("c") or not head[1:].isdigit() or len(head) > 12:
        return None
    customer_id = int(head[1:])
    return customer_id if hmac.compare_digest(sig, _link_sig(tenant_id, customer_id)) else None


def insecure_defaults() -> list[str]:
    """Settings still at their placeholder values (fatal on a public server)."""
    st = get_settings()
    return [name for name in ("session_secret", "token_encryption_key") if getattr(st, name) == "change-me"]
