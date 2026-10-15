"""Remote settings and the one-line install (round 7d; plan 15b, decisions 18.11, 18.16, 18.17; protocol:
"Telepítés egy paranccsal", "Távoli beállítás").

The central service only stores and forwards: the runner reports its settings description, its plain
values and its public key; the owner's changes wait here until the runner asks for them, and the runner
checks every value itself. Keys arrive encrypted by the browser with the runner's public key and are
dropped as soon as the runner has answered. Every change, and every change made on the runner's own
machine, goes to the log; the owner gets a notification.
"""
from __future__ import annotations

import base64
import binascii
import re
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session as DbSession

from . import db
from .db import utcnow
from .models import Notification, NotifySettings, Runner, RunnerInstallCode, RunnerSettingChange
from .security import new_token, token_hash

INSTALL_MINUTES = 5
INSTALL_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MAX_LIVE_CODES = 5
DELIVERY_TIMEOUT = timedelta(minutes=10)
PENDING_DAYS = 7
MAX_CIPHERTEXT = 2048
OPEN_STATES = ("pending", "delivered")


class RemoteError(Exception):
    """A user-facing reason (Hungarian)."""


# ---- install codes ----------------------------------------------------------------------------

def normalise_install_code(code: str) -> str:
    raw = "".join(ch for ch in (code or "").upper() if ch in INSTALL_ALPHABET)
    return f"{raw[:4]}-{raw[4:]}" if len(raw) == 8 else ""


def new_install_code(user_id: int) -> str:
    """A code for the install command: 5 minutes, single use, no approval needed (decision 18.17)."""
    with db.system_session() as s:
        s.execute(delete(RunnerInstallCode).where(RunnerInstallCode.expires_at < utcnow() - timedelta(days=1)))
        live = s.scalar(select(func.count()).select_from(RunnerInstallCode).where(
            RunnerInstallCode.user_id == user_id, RunnerInstallCode.used_at.is_(None),
            RunnerInstallCode.expires_at > utcnow())) or 0
        if live >= MAX_LIVE_CODES:
            raise RemoteError("Túl sok érvényes telepítőparancs van; várj pár percet, amíg lejárnak.")
        raw = "".join(secrets.choice(INSTALL_ALPHABET) for _ in range(8))
        code = f"{raw[:4]}-{raw[4:]}"
        s.add(RunnerInstallCode(user_id=user_id, code_hash=token_hash(code),
                                expires_at=utcnow() + timedelta(minutes=INSTALL_MINUTES)))
        return code


def redeem(code: str, name: str) -> dict[str, Any] | None:
    """The install script trades its code for a runner token (once); None when unknown, used or expired."""
    code = normalise_install_code(code)
    if not code:
        return None
    from .models import User
    with db.system_session() as s:
        row = s.scalar(select(RunnerInstallCode).where(RunnerInstallCode.code_hash == token_hash(code))
                       .with_for_update())
        if row is None or row.used_at is not None or row.expires_at <= utcnow():
            return None
        user = s.get(User, row.user_id)
        if user is None or user.disabled:
            return None
        token = new_token()
        runner = Runner(user_id=row.user_id, name=(name or "").strip()[:80] or "szerver", capabilities={},
                        token_hash=token_hash(token))
        s.add(runner)
        s.flush()
        row.used_at, row.runner_id = utcnow(), runner.id
        return {"runner_id": runner.id, "name": runner.name, "token": token}


# ---- what the runner reports ---------------------------------------------------------------------

