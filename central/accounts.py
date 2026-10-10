"""Google sign-in (OpenID Connect, authorization code flow with PKCE) and invitations.

The ID token comes straight from Google's token endpoint over TLS in exchange for the code,
so its signature check may be replaced by TLS server validation (OpenID Connect Core 1.0,
3.1.3.7 item 6). Issuer, audience, expiry and nonce are still checked, and only a verified
e-mail address is accepted. The account is bound to Google's stable `sub`, not the address.

Who may sign in:
  - an existing, not disabled user with this `sub`;
  - a user created beforehand (an admin from the CLI) whose e-mail matches and has no `sub` yet;
  - with a valid invitation whose e-mail matches (REGISTRATION=invite, the default);
  - anyone with a verified address when REGISTRATION=open.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from .config import Settings
from .db import utcnow
from .models import Invite, LoginState, User
from .security import new_token, token_hash, user_by_email

ISSUERS = ("https://accounts.google.com", "accounts.google.com")
STATE_MINUTES = 10


class SignInError(Exception):
    """A user-facing reason why the sign-in did not succeed (Hungarian)."""


@dataclass
class GoogleIdentity:
    sub: str
    email: str
    name: str | None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def start(db: DbSession, settings: Settings, invite_token: str | None) -> tuple[str, str]:
    """Remember state, nonce and PKCE verifier; return Google's authorization URL and the state
    (the caller also puts the state in a cookie, binding the sign-in to this browser)."""
    if not settings.google_client_id:
        raise SignInError("A Google-belépés nincs beállítva (GOOGLE_CLIENT_ID).")
    state, nonce, verifier = new_token(), new_token(), new_token(48)
    db.execute(delete(LoginState).where(LoginState.created_at < utcnow() - timedelta(minutes=STATE_MINUTES)))
    db.add(LoginState(state_hash=token_hash(state), nonce=nonce, verifier=verifier,
                      invite_token_hash=token_hash(invite_token) if invite_token else None))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    params = {"client_id": settings.google_client_id, "redirect_uri": settings.redirect_uri,
              "response_type": "code", "scope": "openid email profile", "state": state, "nonce": nonce,
              "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"}
    return f"{settings.google_auth_url}?{urlencode(params)}", state


def _claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:
        raise SignInError("A Google válasza nem értelmezhető.") from exc


def consume_state(db: DbSession, state: str) -> LoginState:
    """Take the sign-in state out of the database (one use only, also when the sign-in fails)."""
    login = db.get(LoginState, token_hash(state or "")) if state and len(state) < 200 else None
    if login is None:
        raise SignInError("A belépés lejárt vagy érvénytelen. Kezdd újra.")
    copy = LoginState(state_hash=login.state_hash, nonce=login.nonce, verifier=login.verifier,
                      invite_token_hash=login.invite_token_hash, created_at=login.created_at)
    db.delete(login)
    db.flush()
    return copy


def finish(login: LoginState, settings: Settings, code: str) -> tuple[GoogleIdentity, str | None]:
    """Exchange the code, verify the ID token. Returns the identity and the hash of the
    invitation token the sign-in started with (if any)."""
    if (utcnow() - login.created_at) > timedelta(minutes=STATE_MINUTES):
        raise SignInError("A belépés lejárt. Kezdd újra.")
    try:
        resp = httpx.post(settings.google_token_url, timeout=15, data={
            "code": code, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
            "redirect_uri": settings.redirect_uri, "grant_type": "authorization_code",
            "code_verifier": login.verifier})
    except httpx.HTTPError as exc:
        raise SignInError("A Google nem érhető el; próbáld újra.") from exc
    if resp.status_code != 200:
        raise SignInError("A Google nem fogadta el a belépést; kezdd újra.")
    claims = _claims(resp.json().get("id_token") or "")
    if claims.get("iss") not in ISSUERS or claims.get("aud") != settings.google_client_id:
        raise SignInError("A Google válasza nem ehhez az alkalmazáshoz szól.")
    if float(claims.get("exp") or 0) < time.time() or not secrets.compare_digest(
            str(claims.get("nonce") or ""), login.nonce):
        raise SignInError("A belépés lejárt vagy érvénytelen. Kezdd újra.")
    if not claims.get("sub") or not claims.get("email") or claims.get("email_verified") is not True:
        raise SignInError("A Google-fiók e-mail-címe nincs megerősítve.")
    return GoogleIdentity(sub=str(claims["sub"]), email=str(claims["email"]), name=claims.get("name")), \
        login.invite_token_hash


def resolve_user(db: DbSession, settings: Settings, who: GoogleIdentity, invite_hash: str | None) -> User:
    """The user this Google identity signs in as; creates or binds the account when allowed."""
    user = db.scalar(select(User).where(User.google_sub == who.sub))
    if user is None:
        by_email = user_by_email(db, who.email)
        if by_email is not None and by_email.google_sub is None:
            by_email.google_sub, user = who.sub, by_email  # created beforehand (e.g. the admin)
        elif by_email is not None:
            raise SignInError("Ez az e-mail-cím már egy másik Google-fiókhoz tartozik.")
    if user is not None:
        if user.disabled:
            raise SignInError("Ez a fiók le van tiltva.")
        if not user.name and who.name:
            user.name = who.name
        return user
    invite = None
    if invite_hash:
        invite = db.scalar(select(Invite).where(Invite.token_hash == invite_hash).with_for_update())
        if invite is None or invite.used_at or invite.revoked_at or invite.expires_at <= utcnow():
            raise SignInError("A meghívó lejárt vagy már felhasználták. Kérj újat.")
        if invite.email.strip().lower() != who.email.strip().lower():
            raise SignInError(f"A meghívó másik e-mail-címre szól; ezzel a Google-fiókkal ({who.email}) "
                              "nem használható.")
    elif settings.registration != "open":
        raise SignInError("Ide csak meghívással lehet regisztrálni. Kérj meghívót az üzemeltetőtől.")
    user = User(email=who.email, google_sub=who.sub, name=who.name, role="user")
    db.add(user)
    db.flush()
    if invite is not None:
        invite.used_at, invite.used_by = utcnow(), user.id
    return user


def create_invite(db: DbSession, settings: Settings, email: str, created_by: int | None) -> str:
    """Returns the invitation link (the token itself is stored only hashed)."""
    token = new_token()
    db.add(Invite(email=email.strip(), token_hash=token_hash(token), created_by=created_by,
                  expires_at=utcnow() + timedelta(days=settings.invite_days)))
    return f"{settings.base_url}/meghivo/{token}"


def invite_by_token(db: DbSession, token: str) -> Invite | None:
    if not token or len(token) > 100:
        return None
    invite = db.scalar(select(Invite).where(Invite.token_hash == token_hash(token)))
    if invite is None or invite.used_at or invite.revoked_at or invite.expires_at <= utcnow():
        return None
    return invite
