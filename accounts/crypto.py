"""Encryption of API keys at rest.

Keys are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using FIELD_ENCRYPTION_KEY from the server
environment. The database alone is useless without that key, and the key is never stored in the database.
Generate one with:  python manage.py make_encryption_key   (then put it in .env and keep a backup:
without it, saved API keys can't be read and have to be entered again).
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class KeyStoreError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = getattr(settings, "FIELD_ENCRYPTION_KEY", "")
    if not key:
        raise KeyStoreError("FIELD_ENCRYPTION_KEY is not set on the server, so API keys can't be stored safely. "
                            "Run `python manage.py make_encryption_key`, put the result in .env and restart.")
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise KeyStoreError("FIELD_ENCRYPTION_KEY is not a valid key (use make_encryption_key).") from exc


def ready() -> bool:
    try:
        _fernet()
        return True
    except KeyStoreError:
        return False


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise KeyStoreError("A saved API key can't be decrypted: FIELD_ENCRYPTION_KEY changed. "
                            "Enter the key again on the API keys page.") from exc