def fields_of(settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for group in (settings.get("describe") or {}).get("groups") or []:
        for f in group.get("settings") or []:
            if isinstance(f, dict) and isinstance(f.get("key"), str):
                out[f["key"]] = f
    return out


def editable(field: dict[str, Any]) -> bool:
    return bool(field.get("remote")) and not field.get("readonly")


def _norm(kind: str, text: Any) -> str:
    text = str(text if text is not None else "").strip()
    if kind == "list":
        return ", ".join(p.strip() for p in text.split(",") if p.strip())
    if kind == "map":
        parts = [p.split("=", 1) for p in re.split(r"[;\n]", text) if "=" in p]
        return "; ".join(f"{k.strip()}={v.strip()}" for k, v in parts if k.strip() and v.strip())
    if kind == "bool":
        return "igen" if text.lower() in ("1", "true", "yes", "on", "igen", "be") else "nem"
    if kind == "float":
        return text.replace(",", ".")
    return text


def shown(field: dict[str, Any], text: Any) -> str:
    """A value as the log shows it."""
    kind = field.get("kind", "text")
    value = _norm(kind, text)
    if kind == "bool":
        return "be" if value == "igen" else "ki"
    if kind == "choice":
        for c in field.get("choices") or []:
            if c.get("value") == value:
                return str(c.get("label") or value)
    if field.get("unit") and value:
        value = f"{value} {field['unit']}"
    return (value or "(üres)")[:200]


def _notify(s: DbSession, user_id: int, title: str, message: str, runner_id: int) -> None:
    cfg = s.get(NotifySettings, user_id)
    if cfg is None or cfg.channel == "none":
        return
    from .config import get_settings
    s.add(Notification(user_id=user_id, kind="runner", title=title[:200], message=message[:500],
                       url=f"{get_settings().base_url}/futtatok/{runner_id}/beallitas"))


def _expire(s: DbSession, runner_id: int | None = None) -> None:
    now = utcnow()
    where = [RunnerSettingChange.runner_id == runner_id] if runner_id is not None else []
    s.execute(update(RunnerSettingChange).where(
        *where, RunnerSettingChange.state == "delivered", RunnerSettingChange.delivered_at < now - DELIVERY_TIMEOUT)
        .values(state="failed", secrets=None, finished_at=now,
                result={"message": "A futtató átvette, de nem jelzett vissza. Nézd meg a futtató naplóját."}))
    s.execute(update(RunnerSettingChange).where(
        *where, RunnerSettingChange.state == "pending",
        RunnerSettingChange.created_at < now - timedelta(days=PENDING_DAYS))
        .values(state="failed", secrets=None, finished_at=now,
                result={"message": f"A futtató {PENDING_DAYS} napig nem jelentkezett; a változás elmaradt."}))


def store_report(runner_id: int, report: dict[str, Any]) -> int:
    """Store the runner's settings report; changes made on its machine go to the log. Returns how many
    changes wait for it."""
    with db.system_session() as s:
        r = s.get(Runner, runner_id, with_for_update=True)
        r.version = report.get("runner_version") or r.version
        r.install = report.get("install") or None
        r.latest_version = report.get("latest_version") or None
        if not report.get("remote"):
            r.remote_settings, r.settings, r.public_key = False, {}, None
        else:
            new = {k: report.get(k) for k in ("describe", "modes", "mode", "values", "secrets_set")}
            new["values"] = {str(k): str(v) for k, v in (new.get("values") or {}).items()}
            new["secrets_set"] = [str(k) for k in (new.get("secrets_set") or [])]
            old = r.settings or {}
            lines = _local_lines(fields_of(new), old, new)
            if lines:
                s.add(RunnerSettingChange(runner_id=r.id, user_id=r.user_id, origin="runner", lines=lines,
                                          state="local", finished_at=utcnow()))
                _notify(s, r.user_id, f"{r.name}: beállítás változott a gépén",
                        "; ".join(f"{x['label']}: {x['old']} → {x['new']}" for x in lines), r.id)
            r.remote_settings, r.settings, r.public_key = True, new, report.get("public_key")
        r.settings_at = utcnow()
        _expire(s, runner_id)
        return s.scalar(select(func.count()).select_from(RunnerSettingChange).where(
            RunnerSettingChange.runner_id == runner_id, RunnerSettingChange.state == "pending")) or 0


def _local_lines(fields: dict[str, dict[str, Any]], old: dict[str, Any], new: dict[str, Any]) -> list[dict]:
    if not old.get("values"):
        return []  # the first report (or remote settings just switched on): nothing to compare with
    lines = []
    before, after = old.get("values") or {}, new.get("values") or {}
    for key, value in after.items():
        if key not in before:
            continue
        f = fields.get(key, {"label": key, "kind": "text"})
        if _norm(f.get("kind", "text"), before[key]) != _norm(f.get("kind", "text"), value):
            lines.append({"key": key, "label": f.get("label") or key, "old": shown(f, before[key]),
                          "new": shown(f, value)})
    had, has = set(old.get("secrets_set") or []), set(new.get("secrets_set") or [])
    for key in sorted(had ^ has):
        f = fields.get(key, {"label": key})
        lines.append({"key": key, "label": f.get("label") or key, "old": "beállítva" if key in had else "nincs",
                      "new": "beállítva" if key in has else "törölve", "secret": True})
    return lines


# ---- changes from dwyt ---------------------------------------------------------------------------

def owned(s: DbSession, user_id: int, runner_id: int) -> Runner:
    r = s.get(Runner, runner_id, with_for_update=True)
    if r is None or r.user_id != user_id or r.revoked_at is not None:
        raise RemoteError("Nincs ilyen futtatód.")
    return r


def create_change(user_id: int, by_name: str, runner_id: int, values: dict[str, str],
                  encrypted: dict[str, str] | None = None, clear: list[str] | None = None) -> int | None:
    """Store the owner's changes (only what differs from the runner's last report). Returns the change id,
    None when nothing changed; RemoteError when the runner does not allow it or a field is not editable."""
    encrypted, clear = encrypted or {}, clear or []
    with db.system_session() as s:
        r = owned(s, user_id, runner_id)
        if not r.remote_settings or not r.settings:
            raise RemoteError("Ez a futtató nem engedi a dwyt-ből való beállítást (a gépén kapcsolható be).")
        fields = fields_of(r.settings)
        current = r.settings.get("values") or {}
        payload, lines = {}, []
        for key, text in values.items():
            f = fields.get(key)
            if f is None or not editable(f) or f.get("kind") == "secret":
                raise RemoteError(f"Ez a beállítás nem állítható a dwyt-ből: {key}")
            text = str(text)[:4000]
            kind = f.get("kind", "text")
            if _norm(kind, text) == _norm(kind, current.get(key, "")):
                continue
            payload[key] = _norm(kind, text) if kind in ("list", "map", "bool") else text.strip()
            lines.append({"key": key, "label": f.get("label") or key, "old": shown(f, current.get(key, "")),
                          "new": shown(f, text)})
        sealed = {}
        for key, blob in encrypted.items():
            f = fields.get(key)
            if f is None or not editable(f) or f.get("kind") != "secret":
                raise RemoteError(f"Ez a kulcs nem állítható a dwyt-ből: {key}")
            blob = (blob or "").strip()
            if not blob:
                continue
            try:
                if len(blob) > MAX_CIPHERTEXT or not base64.b64decode(blob, validate=True):
                    raise ValueError
            except (ValueError, binascii.Error):
                raise RemoteError("A titkosított kulcs hibás; töltsd újra az oldalt, és add meg újra.") from None
            if not r.public_key:
                raise RemoteError("A futtató még nem küldte el a nyilvános kulcsát.")
            sealed[key] = blob
            lines.append({"key": key, "label": f.get("label") or key, "old": "", "new": "új kulcs", "secret": True})
        cleared = []
        for key in clear:
            f = fields.get(key)
            if f is None or not editable(f) or f.get("kind") != "secret" or key in sealed:
                continue
            if key in (r.settings.get("secrets_set") or []):
                cleared.append(key)
                lines.append({"key": key, "label": f.get("label") or key, "old": "", "new": "törölve",
                              "secret": True})
        if not lines:
            return None
        change = RunnerSettingChange(runner_id=r.id, user_id=user_id, origin="dwyt", by_name=by_name[:200],
                                     lines=lines, payload_values=payload, secrets=sealed or None,
                                     clear_keys=cleared, state="pending")
        s.add(change)
        s.flush()
        _notify(s, user_id, f"{r.name}: beállítás módosítva a dwyt-ből",
                "; ".join(f"{x['label']}: {x['new']}" if x.get("secret") else f"{x['label']}: {x['old']} → {x['new']}"
                          for x in lines), r.id)
        return change.id


def newer(candidate: str | None, current: str | None) -> bool:
    def key(v: str) -> tuple:
        return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-+]", v))
    return bool(candidate) and bool(current) and key(candidate) > key(current)


