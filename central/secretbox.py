"""Encryption of the few secrets the central service must keep (Google refresh tokens for the
YouTube subscription list), with TOKEN_KEY (a Fernet key from the env file, never in the
database): a copy of the database alone does not give access to anyone's Google account."""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


class NoKey(Exception):
    pass


def _fernet() -> Fernet:
    key = get_settings().token_key
    if not key:
        raise NoKey("A TOKEN_KEY nincs beállítva (docs/telepites.md).")
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise NoKey("A TOKEN_KEY nem érvényes Fernet-kulcs.") from exc


def available() -> bool:
    try:
        _fernet()
        return True
    except NoKey:
        return False


def seal(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def unseal(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise NoKey("A tárolt token nem fejthető vissza (megváltozott a TOKEN_KEY?); kapcsold össze újra.") from exc
