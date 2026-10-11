"""Passwords (argon2) and TOTP for the admin fallback login, server-side sessions, CSRF tokens,
lockout, and the hashing of every bearer secret (session, invite, runner, pairing tokens).

Users sign in with Google (google.py). Only admins may also have a password + TOTP, for the
case when Google sign-in is unavailable. Failed passwords and codes count together; from the
5th failure the account is locked for a doubling time (30 s ... at most 15 min).
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DbSession

from .db import utcnow
from .models import Session, User

COOKIE = "dwyt_session"
PENDING_MINUTES = 5
FREE_FAILURES = 4
MAX_LOCK_S = 15 * 60
TOTP_ISSUER = "dwyt"

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("dummy password for timing")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def check_password(user: User | None, password: str) -> bool:
    """Constant work whether or not the user (or a password) exists."""
    stored = user.password_hash if user is not None and user.password_hash else _DUMMY_HASH
    try:
        return _hasher.verify(stored, password) and user is not None and bool(user.password_hash)
    except (VerificationError, InvalidHashError):
        return False


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(user: User, secret: str | None = None) -> str:
    return pyotp.TOTP(secret or user.totp_secret).provisioning_uri(name=user.email, issuer_name=TOTP_ISSUER)


def totp_qr_svg(uri: str) -> str:
    """The provisioning URI as an inline SVG QR code (no data: URLs under the page's CSP)."""
    import segno
    return segno.make(uri, error="m").svg_inline(scale=4, dark="#111", light="#fff")


def check_totp(user: User, code: str, now: float | None = None, secret: str | None = None) -> bool:
    """Accept the current code or the one before/after it (clock drift), each only once.
    secret: a secret being set up (not yet the user's)."""
    code = "".join(ch for ch in code if ch.isdigit())
    secret = secret or user.totp_secret
    if len(code) != 6 or not secret:
        return False
    totp = pyotp.TOTP(secret)
    now = time.time() if now is None else now
    step_now = int(now // totp.interval)
    for step in (step_now - 1, step_now, step_now + 1):
        if step > user.totp_last_step and secrets.compare_digest(totp.at(step * totp.interval), code):
            user.totp_last_step = step  # a code cannot be replayed
            return True
    return False


def lock_seconds_left(user: User) -> int:
    if user.locked_until is None:
        return 0
    return max(0, int((user.locked_until - utcnow()).total_seconds() + 0.999))


def record_failure(user: User) -> None:
    user.failed_logins += 1
    over = user.failed_logins - FREE_FAILURES
    if over > 0:
        user.locked_until = utcnow() + timedelta(seconds=min(MAX_LOCK_S, 30 * 2 ** (over - 1)))


def record_success(user: User) -> None:
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = utcnow()


def start_session(db: DbSession, user: User, state: str, days: float, ip: str | None,
                  user_agent: str | None) -> str:
    """Create a session and return the cookie value (the token itself is never stored)."""
    token = new_token()
    lifetime = timedelta(minutes=PENDING_MINUTES) if state == "pending" else timedelta(days=days)
    db.add(Session(token_hash=token_hash(token), user_id=user.id, state=state,
                   csrf_token=new_token(24), expires_at=utcnow() + lifetime,
                   ip=ip, user_agent=(user_agent or "")[:300]))
    return token


def find_session(db: DbSession, token: str | None) -> Session | None:
    if not token or len(token) > 100:
        return None
    sess = db.get(Session, token_hash(token))
    if sess is None or sess.expires_at <= utcnow():
        return None
    return sess


def end_session(db: DbSession, token: str | None) -> None:
    if token:
        db.execute(delete(Session).where(Session.token_hash == token_hash(token)))


def end_all_sessions(db: DbSession, user_id: int) -> None:
    db.execute(delete(Session).where(Session.user_id == user_id))


def purge_expired(db: DbSession) -> None:
    db.execute(delete(Session).where(Session.expires_at <= utcnow()))


def user_by_email(db: DbSession, email: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