def can_update(r: Any) -> bool:
    return bool(r.remote_settings and r.install == "service" and newer(r.latest_version, r.version))


def request_update(user_id: int, by_name: str, runner_id: int) -> int:
    """Frissítés most (decision 18.16): the runner installs the newest version when it is idle."""
    with db.system_session() as s:
        r = owned(s, user_id, runner_id)
        if not r.remote_settings or r.install != "service":
            raise RemoteError("Ezt a futtatót a saját gépén kell frissíteni.")
        if s.scalar(select(RunnerSettingChange.id).where(
                RunnerSettingChange.runner_id == r.id, RunnerSettingChange.action == "update",
                RunnerSettingChange.state.in_(OPEN_STATES))) is not None:
            raise RemoteError("A frissítés már folyamatban van.")
        change = RunnerSettingChange(runner_id=r.id, user_id=user_id, origin="dwyt", by_name=by_name[:200],
                                     lines=[{"key": "", "label": "Frissítés most", "old": r.version or "?",
                                             "new": r.latest_version or "a legújabb"}],
                                     action="update", state="pending")
        s.add(change)
        s.flush()
        _notify(s, user_id, f"{r.name}: frissítés kérve", f"{r.version} → {r.latest_version}", r.id)
        return change.id


# ---- the runner takes and answers -------------------------------------------------------------------

