"""Administration on the host:  dwyt-central <command>

  create-admin EMAIL   the admin account: password + TOTP for the fallback login; the same
                       e-mail address also signs in with Google once (and is then bound to it)
  set-password EMAIL   new admin password; logs out every session
  reset-totp EMAIL     new authenticator secret; logs out every session
  unlock EMAIL         clear the lockout after failed attempts
  invite EMAIL         print an invitation link (when there is no admin page at hand)
  check                database, row-level security, settings
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

import segno
from sqlalchemy import select, text

from . import accounts, db, security
from .config import get_settings
from .models import User

MIN_PASSWORD = 12


def _ask_password() -> str:
    while True:
        first = getpass.getpass("Jelszó (legalább 12 karakter): ")
        if len(first) < MIN_PASSWORD:
            print("Túl rövid.")
            continue
        if getpass.getpass("Jelszó újra: ") != first:
            print("A két jelszó nem egyezik.")
            continue
        return first


def _show_totp(user: User) -> None:
    uri = security.totp_uri(user)
    print("\nOlvasd be ezt a QR-kódot a hitelesítő alkalmazással (Google Authenticator, Aegis, 2FAS ...):\n")
    segno.make(uri, error="m").terminal(compact=True)
    print(f"\nVagy add meg kézzel ezt a kulcsot: {user.totp_secret}\n")


def _checked_email(email: str, confirm: bool) -> str:
    """Refuse malformed addresses; interactively, show the address and ask before using it
    (a stray key or terminal escape pasted before the address is easy to miss)."""
    clean = accounts.valid_email(email)
    if clean is None:
        sys.exit(f"Érvénytelen e-mail-cím: {email!r}")
    if confirm and sys.stdin.isatty():
        answer = input(f"E-mail-cím: {clean}  — helyes? [i/N] ").strip().lower()
        if answer not in ("i", "igen", "y", "yes"):
            sys.exit("Megszakítva; semmi nem változott.")
    return clean


def _admin(s, email: str) -> User:
    user = security.user_by_email(s, email)
    if user is None or user.role != "admin":
        sys.exit(f"Nincs ilyen admin: {email}")
    return user


def create_admin(email: str, password: str | None = None) -> None:
    email = _checked_email(email, confirm=password is None)
    password = password or _ask_password()
    with db.system_session() as s:
        user = security.user_by_email(s, email)
        if user is None:
            user = User(email=email.strip(), role="admin")
            s.add(user)
        user.role = "admin"
        user.password_hash = security.hash_password(password)
        user.totp_secret, user.totp_last_step = security.new_totp_secret(), 0
        user.disabled = False
    _show_totp(user)
    print(f"Az admin ({user.email}) kész. Google-lel is beléphet ezzel a címmel; jelszóval: /admin-belepes")


def set_password(email: str) -> None:
    password = _ask_password()
    with db.system_session() as s:
        user = _admin(s, email)
        user.password_hash = security.hash_password(password)
        security.end_all_sessions(s, user.id)
    print("A jelszó megváltozott; minden munkamenet kijelentkezett.")


def reset_totp(email: str) -> None:
    with db.system_session() as s:
        user = _admin(s, email)
        user.totp_secret, user.totp_last_step = security.new_totp_secret(), 0
        security.end_all_sessions(s, user.id)
    _show_totp(user)


def unlock(email: str) -> None:
    with db.system_session() as s:
        user = security.user_by_email(s, email)
        if user is None:
            sys.exit(f"Nincs ilyen felhasználó: {email}")
        security.record_success(user)
    print("Feloldva.")


def invite(email: str) -> None:
    email = _checked_email(email, confirm=True)
    with db.system_session() as s:
        link = accounts.create_invite(s, get_settings(), email, None)
    print(link)


def check() -> None:
    settings = get_settings()
    ok = True
    try:
        with db.system_session() as s:
            version = s.scalar(text("SELECT version_num FROM alembic_version"))
            admins = s.scalar(select(User.id).where(User.role == "admin").limit(1))
            rls = s.execute(text("SELECT relname FROM pg_class WHERE relname IN ('items', 'jobs', 'transcripts') "
                                 "AND relrowsecurity")).scalars().all()
        print(f"adatbázis (rendszer-szerepkör): rendben, séma {version}")
        print(f"sorvédelem: {'bekapcsolva' if len(rls) == 3 else 'HIBA: hiányzik'}")
        ok &= len(rls) == 3
        if admins is None:
            print("figyelem: még nincs admin (dwyt-central create-admin EMAIL)")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"adatbázis (rendszer-szerepkör): HIBA: {exc}")
    try:
        with db.app_engine().connect() as conn:
            role = conn.execute(text("SELECT current_user, rolbypassrls, rolsuper FROM pg_roles "
                                     "WHERE rolname = current_user")).one()
        if role.rolbypassrls or role.rolsuper:
            ok = False
            print(f"alkalmazás-szerepkör ({role.current_user}): HIBA: megkerülheti a sorvédelmet")
        else:
            print(f"alkalmazás-szerepkör ({role.current_user}): rendben")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"alkalmazás-szerepkör: HIBA: {exc}")
    print(f"cím: {settings.base_url}  (Google visszairányítás: {settings.redirect_uri})")
    print("Google-belépés: " + ("beállítva" if settings.google_client_id else "nincs beállítva"))
    print(f"regisztráció: {settings.registration}")
    from . import embed_client
    if not settings.embed_url:
        print("keresés: csak szöveges (EMBED_URL üres)")
    else:
        health = embed_client.health(settings.embed_url)
        if health is None:
            print(f"figyelem: a beágyazó szolgáltatás nem érhető el ({settings.embed_url}); a keresés csak szöveges, "
                  "amíg el nem indul (systemctl status dwyt-central-embed)")
        else:
            print("beágyazó szolgáltatás: " + ("rendben" if health.get("ok") else "még tölt" if health.get("loading")
                                               else f"HIBA: {health.get('error')}") + f" ({health.get('model')})")
    try:
        with db.system_session() as s:
            ext = s.execute(text("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'unaccent', 'pg_trgm')"))
            missing = {"vector", "unaccent", "pg_trgm"} - set(ext.scalars().all())
            waiting = s.scalar(text("SELECT count(*) FROM index_queue"))
        print("bővítmények: " + ("rendben" if not missing else f"HIBA: hiányzik {', '.join(sorted(missing))}"))
        ok &= not missing
        print(f"indexelésre vár: {waiting} elem")
    except Exception as exc:  # noqa: BLE001
        print(f"index: HIBA: {exc}")
    if settings.base_url.startswith("https://") and not settings.cookie_secure:
        print("figyelem: HTTPS mellett COOKIE_SECURE=true legyen")
    trusted = os.environ.get("FORWARDED_ALLOW_IPS", "").strip()
    if not trusted or trusted == "APACHE-IP":
        print("figyelem: FORWARDED_ALLOW_IPS nincs beállítva: a kliens címe az Apache címe lesz, így a "
              "kéréskorlátok mindenkire együtt vonatkoznak")
    elif "*" in trusted:
        ok = False
        print("HIBA: FORWARDED_ALLOW_IPS=* esetén bárki hamisíthatja a címét; az Apache-gép címét írd be")
    else:
        print(f"megbízható proxy (FORWARDED_ALLOW_IPS): {trusted}")
    sys.exit(0 if ok else 1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="dwyt-central")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("create-admin", "set-password", "reset-totp", "unlock", "invite"):
        sub.add_parser(name).add_argument("email")
    sub.add_parser("check")
    args = parser.parse_args()
    {"create-admin": lambda: create_admin(args.email), "set-password": lambda: set_password(args.email),
     "reset-totp": lambda: reset_totp(args.email), "unlock": lambda: unlock(args.email),
     "invite": lambda: invite(args.email), "check": check}[args.cmd]()


if __name__ == "__main__":
    main()