def has_pending(runner_id: int) -> bool:
    with db.system_session() as s:
        return s.scalar(select(RunnerSettingChange.id).join(Runner, Runner.id == RunnerSettingChange.runner_id)
                        .where(RunnerSettingChange.runner_id == runner_id, RunnerSettingChange.state == "pending",
                               Runner.remote_settings.is_(True)).limit(1)) is not None


def take(runner_id: int) -> list[dict[str, Any]]:
    with db.system_session() as s:
        _expire(s, runner_id)
        r = s.get(Runner, runner_id)
        if r is None or not r.remote_settings:
            return []
        rows = s.scalars(select(RunnerSettingChange).where(
            RunnerSettingChange.runner_id == runner_id, RunnerSettingChange.state == "pending")
            .order_by(RunnerSettingChange.id).with_for_update()).all()
        out = []
        for c in rows:
            c.state, c.delivered_at = "delivered", utcnow()
            out.append({"id": c.id, "values": c.payload_values or {}, "secrets": c.secrets or {},
                        "clear": c.clear_keys or [], "action": c.action, "by": c.by_name})
        return out


def finish(runner_id: int, change_id: int, result: dict[str, Any]) -> bool:
    with db.system_session() as s:
        c = s.get(RunnerSettingChange, change_id, with_for_update=True)
        if c is None or c.runner_id != runner_id or c.origin != "dwyt" or c.state not in OPEN_STATES:
            return False
        r = s.get(Runner, runner_id, with_for_update=True)
        applied = [k for k in result.get("applied") or [] if isinstance(k, str)]
        errors = {str(k): str(v)[:300] for k, v in (result.get("errors") or {}).items()}
        if c.action == "update":
            c.state = "done" if result.get("ok") else "failed"
        elif errors and applied:
            c.state = "partial"
        elif errors:
            c.state = "refused"
        else:
            c.state = "done"
        c.result = {"applied": applied, "errors": errors, "message": str(result.get("message") or "")[:500],
                    "checks": {str(k): [bool(v[0]), str(v[1])[:200]] for k, v in (result.get("checks") or {}).items()
                               if isinstance(v, (list, tuple)) and len(v) == 2}}
        c.secrets, c.finished_at = None, utcnow()
        settings = dict(r.settings or {})
        values = dict(settings.get("values") or {})
        for key in applied:  # the next report then shows no "changed on its machine" for these
            if key in (c.payload_values or {}):
                values[key] = c.payload_values[key]
        settings["values"] = values
        secret_set = set(settings.get("secrets_set") or [])
        secret_set |= {k for k in applied if any(x.get("key") == k and x.get("new") == "új kulcs" for x in c.lines)}
        secret_set -= {k for k in applied if k in (c.clear_keys or [])}
        settings["secrets_set"] = sorted(secret_set)
        r.settings = settings
        if "name" in applied and c.payload_values.get("name"):
            r.name = str(c.payload_values["name"])[:80]
        if c.state in ("refused", "partial", "failed"):
            _notify(s, c.user_id, f"{r.name}: a futtató nem vett át mindent",
                    "; ".join(errors.values()) or c.result["message"] or "ismeretlen ok", r.id)
        return True


# ---- the settings page ------------------------------------------------------------------------------

def page_data(user_id: int, runner_id: int) -> dict[str, Any] | None:
    with db.user_session(user_id) as s:
        r = s.get(Runner, runner_id)
        if r is None or r.revoked_at is not None:
            return None
        s.expunge(r)
        rows = s.scalars(select(RunnerSettingChange).where(RunnerSettingChange.runner_id == runner_id)
                         .order_by(RunnerSettingChange.id.desc()).limit(40)).all()
        for row in rows:
            s.expunge(row)
    open_ = [c for c in rows if c.state in OPEN_STATES]
    recent = [c for c in rows if c.origin == "dwyt" and c.state not in OPEN_STATES and c.finished_at
              and (utcnow() - c.finished_at).total_seconds() < 600][:3]
    return {"runner": r, "changes": rows, "open": open_, "recent": recent}


def any_open(user_id: int, runner_id: int) -> bool:
    with db.user_session(user_id) as s:
        return s.scalar(select(RunnerSettingChange.id).where(
            RunnerSettingChange.runner_id == runner_id,
            RunnerSettingChange.state.in_(OPEN_STATES)).limit(1)) is not None
